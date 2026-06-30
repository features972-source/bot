"""/paybuttons — post this week's payments as interactive Paid/Not Paid buttons."""
from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from database import (
    get_payment_by_id,
    list_all_payments,
    update_payment_cleared,
)
from handlers.admin_access import is_bot_admin
from money_format import format_amount
from payments_excel_export import sorted_payment_records
from handlers.payment_reports import schedule_payment_report_refresh

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "paybtn:"


def _payment_row_text(record) -> str:
    finisher = record.display_name or record.finisher_username or str(record.finisher_user_id)
    amount = format_amount(record.amount)
    card = f" ····{record.card_last4}" if record.card_last4 else ""
    status = "🟩 Cleared" if record.cleared == "cleared" else ("🟥 Not cleared" if record.cleared == "not_cleared" else "🟧 Waiting")
    return f"<b>#{record.id}</b> {html.escape(amount)} — {html.escape(finisher)}{html.escape(card)} · {status}"


def _keyboard_for(record) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Paid", callback_data=f"{CALLBACK_PREFIX}paid:{record.id}"),
        InlineKeyboardButton("❌ Not Cleared", callback_data=f"{CALLBACK_PREFIX}notcleared:{record.id}"),
        InlineKeyboardButton("⏳ Reset", callback_data=f"{CALLBACK_PREFIX}reset:{record.id}"),
    ]])


async def paybuttons_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    settings = context.bot_data.get("settings")
    if settings is None or not is_bot_admin(settings, settings.database_path, update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    records = sorted_payment_records(list_all_payments(settings.database_path))

    if not records:
        await update.message.reply_text("No payments on record. Use /clearpayments to reset.")
        return

    await update.message.reply_text(
        "💰 <b>All payments — tap to mark paid/not cleared</b>",
        parse_mode="HTML",
    )

    for record in records:
        await update.message.reply_text(
            _payment_row_text(record),
            parse_mode="HTML",
            reply_markup=_keyboard_for(record),
        )


async def paybtn_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return

    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "paybtn":
        return

    action = parts[1]
    try:
        payment_id = int(parts[2])
    except ValueError:
        return

    settings = context.bot_data.get("settings")
    if settings is None:
        return

    if action == "paid":
        update_payment_cleared(settings.database_path, payment_id, cleared=True)
    elif action == "notcleared":
        update_payment_cleared(settings.database_path, payment_id, cleared=False)
    elif action == "reset":
        from database import _connect
        with _connect(settings.database_path) as conn:
            conn.execute("UPDATE payment_outs SET cleared = 'pending' WHERE id = ?", (payment_id,))
            conn.commit()

    # Refresh the live payment table image
    schedule_payment_report_refresh(query.bot, settings)

    # Update the button message with new status
    record = get_payment_by_id(settings.database_path, payment_id)
    if record and query.message:
        try:
            await query.message.edit_text(
                _payment_row_text(record),
                parse_mode="HTML",
                reply_markup=_keyboard_for(record),
            )
        except Exception:
            pass


def build_pay_buttons_handlers() -> list:
    return [
        CommandHandler("paybuttons", paybuttons_command),
        CallbackQueryHandler(paybtn_callback, pattern=rf"^{CALLBACK_PREFIX}"),
    ]
