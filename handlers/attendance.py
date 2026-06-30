"""/attendance — show all linked agents and their call count today.
/clearlinks — admin only, unlink all agents at once.
"""
from __future__ import annotations

import html
import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from handlers.admin_access import is_bot_admin
from database import list_links, unlink_extension

logger = logging.getLogger(__name__)


def _count_all_calls_for_user(database_path: str, telegram_user_id: int) -> int:
    from database import _connect
    with _connect(database_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM completed_calls WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
    return int(row[0]) if row else 0


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

    lines = ["👥 <b>Attendance — Linked Agents</b>\n──────────────"]

    for link in links:
        name = link.display_name or link.telegram_username or str(link.telegram_user_id)
        total_calls = _count_all_calls_for_user(settings.database_path, link.telegram_user_id)
        lines.append(
            f"🟢 <b>{html.escape(name)}</b> — ext {html.escape(link.extension)} · {total_calls} total call(s)"
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
