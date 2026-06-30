"""/attendance — show all linked agents and their call count since last reset.
/resetattendance — admin only, reset all attendance counts.
/clearlinks — admin only, unlink all agents at once.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from handlers.admin_access import is_bot_admin
from database import list_links, unlink_extension, _get_bot_setting, _set_bot_setting

logger = logging.getLogger(__name__)

ATTENDANCE_RESET_KEY = "attendance_reset_at"


def seed_attendance_reset(database_path: str) -> None:
    """Called on startup — sets reset timestamp to now if never set before."""
    raw = _get_bot_setting(database_path, ATTENDANCE_RESET_KEY)
    if raw is None:
        now = datetime.now(timezone.utc)
        _set_bot_setting(database_path, ATTENDANCE_RESET_KEY, now.isoformat())
        logger.info("Attendance reset seeded at %s", now.isoformat())


def _get_reset_since(database_path: str) -> datetime | None:
    raw = _get_bot_setting(database_path, ATTENDANCE_RESET_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _count_calls_for_user(database_path: str, telegram_user_id: int, since: datetime | None) -> int:
    from database import _connect
    if since is not None:
        with _connect(database_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM completed_calls WHERE telegram_user_id = ? AND ended_at >= ?",
                (telegram_user_id, since.isoformat()),
            ).fetchone()
    else:
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

    since = _get_reset_since(settings.database_path)
    if since is None:
        # First use — seed from now so only future calls are counted
        since = datetime.now(timezone.utc)
        _set_bot_setting(settings.database_path, ATTENDANCE_RESET_KEY, since.isoformat())

    since_label = since.strftime("%d %b %Y %H:%M")
    lines = [f"👥 <b>Attendance (since {since_label} UTC)</b>\n──────────────"]

    for link in links:
        name = link.display_name or link.telegram_username or str(link.telegram_user_id)
        calls = _count_calls_for_user(settings.database_path, link.telegram_user_id, since)
        lines.append(
            f"🟢 <b>{html.escape(name)}</b> — ext {html.escape(link.extension)} · {calls} call(s)"
        )

    lines.append("──────────────")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def resetattendance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    settings = context.bot_data.get("settings")
    if settings is None or not is_bot_admin(settings, settings.database_path, update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    now = datetime.now(timezone.utc)
    _set_bot_setting(settings.database_path, ATTENDANCE_RESET_KEY, now.isoformat())
    await update.message.reply_text(
        f"✅ Attendance reset. Counts now start from {now.strftime('%d %b %Y %H:%M')} UTC."
    )


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
        CommandHandler("resetattendance", resetattendance_command),
        CommandHandler("clearlinks", clearlinks_command),
    ]
