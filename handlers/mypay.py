"""/mypay — show estimated pay for the current week based on call count.

Set PAY_PER_CALL in .env to the £ amount per completed call.
e.g. PAY_PER_CALL=2.50
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from database import count_user_calls_since, get_link_by_telegram_user_id
from handlers.stats_period import stats_timezone, current_payment_week_start


def _pay_per_call() -> float | None:
    raw = os.getenv("PAY_PER_CALL", "").strip()
    if not raw:
        return None
    try:
        val = float(raw.lstrip("£$€"))
        return val if val > 0 else None
    except ValueError:
        return None


async def mypay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    settings = context.bot_data.get("settings")
    if settings is None:
        await update.message.reply_text("❌ Bot not fully initialised yet.")
        return

    # Check user is linked
    link = get_link_by_telegram_user_id(settings.database_path, user.id)
    if link is None:
        await update.message.reply_text(
            "❌ Your Telegram account isn't linked to an extension yet.\n"
            "Ask an admin to run /link."
        )
        return

    rate = _pay_per_call()
    if rate is None:
        await update.message.reply_text(
            "⚠️ Pay rate not configured yet.\n"
            "Ask your manager to set <code>PAY_PER_CALL</code> in the bot settings.",
            parse_mode="HTML",
        )
        return

    tz = stats_timezone()
    week_start, week_label = current_payment_week_start()

    # This week
    calls_week = count_user_calls_since(
        settings.database_path,
        telegram_user_id=user.id,
        since=week_start,
    )

    # Today
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    calls_today = count_user_calls_since(
        settings.database_path,
        telegram_user_id=user.id,
        since=today_start,
    )

    pay_week = calls_week * rate
    pay_today = calls_today * rate

    name = f"@{user.username}" if user.username else user.first_name

    await update.message.reply_text(
        f"💷 <b>{name} — Pay Estimate</b>\n\n"
        f"📅 <b>This week</b> ({week_label})\n"
        f"  📞 {calls_week} calls · <b>£{pay_week:.2f}</b>\n\n"
        f"☀️ <b>Today</b>\n"
        f"  📞 {calls_today} calls · <b>£{pay_today:.2f}</b>\n\n"
        f"<i>Rate: £{rate:.2f} per call</i>",
        parse_mode="HTML",
    )


def build_mypay_handlers() -> list:
    return [CommandHandler("mypay", mypay_command)]
