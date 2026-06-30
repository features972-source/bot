"""/adminpayments — full payment list with starter, finisher, card, status."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from database import list_all_payments
from handlers.admin_access import is_bot_admin
from money_format import format_amount

logger = logging.getLogger(__name__)

MAX_PER_MESSAGE = 30


def _status_label(cleared) -> str:
    if cleared is True:
        return "🟩 Cleared"
    if cleared is False:
        return "🟥 Not cleared"
    return "🟧 Waiting"


def _format_record(r) -> str:
    amount = format_amount(r.amount)
    card = f"····{r.card_last4}" if r.card_last4 else "no card"
    status = _status_label(r.cleared)

    finisher_name = r.display_name or r.finisher_username or str(r.finisher_user_id)
    finisher_uname = f"@{r.finisher_username}" if r.finisher_username else ""
    finisher_label = f"{finisher_name} ({finisher_uname})" if finisher_uname and finisher_name != finisher_uname.lstrip("@") else finisher_name

    starter_name = r.starter_display_name or r.starter_username
    starter_uname = f"@{r.starter_username}" if r.starter_username else ""
    starter_label = f"{starter_name} ({starter_uname})" if starter_uname and starter_name != starter_uname.lstrip("@") else (starter_name or "")

    date = ""
    if r.created_at:
        try:
            dt = datetime.fromisoformat(r.created_at)
            date = dt.strftime("%d %b %Y")
        except Exception:
            pass

    if r.starter_user_id and r.starter_user_id == r.finisher_user_id:
        team = f"🔒 {html.escape(finisher_label)} (starter &amp; finisher)"
    elif starter_label:
        team = f"🔓 {html.escape(starter_label)} → 🔒 {html.escape(finisher_label)}"
    else:
        team = f"🔒 {html.escape(finisher_label)}"

    return (
        f"<b>ID #{r.id}</b> · <b>{html.escape(amount)}</b> · {date}\n"
        f"{team}\n"
        f"💳 {html.escape(card)} · {status}"
    )


async def adminpayments_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    settings = context.bot_data.get("settings")
    if settings is None or not is_bot_admin(settings, settings.database_path, update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    records = list_all_payments(settings.database_path)
    if not records:
        await update.message.reply_text("No payments on record.")
        return

    total_amount = sum(r.amount for r in records)
    cleared = sum(1 for r in records if r.cleared is True)
    waiting = sum(1 for r in records if r.cleared is None)
    not_cleared = sum(1 for r in records if r.cleared is False)

    header = (
        f"📋 <b>All Payments ({len(records)})</b>\n"
        f"Total: <b>{html.escape(format_amount(total_amount))}</b>\n"
        f"🟩 {cleared} cleared · 🟧 {waiting} waiting · 🟥 {not_cleared} not cleared\n"
        f"──────────────"
    )
    await update.message.reply_text(header, parse_mode="HTML")

    # Send in batches to avoid hitting message length limits
    batch: list[str] = []
    batch_len = 0
    for r in records:
        line = _format_record(r)
        if batch_len + len(line) > 3500:
            await update.message.reply_text("\n\n".join(batch), parse_mode="HTML")
            batch = []
            batch_len = 0
        batch.append(line)
        batch_len += len(line)

    if batch:
        await update.message.reply_text("\n\n".join(batch), parse_mode="HTML")


def build_admin_payments_handlers() -> list:
    return [CommandHandler("adminpayments", adminpayments_command)]
