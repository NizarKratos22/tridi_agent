"""
Run this ONCE to see all Telegram channels/groups you have joined.
Copy the usernames you want into your .env as CHANNEL_1 … CHANNEL_4.

Usage:
  python list_channels.py
"""
import asyncio
import os
import sys
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

load_dotenv()


async def main():
    api_id   = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    phone    = os.environ["TELEGRAM_PHONE"].split("#")[0].strip()   # strip inline comment

    client = TelegramClient("session_tridi", api_id, api_hash)

    # start() will prompt for the SMS code in the terminal automatically
    await client.start(phone=phone)

    print("\n" + "="*72)
    print(f"  {'#':<4}  {'Type':<10}  {'Username / ID':<32}  Title")
    print("="*72)

    idx = 1
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not isinstance(entity, (Channel, Chat)):
            continue

        username = getattr(entity, "username", None) or f"id:{entity.id}"
        kind     = "Channel" if isinstance(entity, Channel) else "Group"
        title    = dialog.name or ""

        print(f"  {idx:<4}  {kind:<10}  {username:<32}  {title}")
        idx += 1

    print("="*72)
    print()
    print("Copy the usernames (or id:XXXXXXX) you want into .env:")
    print("  CHANNEL_1=username_here")
    print("  CHANNEL_2=username_here  (etc.)\n")

    await client.disconnect()


if __name__ == "__main__":
    # Windows needs this for asyncio + stdin
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
