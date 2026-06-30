"""/payout — calculate who you owe and how much based on 5% starter / 15% finisher."""
from __future__ import annotations

import html
import logging
from collections import defaultdict

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from database import list_all_payments
from handlers.admin_access import is_bot_admin
from money_format import format_amount

logger = logging.getLogger(__name__)

STARTER_PCT = 0.05
FINISHER_PCT = 0.15


def _agent_key(user_id: int, username: str | None, display_name: str | None) -> str:
    name = display_name.strip() if display_name else ""
    uname = f"@{username.lstrip('@')}" if username else ""
    if name and uname:
        return f"{name} ({uname})"
    return name or uname or str(user_id)


async def payout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    # user_id -> {label, starter_owed, finisher_owed}
    agents: dict[int, dict] = {}

    for r in records:
        amount = r.amount

        # Starter cut
        if r.starter_user_id is not None:
            sid = r.starter_user_id
            if sid not in agents:
                agents[sid] = {
                    "label": _agent_key(sid, r.starter_username, r.starter_display_name),
                    "starter_owed": 0.0,
                    "finisher_owed": 0.0,
                }
            agents[sid]["starter_owed"] += amount * STARTER_PCT

        # Finisher cut
        fid = r.finisher_user_id
        if fid not in agents:
            agents[fid] = {
                "label": _agent_key(fid, r.finisher_username, r.finisher_display_name),
                "starter_owed": 0.0,
                "finisher_owed": 0.0,
            }
        # If starter == finisher, they get both cuts
        if r.starter_user_id == fid:
            agents[fid]["finisher_owed"] += amount * FINISHER_PCT
        else:
            agents[fid]["finisher_owed"] += amount * FINISHER_PCT

    lines = [
        "💷 <b>Payout Summary</b>\n"
        f"Starter: <b>{int(STARTER_PCT*100)}%</b> · Finisher: <b>{int(FINISHER_PCT*100)}%</b>\n"
        "──────────────"
    ]

    grand_total = 0.0
    for user_id, data in sorted(agents.items(), key=lambda x: -(x[1]["starter_owed"] + x[1]["finisher_owed"])):
        total = data["starter_owed"] + data["finisher_owed"]
        grand_total += total
        label = html.escape(data["label"])
        parts = []
        if data["starter_owed"] > 0:
            parts.append(f"Starter: {html.escape(format_amount(data['starter_owed']))}")
        if data["finisher_owed"] > 0:
            parts.append(f"Finisher: {html.escape(format_amount(data['finisher_owed']))}")
        breakdown = " · ".join(parts)
        lines.append(
            f"👤 <b>{label}</b>\n"
            f"   Total owed: <b>{html.escape(format_amount(total))}</b>\n"
            f"   {breakdown}"
        )

    lines.append(f"──────────────")
    lines.append(f"<b>Grand total owed: {html.escape(format_amount(grand_total))}</b>")
    lines.append(f"<i>Use /clearpayments once everyone is paid.</i>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def build_payout_handlers() -> list:
    return [CommandHandler("payout", payout_command)]
