"""Shift-start ready check command and scheduled prompts."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from config import Settings
from database import get_link_by_telegram_user_id, list_links, ready_check_sent_today
from handlers.stats_period import stats_timezone
from ready_check_service import (
    CALLBACK_PREFIX,
    load_session,
    refresh_ready_check_message,
    send_ready_check,
)

logger = logging.getLogger(__name__)

READY_LOOP_SECONDS = 45


def build_ready_check_handlers() -> list:
    return []


async def ready_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    link = get_link_by_telegram_user_id(settings.database_path, user.id)
    if link is None:
        await message.reply_text(
            "You need a linked 3CX extension first.\n\n"
            "Ask an admin to reply to your message with /link 101"
        )
        return

    session = load_session(context.bot_data, user.id)
    session["headset"] = False
    session["softphone_manual"] = False

    await send_ready_check(
        context.bot,
        settings,
        context.bot_data,
        link,
        intro="🟢 <b>Ready check</b>",
    )


async def ready_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    await query.answer()
    action = (query.data or "").removeprefix(CALLBACK_PREFIX)
    session = load_session(context.bot_data, user.id)

    if action == "headset":
        session["headset"] = True
    elif action == "softphone":
        session["softphone_manual"] = True
    elif action != "recheck":
        return

    settings: Settings = context.bot_data["settings"]
    await refresh_ready_check_message(
        context.bot,
        settings,
        context.bot_data,
        user.id,
        intro="🟢 <b>Ready check</b>",
    )


async def ready_check_shift_loop(
    bot,
    settings: Settings,
    bot_data: dict,
) -> None:
    """Once per day at shift hour, DM linked agents a ready check."""
    last_slot: str | None = None
    while True:
        try:
            await asyncio.sleep(READY_LOOP_SECONDS)
            if not settings.ready_check_enabled or settings.ready_check_hour is None:
                continue

            tz = stats_timezone()
            now = datetime.now(tz)
            slot = f"{now.date().isoformat()}:{settings.ready_check_hour}"
            if now.hour != settings.ready_check_hour or slot == last_slot:
                continue

            sent_any = False
            for link in list_links(settings.database_path):
                if ready_check_sent_today(settings.database_path, link.telegram_user_id):
                    continue
                session = load_session(bot_data, link.telegram_user_id)
                session["headset"] = False
                session["softphone_manual"] = False
                ok = await send_ready_check(
                    bot,
                    settings,
                    bot_data,
                    link,
                    intro=(
                        "🌅 <b>Shift start</b> — quick ready check\n"
                        "<i>One check per day. Use /ready anytime to run again.</i>"
                    ),
                    mark_daily=True,
                )
                if ok:
                    sent_any = True

            if sent_any:
                last_slot = slot
                logger.info(
                    "Shift ready checks sent (%s %02d:00)",
                    now.date().isoformat(),
                    settings.ready_check_hour,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ready check shift loop error")
