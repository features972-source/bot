"""One-time Telethon login for the mailer userbot bridge.

Usage:
  python scripts/telethon_login.py
  python scripts/telethon_login.py --env-file .env.bot2
  python scripts/telethon_login.py --env-file .env.q1australia
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from telethon import TelegramClient

from config import load_settings


async def main() -> None:
    settings = load_settings()
    if not settings.mailer_bridge_enabled:
        print(
            "Set TELETHON_API_ID and TELETHON_API_HASH in your .env first "
            "(from https://my.telegram.org)."
        )
        sys.exit(1)

    client = TelegramClient(
        settings.telethon_session_path,
        settings.telethon_api_id,
        settings.telethon_api_hash,
    )
    await client.start()
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (@{me.username})")
    print(f"Session saved: {settings.telethon_session_path}.session")
    await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Log in Telethon userbot for mailer bridge")
    parser.add_argument(
        "--env-file",
        default=None,
        help="Env file (e.g. .env.bot2, .env.q1australia)",
    )
    args = parser.parse_args()
    if args.env_file:
        os.environ["BOT_ENV_FILE"] = args.env_file
    asyncio.run(main())
