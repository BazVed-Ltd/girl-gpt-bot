import os
import time
import traceback
from functools import partial

import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
import openai


def get_env(name: str) -> str:
    env = os.getenv(name)
    if not env:
        raise Exception(f"Env {name} not set")
    return env


openai.api_key = get_env("OPENAI_TOKEN")

CHAT_OFFSET = 2000000000

MESSAGES_COUNT = 10
TARGET_PEER_ID = CHAT_OFFSET + int(get_env("CHAT_ID"))

vk_session = vk_api.VkApi(token=get_env("VK_TOKEN"))
vk = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

TRIGGER_WORD = get_env("TRIGGER_WORD")
NAME = get_env("BOT_NAME")
PROMPT = f"""\
Your task is to play a girl named {NAME}.
You entertain the chat users.
You always use an abundance of emoji and silly jokes about all chat members.
You really like male attention.
You don't write more than 5 sentences.
"""


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


def get_chat_history(peer_id):
    messages = vk.messages.getHistory(peer_id=peer_id, count=MESSAGES_COUNT)
    return reversed(messages["items"])


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
    if ids == []:
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
        if not text:
            continue

        author = message["name"]
        result += f"{author}:\n{text}\n"

    return result


def send_message(peer_id, message):
    return vk.messages.send(peer_id=peer_id, message=message, random_id=0)


def send_typing(peer_id):
    return vk.messages.setActivity(peer_id=peer_id, type="typing")


def mark_as_read(peer_id):
    return vk.messages.markAsRead(peer_id=peer_id)


def reply_chat(peer_id):
    mark_as_read(peer_id)

    messages = list(get_chat_history(peer_id))
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

    send_message(peer_id, response)


def main():
    for event in longpoll.listen():
        if (
            event.type == VkEventType.MESSAGE_NEW
            and event.peer_id == TARGET_PEER_ID
            and not event.from_me
        ):
            if TRIGGER_WORD in event.text.lower():
                reply_chat(TARGET_PEER_ID)


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            print(traceback.format_exc())
