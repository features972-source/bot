"""/adminpayments — full payment list with starter, finisher, card, status."""
from __future__ import annotations

import logging
from datetime import datetime

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from database import list_all_payments
from handlers.admin_access import is_bot_admin
from money_format import format_amount

logger = logging.getLogger(__name__)


async def adminpayments_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    try:
        await _adminpayments_inner(update, context)
    except Exception as exc:
        logger.exception("adminpayments_command crashed")
        try:
            await update.message.reply_text(f"Error: {exc}")
        except Exception:
            pass


async def _adminpayments_inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.bot_data.get("settings")
    if settings is None:
        await update.message.reply_text("No settings found.")
        return

    from handlers.admin_access import is_bot_admin as _is_admin
    if not _is_admin(settings, settings.database_path, update.effective_user.id):
        await update.message.reply_text("Admins only.")
        return

    all_records = list_all_payments(settings.database_path)
    records = [r for r in all_records if r.cleared is True]
    await update.message.reply_text(f"Fetched {len(records)} cleared records, building list...")

    if not records:
        await update.message.reply_text("No payments on record.")
        return

    total_amount = sum(r.amount for r in records)

    lines = [
        f"Cleared Payments ({len(records)})",
        f"Total: {format_amount(total_amount)}",
        "---",
    ]

    for r in records:
        amount = format_amount(r.amount)
        card = r.card_last4 or "?"
        if r.cleared is True:
            status = "CLR"
        elif r.cleared is False:
            status = "NO"
        else:
            status = "WAIT"

        finisher = (r.finisher_display_name or r.finisher_username or str(r.finisher_user_id)).strip()
        starter = (r.starter_display_name or r.starter_username or "").strip()

        date = ""
        if r.created_at:
            try:
                date = datetime.fromisoformat(r.created_at).strftime("%d/%m")
            except Exception:
                pass

        if r.starter_user_id and r.starter_user_id == r.finisher_user_id:
            who = finisher
        elif starter:
            who = f"{starter} > {finisher}"
        else:
            who = finisher

        lines.append(f"#{r.id} {amount} {date} [{status}] {who} card:{card}")

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 4000:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await update.message.reply_text(chunk)


def build_admin_payments_handlers() -> list:
    return [CommandHandler("adminpayments", adminpayments_command)]
