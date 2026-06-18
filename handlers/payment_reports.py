"""Payment report channel (/setnotifypayments) — one live post, edited on changes."""

from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from config import Settings
from database import (
    PaymentRecord,
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
    STARTER_PAY_PERCENT,
    format_payment_sheet_updated_note,
    payment_sheet_data_rows,
    payment_sheet_totals_row,
    sorted_payment_records,
)

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "paynotify:"
MAX_MESSAGE_LEN = 4096

_ROW_DIVIDER = "────────────────────────"
_STATUS_LEGEND = "🟩 Cleared   🟧 Pending   🟥 Not cleared"


def build_payment_report_handlers() -> list:
    return [
        CommandHandler("setnotifypayments", setnotifypayments_command),
        CallbackQueryHandler(
            setnotifypayments_callback, pattern=rf"^{CALLBACK_PREFIX}[a-z0-9]+$"
        ),
    ]


def _status_banner(cleared: bool | None) -> str:
    if cleared is None:
        return "🟧 <b>PENDING</b>"
    if cleared:
        return "🟩 <b>CLEARED</b>"
    return "🟥 <b>NOT CLEARED</b>"


def _format_payment_block_html(record: PaymentRecord, row: list[str]) -> str:
    amount, date, starter, finisher, card, _cleared, pay_starter, pay_finisher, pay_centre = row
    lines = [
        _status_banner(record.cleared),
        f"💷 <b>{html.escape(amount)}</b>     📅 {html.escape(date)}",
        (
            f"👤 Starter: <b>{html.escape(starter or '—')}</b>"
            f"     ➜ Finisher: <b>{html.escape(finisher)}</b>"
        ),
    ]
    if card:
        lines.append(f"💳 Card ····{html.escape(card)}")
    lines.append(
        f"Pay starter: <b>{html.escape(pay_starter or '—')}</b>"
        f"     Pay finisher: <b>{html.escape(pay_finisher)}</b>"
        f"     Pay centre: <b>{html.escape(pay_centre)}</b>"
    )
    return "\n".join(lines)


def _format_totals_html(records: list[PaymentRecord]) -> str:
    _total_label, amount, count, *_rest, pay_starter, pay_finisher, pay_centre = (
        payment_sheet_totals_row(records)
    )
    return (
        f"\n<b>━━━━━━━━  TOTAL  ━━━━━━━━</b>\n\n"
        f"💷 <b>{html.escape(amount)}</b>     📋 {html.escape(count)}\n\n"
        f"Pay starter: <b>{html.escape(pay_starter)}</b>\n"
        f"Pay finisher: <b>{html.escape(pay_finisher)}</b>\n"
        f"Pay centre: <b>{html.escape(pay_centre)}</b>\n\n"
        f"<i>{html.escape(format_payment_sheet_updated_note())}</i>"
    )


def _format_report_body(
    records: list[PaymentRecord],
    *,
    hidden_count: int = 0,
) -> str:
    data_rows = payment_sheet_data_rows(records)
    blocks = [
        _format_payment_block_html(record, row)
        for record, row in zip(records, data_rows)
    ]
    body = f"\n\n{_ROW_DIVIDER}\n\n".join(blocks)
    body += _format_totals_html(records)
    if hidden_count > 0:
        body += (
            f"\n\n<i>… plus {hidden_count} older payment"
            f"{'' if hidden_count == 1 else 's'} this week (see /payments)</i>"
        )
    return body


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
        f"<i>{html.escape(period_label)}</i>\n\n"
        f"{_STATUS_LEGEND}"
    )
    if not all_records:
        return (
            f"{title}\n\n"
            "No payments logged this week yet.\n\n"
            f"<i>Same data as /payments · resets every Sunday</i>"
        )

    shown = list(all_records)
    hidden = 0
    while shown:
        body = f"{title}{_format_report_body(shown, hidden_count=hidden)}"
        if len(body) <= MAX_MESSAGE_LEN - 16:
            return body
        if len(shown) == 1:
            break
        shown.pop()
        hidden += 1

    return f"{title}{_format_report_body(shown, hidden_count=hidden)}"


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
        f"Same data as the Excel sheet · /payments (resets Sunday). "
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
