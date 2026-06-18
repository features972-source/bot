"""Payment report channel (/setnotifypayments) — one live post, edited on changes."""

from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from config import Settings
from database import (
    clear_payment_notify_message_id,
    get_payment_notify_chat_id,
    get_payment_notify_message_id,
    list_payments_since,
    set_payment_notify_chat_id,
    set_payment_notify_message_id,
)
from handlers.admin_access import require_admin
from handlers.stats_period import current_payment_week_start
from instance_registry import get_instance, list_instances
from payments_excel_export import (
    CENTRE_PAY_PERCENT,
    FINISHER_PAY_PERCENT,
    HEADERS,
    STARTER_PAY_PERCENT,
    format_payment_sheet_updated_note,
    payment_sheet_data_rows,
    payment_sheet_totals_row,
    sorted_payment_records,
)

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "paynotify:"
MAX_MESSAGE_LEN = 4096

# Fixed-width columns matching the Excel export layout.
_COL_WIDTHS = (11, 11, 12, 12, 5, 8, 14, 14, 14)


def build_payment_report_handlers() -> list:
    return [
        CommandHandler("setnotifypayments", setnotifypayments_command),
        CallbackQueryHandler(
            setnotifypayments_callback, pattern=rf"^{CALLBACK_PREFIX}[a-z0-9]+$"
        ),
    ]


def _fit_cell(value: str, width: int) -> str:
    text = value or ""
    if len(text) <= width:
        return text.ljust(width)
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _format_sheet_row(cells: list[str]) -> str:
    padded = [
        _fit_cell(cells[i] if i < len(cells) else "", _COL_WIDTHS[i])
        for i in range(len(_COL_WIDTHS))
    ]
    return "".join(padded).rstrip()


def _format_sheet_table(
    records: list,
    *,
    hidden_count: int = 0,
) -> str:
    header = _format_sheet_row(list(HEADERS))
    divider = "─" * min(len(header), 96)
    lines = [header, divider]
    lines.extend(_format_sheet_row(row) for row in payment_sheet_data_rows(records))
    lines.append(divider)
    lines.append(_format_sheet_row(payment_sheet_totals_row(records)))
    if hidden_count > 0:
        lines.append(
            f"… plus {hidden_count} older payment"
            f"{'' if hidden_count == 1 else 's'} this week (see /payments)"
        )
    footer = format_payment_sheet_updated_note()
    lines.append(f"{footer:>{len(header)}}")
    return "\n".join(lines)


def _week_records(settings: Settings) -> tuple:
    """Same week window and record set as /payments (newest first, like Excel)."""
    since, period_label = current_payment_week_start()
    all_records = list_payments_since(settings.database_path, since=since)
    sorted_all = sorted_payment_records(all_records)
    return since, period_label, sorted_all


def build_payment_report_text(settings: Settings) -> str:
    _, period_label, all_records = _week_records(settings)

    title = (
        f"📊 <b>{html.escape(settings.bot_display_name)}</b>\n"
        f"<i>{html.escape(period_label)}</i>"
    )
    if not all_records:
        return (
            f"{title}\n\n"
            "<pre>No payments logged this week yet.</pre>\n\n"
            f"<i>Same data as /payments · resets every Sunday</i>"
        )

    shown = list(all_records)
    hidden = 0
    while shown:
        table = _format_sheet_table(shown, hidden_count=hidden)
        body = f"{title}\n\n<pre>{html.escape(table)}</pre>"
        if len(body) <= MAX_MESSAGE_LEN - 16:
            return body
        if len(shown) == 1:
            break
        shown.pop()
        hidden += 1

    table = _format_sheet_table(shown, hidden_count=hidden)
    return f"{title}\n\n<pre>{html.escape(table)}</pre>"


async def refresh_payment_report(bot, settings: Settings) -> None:
    """Edit the live payment report post, or create it if missing."""
    chat_id = get_payment_notify_chat_id(settings.database_path)
    if chat_id is None:
        return

    text = build_payment_report_text(settings)
    message_id = get_payment_notify_message_id(settings.database_path)

    try:
        if message_id is not None:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.warning(
            "Could not edit payment report msg %s: %s — posting new",
            message_id,
            exc,
        )
    except Exception:
        logger.exception("Failed to edit payment report for %s", settings.bot_display_name)

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        set_payment_notify_message_id(settings.database_path, sent.message_id)
    except Exception:
        logger.exception(
            "Failed to post payment report for %s to chat %s",
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
    for instance_id, settings in instances:
        label = settings.bot_display_name
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


async def setnotifypayments_command(
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
            "Run **/setnotifypayments** inside the group where you want the live payment list.",
            parse_mode="Markdown",
        )
        return

    instance_id = context.bot_data.get("instance_id", "q1")
    await message.reply_text(
        "💸 **Live payment list**\n\n"
        "Pick **Q1** or **Q2**. One message in this group will stay updated "
        "whenever payments are logged, edited, cleared, or removed.\n\n"
        f"Same layout as the Excel sheet · /payments data (resets Sunday). "
        f"Payouts: starter {STARTER_PAY_PERCENT}%, finisher {FINISHER_PAY_PERCENT}%, "
        f"owners {CENTRE_PAY_PERCENT}%.",
        parse_mode="Markdown",
        reply_markup=_instance_picker_keyboard(instance_id),
    )


async def setnotifypayments_callback(
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
    set_payment_notify_chat_id(target_settings.database_path, chat.id)
    clear_payment_notify_message_id(target_settings.database_path)

    if query.message:
        await query.edit_message_text(
            f"✅ **{target_settings.bot_display_name}** — live payment list enabled here.\n\n"
            f"Chat id: `{chat.id}`\n"
            "The list below updates automatically when payments change.",
            parse_mode="Markdown",
        )

    await refresh_payment_report(context.bot, target_settings)
