"""Payment report channel (/setnotifypayments) — one live post, edited on changes."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from config import Settings
from database import (
    clear_payment_notify_message_id,
    PaymentRecord,
    get_payment_notify_chat_id,
    get_payment_notify_message_id,
    get_payment_totals,
    list_payments_since,
    set_payment_notify_chat_id,
    set_payment_notify_message_id,
)
from handlers.admin_access import require_admin
from handlers.payments import _payment_records_for_period, _stored_user_label
from handlers.stats_period import current_payment_week_start, stats_timezone
from instance_registry import get_instance, list_instances
from money_format import format_amount
from payments_excel_export import (
    CENTRE_PAY_PERCENT,
    FINISHER_PAY_PERCENT,
    STARTER_PAY_PERCENT,
    centre_payout,
    finisher_payout,
    starter_payout,
)

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "paynotify:"
MAX_MESSAGE_LEN = 4096


def build_payment_report_handlers() -> list:
    return [
        CommandHandler("setnotifypayments", setnotifypayments_command),
        CallbackQueryHandler(
            setnotifypayments_callback, pattern=rf"^{CALLBACK_PREFIX}[a-z0-9]+$"
        ),
    ]


def _parse_local_datetime(iso_timestamp: str) -> datetime:
    text = iso_timestamp.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(stats_timezone())


def _cleared_label(cleared: bool | None) -> str:
    if cleared is None:
        return "🟠 Pending"
    if cleared:
        return "🟢 Cleared"
    return "🔴 Not cleared"


def _starter_label(record: PaymentRecord) -> str:
    if record.starter_user_id is None:
        return "—"
    return _stored_user_label(
        record.starter_username,
        record.starter_display_name,
        record.starter_user_id,
    )


def _finisher_label(record: PaymentRecord) -> str:
    return _stored_user_label(
        record.finisher_username,
        record.finisher_display_name,
        record.finisher_user_id,
    )


def _format_payment_report_row(record: PaymentRecord) -> str:
    when = _parse_local_datetime(record.created_at)
    date_str = when.strftime("%d/%m/%Y")
    time_str = when.strftime("%H:%M")
    starter = html.escape(_starter_label(record))
    finisher = html.escape(_finisher_label(record))
    amount = html.escape(format_amount(record.amount))
    status = html.escape(_cleared_label(record.cleared))
    s_pay = starter_payout(record)
    f_pay = finisher_payout(record)
    o_pay = centre_payout(record)
    card = f" · 💳 ····{html.escape(record.card_last4)}" if record.card_last4 else ""

    return (
        f"<b>#{record.id}</b> · {date_str} · {time_str}\n"
        f"💷 <b>{amount}</b> · {status}{card}\n"
        f"Starter: {starter} → Finisher: {finisher}\n"
        f"Pay starter ({STARTER_PAY_PERCENT}%): {html.escape(format_amount(s_pay))} · "
        f"Pay finisher ({FINISHER_PAY_PERCENT}%): {html.escape(format_amount(f_pay))} · "
        f"Owners ({CENTRE_PAY_PERCENT}%): {html.escape(format_amount(o_pay))}"
    )


def _week_records(settings: Settings) -> tuple:
    """Same week window and record set as /payments."""
    since, period_label = current_payment_week_start()
    all_records = list_payments_since(settings.database_path, since=since)
    total_count = len(all_records)
    shown = _payment_records_for_period(settings.database_path, since=since, limit=30)
    return since, period_label, shown, total_count, all_records


def build_payment_report_text(settings: Settings) -> str:
    since, period_label, records, total_count, all_records = _week_records(settings)
    total_count_db, total_amount = get_payment_totals(settings.database_path, since=since)
    _, pending_amount = get_payment_totals(
        settings.database_path, since=since, pending=True
    )
    _, cleared_amount = get_payment_totals(
        settings.database_path, since=since, cleared=True
    )
    _, not_cleared_amount = get_payment_totals(
        settings.database_path, since=since, cleared=False
    )

    title = (
        f"📊 <b>{html.escape(settings.bot_display_name)} — Payments</b>\n"
        f"<i>{html.escape(period_label)}</i> · auto-updated\n"
    )
    if not records:
        return (
            f"{title}\n"
            "No payments logged this week yet.\n\n"
            "<i>Same data as /payments · resets every Sunday</i>"
        )

    total_starter = sum(starter_payout(r) for r in all_records)
    total_finisher = sum(finisher_payout(r) for r in all_records)
    total_owners = sum(centre_payout(r) for r in all_records)

    footer = (
        f"\n<b>━━━ Week totals ({total_count_db} payment"
        f"{'' if total_count_db == 1 else 's'}) ━━━</b>\n"
        f"Volume: <b>{html.escape(format_amount(total_amount))}</b>\n"
        f"🟠 Pending: {html.escape(format_amount(pending_amount))} · "
        f"🟢 Cleared: {html.escape(format_amount(cleared_amount))} · "
        f"🔴 Not cleared: {html.escape(format_amount(not_cleared_amount))}\n"
        f"Pay starter ({STARTER_PAY_PERCENT}%): {html.escape(format_amount(total_starter))} · "
        f"Pay finisher ({FINISHER_PAY_PERCENT}%): {html.escape(format_amount(total_finisher))} · "
        f"Owners ({CENTRE_PAY_PERCENT}%): {html.escape(format_amount(total_owners))}\n"
        f"<i>Same list as /payments · edits when you log, update, or remove</i>"
    )

    rows: list[str] = []
    for record in records:
        row = _format_payment_report_row(record)
        candidate = title + "\n".join(rows + [row]) + footer
        if len(candidate) > MAX_MESSAGE_LEN - 80:
            hidden = total_count - len(rows)
            if hidden > 0:
                rows.append(
                    f"<i>… plus {hidden} older payment"
                    f"{'' if hidden == 1 else 's'} this week (see /payments)</i>"
                )
            break
        rows.append(row)

    return title + "\n\n".join(rows) + footer


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
        f"Uses the same data as **/payments** (resets Sunday). "
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
