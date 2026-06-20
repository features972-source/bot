"""Live expense report channel (/setexpenses) — one table, edited on changes."""

from __future__ import annotations

import asyncio
import html
import logging
from collections import defaultdict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from config import Settings
from database import (
    clear_expense_report_message_id,
    get_expense_report_chat_id,
    get_expense_report_message_id,
    list_expenses_since,
    set_expense_report_chat_id,
    set_expense_report_message_id,
    get_expense_totals,
)
from handlers.admin_access import require_admin
from handlers.expense_table import LIVE_EXPENSE_ROW_LIMIT, format_expense_subtitle
from handlers.expense_table_image import (
    expense_report_title,
    expense_table_input_file,
    render_expenses_table_png,
)
from handlers.stats_period import current_payment_week_start
from instance_registry import get_instance, list_instances

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "expnotify:"
_REFRESH_LOCKS: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_PENDING_REFRESH: dict[str, tuple] = {}
_DEBOUNCE_HANDLES: dict[str, asyncio.TimerHandle] = {}
_REFRESH_DEBOUNCE_SEC = 0.35


def build_expense_report_handlers() -> list:
    return [
        CommandHandler("setexpenses", setexpenses_command),
        CallbackQueryHandler(
            setexpenses_callback, pattern=rf"^{CALLBACK_PREFIX}[a-z0-9]+$"
        ),
    ]


def _sorted_expense_records(records):
    return sorted(records, key=lambda row: (row.created_at, row.id))


def _photo_file(image_bytes: bytes):
    return expense_table_input_file(image_bytes)


async def _delete_notify_message(bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest:
        pass
    except Exception:
        logger.warning(
            "Could not delete expense report message %s in chat %s",
            message_id,
            chat_id,
        )


async def _try_edit_photo(
    bot, chat_id: int, message_id: int, image_bytes: bytes
) -> bool:
    try:
        await bot.edit_message_media(
            chat_id=chat_id,
            message_id=message_id,
            media=InputMediaPhoto(media=_photo_file(image_bytes)),
        )
        return True
    except BadRequest as exc:
        err = str(exc).lower()
        if "message is not modified" in err:
            return True
        logger.debug("edit_message_media failed for expense msg %s: %s", message_id, exc)
        return False
    except Exception:
        logger.exception("edit_message_media error for expense msg %s", message_id)
        return False


def _week_records(settings: Settings) -> tuple:
    since, period_label = current_payment_week_start()
    all_records = list_expenses_since(settings.database_path, since=since)
    return since, period_label, _sorted_expense_records(all_records)


def build_expense_report_image(settings: Settings) -> bytes | None:
    since, period_label, all_records = _week_records(settings)
    if not all_records:
        return None

    total_count, total_amount = get_expense_totals(settings.database_path, since=since)
    shown = all_records[-LIVE_EXPENSE_ROW_LIMIT:]
    return render_expenses_table_png(
        shown,
        database_path=settings.database_path,
        total_amount=total_amount,
        total_count=total_count,
        title=expense_report_title(settings.bot_display_name),
        subtitle=format_expense_subtitle(period_label),
        live=True,
    )


def build_expense_report_empty_text(settings: Settings) -> str:
    _, period_label, _ = _week_records(settings)
    title = expense_report_title(settings.bot_display_name)
    return (
        f"<b>{html.escape(title)}</b>\n"
        f"<i>{html.escape(format_expense_subtitle(period_label))}</i>\n\n"
        "No expenses logged this week yet.\n\n"
        "Post lines like <code>£132 blast</code> or <code>/expense</code> — step-by-step.\n"
        "<i>New week every Sunday.</i>"
    )


def schedule_expense_report_refresh(bot, settings: Settings) -> None:
    key = settings.database_path
    _PENDING_REFRESH[key] = (bot, settings)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    handle = _DEBOUNCE_HANDLES.get(key)
    if handle is not None:
        handle.cancel()
    _DEBOUNCE_HANDLES[key] = loop.call_later(
        _REFRESH_DEBOUNCE_SEC,
        lambda k=key: asyncio.create_task(_run_pending_refresh(k)),
    )


async def _run_pending_refresh(key: str) -> None:
    _DEBOUNCE_HANDLES.pop(key, None)
    bot, settings = _PENDING_REFRESH.pop(key, (None, None))
    if bot is None or settings is None:
        return
    await refresh_expense_report(bot, settings)


async def refresh_expense_report(bot, settings: Settings) -> None:
    chat_id = get_expense_report_chat_id(settings.database_path)
    if chat_id is None:
        return

    lock = _REFRESH_LOCKS[settings.database_path]
    async with lock:
        message_id = get_expense_report_message_id(settings.database_path)
        image_bytes = await asyncio.to_thread(build_expense_report_image, settings)

        if image_bytes is None:
            text = build_expense_report_empty_text(settings)
            if message_id is not None:
                await _delete_notify_message(bot, chat_id, message_id)
                message_id = None
            try:
                sent = await bot.send_message(
                    chat_id=chat_id, text=text, parse_mode="HTML"
                )
                set_expense_report_message_id(settings.database_path, sent.message_id)
            except Exception:
                logger.exception("Failed to post empty expense report")
            return

        if message_id is not None:
            await _delete_notify_message(bot, chat_id, message_id)

        try:
            sent = await bot.send_photo(
                chat_id=chat_id, photo=_photo_file(image_bytes)
            )
            set_expense_report_message_id(settings.database_path, sent.message_id)
        except Exception:
            logger.exception(
                "Failed to post expense report image for %s to chat %s",
                settings.bot_display_name,
                chat_id,
            )


def _instance_picker_keyboard(current_instance_id: str) -> InlineKeyboardMarkup:
    instances = list_instances()
    if not instances:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Use this bot",
                        callback_data=f"{CALLBACK_PREFIX}{current_instance_id or 'q1'}",
                    )
                ]
            ]
        )
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for instance_id, inst_settings in instances:
        label = inst_settings.bot_display_name
        if len(label) > 28:
            label = f"{label[:25]}…"
        row.append(
            InlineKeyboardButton(label, callback_data=f"{CALLBACK_PREFIX}{instance_id}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def setexpenses_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(
            "Run **/setexpenses** in the group where you want the live expense table.",
            parse_mode="Markdown",
        )
        return

    instance_id = context.bot_data.get("instance_id", "q1")
    await message.reply_text(
        "🧾 **Live expense table**\n\n"
        "Choose **Q1** or **Q2**. The bot posts **one table** in this group and "
        "**reposts it at the bottom** whenever an expense is logged.\n\n"
        "Run **/setnotifyexpenses** first in the group where people log expenses.\n"
        "Post expenses like: `£132 blast` or use **/expense** (step-by-step)\n"
        "New week every **Sunday**.",
        parse_mode="Markdown",
        reply_markup=_instance_picker_keyboard(instance_id),
    )


async def setexpenses_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    settings_ctx: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings_ctx):
        await query.answer("Admin only.", show_alert=True)
        return

    chat = query.message.chat if query.message else None
    if not chat or chat.type not in ("group", "supergroup"):
        await query.answer("Use this in a group.", show_alert=True)
        return

    instance_id = query.data.split(":", 1)[1]
    target_settings = get_instance(instance_id) or settings_ctx

    await query.answer()
    old_chat_id = get_expense_report_chat_id(target_settings.database_path)
    set_expense_report_chat_id(target_settings.database_path, chat.id)
    if old_chat_id is None or old_chat_id != chat.id:
        clear_expense_report_message_id(target_settings.database_path)

    if query.message:
        await query.edit_message_text(
            f"✅ **{expense_report_title(target_settings.bot_display_name)}** is live in this group.\n\n"
            f"Chat id: `{chat.id}`\n\n"
            "The table below updates when expenses are logged "
            "(in the group set with **/setnotifyexpenses**).",
            parse_mode="Markdown",
        )

    schedule_expense_report_refresh(context.bot, target_settings)
    from handlers.admin_access import sync_bot_command_menu

    await sync_bot_command_menu(context.bot, target_settings)
