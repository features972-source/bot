"""/attendance — show all linked agents and their call count today.
/clearlinks — admin only, unlink all agents at once.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from handlers.admin_access import is_bot_admin
from database import (
    list_links,
    unlink_extension,
    count_user_calls_since,
)

logger = logging.getLogger(__name__)


def _today_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def attendance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    settings = context.bot_data.get("settings")
    if settings is None:
        return

    links = list_links(settings.database_path)
    if not links:
        await update.message.reply_text("No agents currently linked.")
        return

    today = _today_start()
    lines = ["👥 <b>Attendance — Linked Agents</b>\n──────────────"]

    for link in links:
        name = link.display_name or link.telegram_username or str(link.telegram_user_id)
        calls_today = count_user_calls_since(
            settings.database_path,
            telegram_user_id=link.telegram_user_id,
            since=today,
        )
        status = "🟢" if calls_today > 0 else "⚪"
        lines.append(
            f"{status} <b>{html.escape(name)}</b> — ext {html.escape(link.extension)} · {calls_today} call(s) today"
        )

    lines.append("──────────────")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def clearlinks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    settings = context.bot_data.get("settings")
    if settings is None or not is_bot_admin(settings, settings.database_path, update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    links = list_links(settings.database_path)
    if not links:
        await update.message.reply_text("No linked agents to clear.")
        return

    count = 0
    for link in links:
        if unlink_extension(settings.database_path, link.extension):
            count += 1

    await update.message.reply_text(f"✅ Cleared {count} linked agent(s).")


def build_attendance_handlers() -> list:
    return [
        CommandHandler("attendance", attendance_command),
        CommandHandler("clearlinks", clearlinks_command),
    ]
