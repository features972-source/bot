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
    card = f"····{r.card_last4}" if r.card_last4 else "—"
    status = _status_label(r.cleared)

    finisher_name = (r.display_name or r.finisher_username or str(r.finisher_user_id)).strip()
    finisher_uname = r.finisher_username or ""
    finisher_label = f"{finisher_name} (@{finisher_uname.lstrip('@')})" if finisher_uname and finisher_name.lower() != finisher_uname.lower().lstrip("@") else finisher_name

    starter_name = (r.starter_display_name or r.starter_username or "").strip()
    starter_uname = r.starter_username or ""
    starter_label = f"{starter_name} (@{starter_uname.lstrip('@')})" if starter_uname and starter_name.lower() != starter_uname.lower().lstrip("@") else starter_name

    date = ""
    if r.created_at:
        try:
            dt = datetime.fromisoformat(r.created_at)
            date = dt.strftime("%d %b")
        except Exception:
            pass

    if r.starter_user_id and r.starter_user_id == r.finisher_user_id:
        team_line = f"\U0001f464 {html.escape(finisher_label)} (starter &amp; finisher)"
    elif starter_label:
        team_line = f"\U0001f513 {html.escape(starter_label)}  \u2192  \U0001f512 {html.escape(finisher_label)}"
    else:
        team_line = f"\U0001f464 {html.escape(finisher_label)}"

    return (
        f"<b>#{r.id}</b>  <b>{html.escape(amount)}</b>  {date}  {status}\n"
        f"{team_line}\n"
        f"💳 {html.escape(card)}"
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
        f"🟩 {cleared} cleared · 🟧 {waiting} waiting · 🟥 {not_cleared} not cleared"
    )
    await update.message.reply_text(header, parse_mode="HTML")

    # Build one message per record, send individually to avoid length limits
    for r in records:
        try:
            await update.message.reply_text(_format_record(r), parse_mode="HTML")
        except Exception:
            logger.exception("Failed to send adminpayments record #%s", r.id)


def build_admin_payments_handlers() -> list:
    return [CommandHandler("adminpayments", adminpayments_command)]
