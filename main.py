import os
import time
import traceback

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
        print(resp)
        content = resp["choices"][0]["delta"].get("content")
        if content is None:
            return
        yield content


def get_chat_history(peer_id):
    messages = vk.messages.getHistory(peer_id=peer_id, count=MESSAGES_COUNT)
    return messages["items"][::-1]


def get_names_from_message(messages):
    ids = list(map(lambda message: message["from_id"], messages))
    user_ids = list(map(str, filter(lambda x: x > 0, ids)))
    group_ids = list(map(lambda x: str(-x), filter(lambda x: x < 0, ids)))

    users = user_ids and vk.users.get(user_ids=",".join(user_ids))
    groups = group_ids and vk.groups.getById(group_ids=",".join(group_ids))

    id_to_names = {}
    for user in users:
        id_to_names[user["id"]] = user["first_name"] + " " + user["last_name"]
    for group in groups:
        id_to_names[-group["id"]] = group["name"]

    return id_to_names


def insert_names(messages, id_to_names):
    result = []
    for message in messages:
        message["name"] = id_to_names[message["from_id"]]
        result.append(message)
    return result


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
    vk.messages.setActivity(peer_id=peer_id, type="typing")

def mark_as_read(peer_id):
    vk.messages.markAsRead(peer_id=peer_id)


def reply_chat(peer_id):
    mark_as_read(peer_id)
    messages = get_chat_history(peer_id)
    id_to_name = get_names_from_message(messages)
    messages_with_name = insert_names(messages, id_to_name)
    formatted_history = format_messages_for_gpt(messages_with_name)

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
        if event.type == VkEventType.MESSAGE_NEW and event.peer_id == TARGET_PEER_ID and not event.from_me:
            if TRIGGER_WORD in event.text.lower():
                reply_chat(TARGET_PEER_ID)


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            print(traceback.format_exc())
