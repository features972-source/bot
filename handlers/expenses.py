"""Log expenses in the expenses group (e.g. £132 blast)."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters

from config import Settings
from database import get_expense_notify_chat_id, record_expense
from money_format import format_amount, parse_expense_line

logger = logging.getLogger(__name__)


def build_expense_message_handlers() -> list:
    return [
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            expense_message,
            block=False,
        ),
    ]


def build_expense_command_handlers() -> list:
    return []


def _expense_chat_allowed(settings: Settings, chat) -> bool:
    if chat is None:
        return False
    expense_chat_id = get_expense_notify_chat_id(settings.database_path)
    if expense_chat_id is None:
        return False
    return chat.id == expense_chat_id


def _display_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    return name or "Unknown"


async def expense_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or not message.text:
        return
    if not _expense_chat_allowed(settings, chat):
        return

    parsed = parse_expense_line(message.text)
    if parsed is None:
        return

    amount, reason, raw_text = parsed
    expense_id = record_expense(
        settings.database_path,
        telegram_user_id=user.id,
        telegram_username=user.username,
        display_name=_display_name(user),
        amount=amount,
        raw_text=raw_text,
        reason=reason,
        chat_id=chat.id,
        telegram_message_id=message.message_id,
    )
    if expense_id is None:
        return

    logger.info(
        "expense logged chat=%s user=%s amount=%s reason=%r id=%s",
        chat.id,
        user.id,
        amount,
        reason,
        expense_id,
    )

    from handlers.expense_reports import schedule_expense_report_refresh

    schedule_expense_report_refresh(context.bot, settings)

    await message.reply_text(
        f"✅ Logged **{format_amount(amount)}** · {reason} (#{expense_id})",
        parse_mode="Markdown",
    )
