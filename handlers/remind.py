"""
/remind <time> <message>
Examples:
  /remind 30m call back John
  /remind 1h follow up with client
  /remind 2h30m check voicemail
  /remind 45 ring back 07401234567
"""
from __future__ import annotations

import asyncio
import re

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes


def _parse_duration(text: str) -> int | None:
    """Parse a duration string into total seconds. Returns None if unparseable."""
    text = text.strip().lower()

    total = 0

    # Match explicit h/m components e.g. 2h30m, 1h, 45m
    matched = False
    for value, unit in re.findall(r"(\d+)\s*([hm])", text):
        matched = True
        if unit == "h":
            total += int(value) * 3600
        elif unit == "m":
            total += int(value) * 60

    if matched:
        return total if total > 0 else None

    # Bare number with no suffix — treat as minutes
    bare = re.fullmatch(r"\d+", text)
    if bare:
        total = int(text) * 60
        return total if total > 0 else None

    return None


def _format_duration_human(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


async def remind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /remind <time> <message>\n"
            "Examples:\n"
            "  /remind 30m call back John\n"
            "  /remind 1h follow up with client\n"
            "  /remind 2h30m check voicemail"
        )
        return

    duration_str = args[0]
    seconds = _parse_duration(duration_str)

    if seconds is None:
        await update.message.reply_text(
            f"❌ Couldn't understand time <b>{duration_str}</b>.\n"
            "Use formats like: <b>30m</b>, <b>1h</b>, <b>2h30m</b>",
            parse_mode="HTML",
        )
        return

    note = " ".join(args[1:]).strip() if len(args) > 1 else "reminder"
    human = _format_duration_human(seconds)
    user = update.effective_user
    chat_id = update.effective_chat.id if update.effective_chat else user.id
    message_id = update.message.message_id

    await update.message.reply_text(
        f"⏰ Got it! I'll remind you in <b>{human}</b>.",
        parse_mode="HTML",
    )

    async def _fire() -> None:
        await asyncio.sleep(seconds)
        mention = f"@{user.username}" if user.username else user.first_name
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ {mention} — <b>{note}</b>",
                parse_mode="HTML",
                reply_to_message_id=message_id,
            )
        except Exception:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏰ {mention} — <b>{note}</b>",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    asyncio.ensure_future(_fire())


def build_remind_handlers() -> list:
    return [CommandHandler("remind", remind_command)]
