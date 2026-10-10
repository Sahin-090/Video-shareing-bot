"""Run this ONCE on your own computer (not on Railway) to create SESSION_STRING.

    pip install telethon
    python generate_session.py

It asks for API_ID / API_HASH (from https://my.telegram.org -> API development tools), then
your PHONE NUMBER (a normal user account - never the bot token, bots cannot read channel history) and the login code Telegram sends you. Use an account that is a member of the
source channel. The printed string gives full access to that account: keep it secret and put it
only in Railway's Variables, never in git.
"""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nSESSION_STRING (copy everything on the next line):\n")
    print(client.session.save())
