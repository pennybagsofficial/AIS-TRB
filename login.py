"""
Run this ONCE on your own computer to create the session string.

    pip install telethon
    python login.py

It will ask for your API_ID, API_HASH, phone number, the login code Telegram
sends you, and (if set) your 2-step password. At the end it prints a long
SESSION STRING.

Copy that string and save it as the GitHub secret named  TG_SESSION.
Treat it like a password -- anyone who has it can use this account.

>>> Use a SECONDARY Telegram account here, not your main personal account. <<<
"""

from telethon import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\n================= TG_SESSION (save as a GitHub secret) =================\n")
    print(client.session.save())
    print("\n========================================================================\n")
    print("Done. Keep this string private.")
