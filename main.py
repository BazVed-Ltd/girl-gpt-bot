import os
import time
import traceback
from functools import partial

import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
import openai

openai.api_key = os.environ["OPENAI_TOKEN"]


vk_session = vk_api.VkApi(token=os.environ["VK_TOKEN"])
vk = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

CHAT_OFFSET = 2000000000

MESSAGES_COUNT = 10
TARGET_PEER_ID = CHAT_OFFSET + int(os.environ["CHAT_ID"])

if os.getenv("IGNORE_LIST"):
    IGNORE_LIST = list(map(int, os.environ["IGNORE_LIST"].split(",")))
else:
    IGNORE_LIST = []


def get_full_name():
    profile_info = vk.account.getProfileInfo()
    return profile_info["first_name"] + " " + profile_info["last_name"]


PROMPTS = {
    "silly": """\
Your task is to play a girl named {name}.
You entertain the chat users.
You always use an abundance of emoji and silly jokes about all chat members.
You really like male attention.
You don't write more than 5 sentences.
Sometimes you use the psychological technique of "projection".
""",
    "nerd": """\
Your task is to play a girl named {name}.
You are a very smart girl. You're a nerd.
You always use a lot of abstruse words.
You really dislike male attention.
You are a self-sufficient girl who fights for women's rights.
You don't write more than 3 sentences.
Sometimes you use the psychological technique of "projection".
""",
}

PROMPT_TYPE = os.environ["PROMPT_TYPE"]

if PROMPT_TYPE not in PROMPTS.keys():
    raise Exception(
        f"Missing Prompt Type, select one from the list provided: {', '.join(PROMPTS.keys())}"
    )

NAME = os.getenv("BOT_NAME") or get_full_name()
TRIGGER_WORD = os.getenv("TRIGGER_WORD") or NAME.split(" ")[0].lower()
PROMPT = PROMPTS[PROMPT_TYPE].format(name=NAME)


def pipe(arg, *funcs):
    result = arg
    for func in funcs:
        result = func(result)
    return result


def get_bot_response(message):
    stream = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": message},
        ],
        stream=True,
    )
    for resp in stream:
        content = resp["choices"][0]["delta"].get("content")
        if content is None:
            return
        yield content


def get_chat_history(peer_id, start_message_id):
    result = []
    offset = 0
    while len(result) < MESSAGES_COUNT:
        messages = vk.messages.getHistory(
            peer_id=peer_id,
            start_message_id=start_message_id,
            count=MESSAGES_COUNT,
            offset=offset,
        )
        result += list(filter(lambda message: bool(message["text"]), messages["items"]))
        offset += 10
    return reversed(result[:MESSAGES_COUNT])


def fetch_ids(messages):
    for message in messages:
        yield message["from_id"]


def split_user_group_ids(ids):
    user_ids = []
    group_ids = []
    for item_id in ids:
        if item_id > 0:
            user_ids.append(str(item_id))
        else:
            group_ids.append(str(-item_id))
    return group_ids, user_ids


def get_names(ids, make_req, fetch_id, fetch_name):
    if not ids:
        return {}
    items = make_req(",".join(list(ids)))
    id_to_name = {}
    for item in items:
        id_to_name[fetch_id(item)] = fetch_name(item)
    return id_to_name


def get_user_names(ids):
    return get_names(
        ids,
        lambda ids: vk.users.get(user_ids=ids),
        lambda user: user["id"],
        lambda user: user["first_name"] + " " + user["last_name"],
    )


def get_group_names(ids):
    return get_names(
        ids,
        lambda ids: vk.groups.getById(group_ids=ids),
        lambda group: -group["id"],
        lambda group: group["name"],
    )


def create_id_to_name(ids):
    group_ids, user_ids = split_user_group_ids(ids)

    return get_group_names(group_ids) | get_user_names(user_ids)


def insert_names(messages, id_to_name):
    for message in messages:
        message["name"] = id_to_name[message["from_id"]]
        yield message


def format_messages_for_gpt(messages):
    result = ""
    for message in messages:
        text = message["text"]
        author = message["name"]
        result += f"{author}:\n{text}\n"

    return result


def strip_name(text):
    return text.removeprefix(f"{NAME}:")


def send_message(peer_id, message):
    return vk.messages.send(peer_id=peer_id, message=message, random_id=0)


def send_typing(peer_id):
    return vk.messages.setActivity(peer_id=peer_id, type="typing")


def mark_as_read(peer_id):
    return vk.messages.markAsRead(peer_id=peer_id)


def reply_chat(peer_id, start_message_id):
    mark_as_read(peer_id)

    messages = list(get_chat_history(peer_id, start_message_id))
    id_to_name = pipe(messages, fetch_ids, create_id_to_name)
    formatted_history = pipe(
        id_to_name, partial(insert_names, messages), format_messages_for_gpt
    )

    # TODO: Refactor this loop by moving to function
    response = ""
    last_send_typing = 0
    for token in get_bot_response(formatted_history):
        now = time.time()
        if now - last_send_typing > 5:
            send_typing(peer_id)
            last_send_typing = now
        response += token

    pipe(response, strip_name, partial(send_message, peer_id))


def main():
    for event in longpoll.listen():
        if (
            event.type == VkEventType.MESSAGE_NEW
            and event.peer_id == TARGET_PEER_ID
            and not event.from_me
            and event.user_id not in IGNORE_LIST
        ):
            if TRIGGER_WORD in event.text.lower():
                reply_chat(TARGET_PEER_ID, event.message_id)


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            print(traceback.format_exc())
