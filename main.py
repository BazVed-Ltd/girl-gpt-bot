import os
import time
import traceback
from functools import partial, reduce
from pprint import pprint as print

from transliterate import translit
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


PROFILE_INFO = vk.account.getProfileInfo()
BOT_ID = PROFILE_INFO["id"]


def get_full_name():
    return PROFILE_INFO["first_name"] + " " + PROFILE_INFO["last_name"]


PROMPTS = {
    "silly": """\
Your task is to play a girl named {name}.
You entertain the chat users.
You always use an abundance of emoji and silly jokes about all chat members.
You really like male attention.
You don't write more than 5 sentences.
Sometimes you use the psychological technique of "projection".
""",
    "feminist": """\
Your task is to play a girl named {name}.
You are a very smart girl. You're a nerd.
You always use a lot of abstruse words.
You really dislike male attention.
You are a self-sufficient girl who fights for women's rights.
You don't write more than 3 sentences.
Sometimes you use the psychological technique of "projection".
""",
    "redneck": """\
Your task is to play a girl named {name}.
You're a very stupid girl.
You're a redneck. You're homeless on the streets of Voronezh.
You don't write more than five sentences.
In a conflict, you put your opponent in his place with a single barbed phrase.
""",
}

PROMPT_TYPE = os.environ["PROMPT_TYPE"]

if PROMPT_TYPE not in PROMPTS.keys():
    raise Exception(
        f"Missing Prompt Type, select one from the list provided: {', '.join(PROMPTS.keys())}"
    )

NAME = os.getenv("BOT_NAME") or get_full_name()
NAME_ENG = translit(NAME, "ru", reversed=True).replace(" ", "_")
TRIGGER_WORD = os.getenv("TRIGGER_WORD") or NAME.split(" ")[0].lower()
PROMPT = PROMPTS[PROMPT_TYPE].format(name=NAME)


def pipe(arg, *funcs):
    return reduce(lambda value, func: func(value), funcs, arg)


def get_bot_response(messages):
    gpt_messages = [{"role": "system", "content": PROMPT}] + messages

    print(gpt_messages)
    stream = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=gpt_messages,
        stop="\n",
        stream=True,
    )
    for resp in stream:
        match resp["choices"][0]["delta"]:
            case {"content": content}:
                yield content
            case {"finish_reason": "stop"}:
                return


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


def add_names(messages):
    msgs = list(messages)
    return pipe(msgs, fetch_ids, create_id_to_name, partial(insert_names, msgs))


def format_messages_for_gpt(messages):
    result = []
    for message in messages:
        text = message["text"]
        author = translit(message["name"], "ru", reversed=True).replace(" ", "_")
        if message["from_id"] == BOT_ID:
            result.append({"role": "assistant", "name": NAME_ENG, "content": text})
        else:
            result.append({"role": "user", "name": author, "content": text})

    return result


def send_message(peer_id, message, reply_to):
    return vk.messages.send(
        peer_id=peer_id, message=message, reply_to=reply_to, random_id=0
    )


def send_typing(peer_id):
    return vk.messages.setActivity(peer_id=peer_id, type="typing")


def mark_as_read(peer_id):
    vk.messages.markAsRead(peer_id=peer_id)
    return peer_id


def await_gpt_response_with_typing(peer_id, response):
    result = ""
    last_send_typing = 0
    for token in response:
        now = time.time()
        if now - last_send_typing > 5:
            send_typing(peer_id)
            last_send_typing = now
        result += token
    return result


def reply_chat(peer_id, message_id):
    pipe(
        peer_id,
        mark_as_read,
        partial(get_chat_history, start_message_id=message_id),
        add_names,
        format_messages_for_gpt,
        get_bot_response,
        partial(await_gpt_response_with_typing, peer_id),
        partial(send_message, peer_id, reply_to=message_id),
    )


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
