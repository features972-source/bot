"""/payout — calculate who you owe. /pay @user amount — record a payment."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from database import list_all_payments, _connect
from handlers.admin_access import is_bot_admin
from money_format import format_amount

logger = logging.getLogger(__name__)

STARTER_PCT = 0.05
FINISHER_PCT = 0.15


def _ensure_pay_log_table(database_path: str) -> None:
    with _connect(database_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_pay_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paid_at TEXT NOT NULL,
                username TEXT,
                display_name TEXT,
                amount REAL NOT NULL,
                paid_by_user_id INTEGER,
                note TEXT
            )
        """)
        conn.commit()


def _record_pay(database_path: str, *, username: str | None, display_name: str | None,
                amount: float, paid_by_user_id: int, note: str = "") -> None:
    _ensure_pay_log_table(database_path)
    now = datetime.now(timezone.utc).isoformat()
    with _connect(database_path) as conn:
        conn.execute(
            "INSERT INTO agent_pay_log (paid_at, username, display_name, amount, paid_by_user_id, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now, username, display_name, amount, paid_by_user_id, note),
        )
        conn.commit()


def _list_pay_log(database_path: str) -> list[dict]:
    _ensure_pay_log_table(database_path)
    with _connect(database_path) as conn:
        rows = conn.execute(
            "SELECT paid_at, username, display_name, amount, note FROM agent_pay_log ORDER BY paid_at DESC"
        ).fetchall()
    return [
        {"paid_at": r[0], "username": r[1], "display_name": r[2], "amount": r[3], "note": r[4]}
        for r in rows
    ]


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

    # Build paid totals per username from pay log
    paid_by_username: dict[str, float] = {}
    for entry in _list_pay_log(settings.database_path):
        uname = (entry["username"] or "").lower().lstrip("@")
        if uname:
            paid_by_username[uname] = paid_by_username.get(uname, 0.0) + entry["amount"]

    lines = [
        "💷 <b>Payout Summary</b>\n"
        f"Starter: <b>{int(STARTER_PCT*100)}%</b> · Finisher: <b>{int(FINISHER_PCT*100)}%</b>\n"
        "──────────────"
    ]

    grand_total = 0.0
    for user_id, data in sorted(agents.items(), key=lambda x: -(x[1]["starter_owed"] + x[1]["finisher_owed"])):
        gross = data["starter_owed"] + data["finisher_owed"]
        label_raw = data["label"]
        # Match paid log by username
        uname_key = ""
        if "@" in label_raw:
            uname_key = label_raw.split("(")[-1].rstrip(")").lstrip("@").lower()
        already_paid = paid_by_username.get(uname_key, 0.0)
        remaining = max(0.0, gross - already_paid)
        grand_total += remaining
        label = html.escape(label_raw)
        parts = []
        if data["starter_owed"] > 0:
            parts.append(f"Starter: {html.escape(format_amount(data['starter_owed']))}")
        if data["finisher_owed"] > 0:
            parts.append(f"Finisher: {html.escape(format_amount(data['finisher_owed']))}")
        breakdown = " · ".join(parts)
        paid_line = f"\n   Already paid: {html.escape(format_amount(already_paid))}" if already_paid > 0 else ""
        status = "✅" if remaining == 0 else "💷"
        lines.append(
            f"{status} <b>{label}</b>\n"
            f"   Gross owed: {html.escape(format_amount(gross))}{paid_line}\n"
            f"   <b>Still owed: {html.escape(format_amount(remaining))}</b>\n"
            f"   {breakdown}"
        )

    lines.append(f"──────────────")
    lines.append(f"<b>Total still owed: {html.escape(format_amount(grand_total))}</b>")
    lines.append(f"<i>/pay @user amount to record a payment · /paylog for history</i>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /pay @username amount [note]"""
    if not update.effective_user or not update.message:
        return

    settings = context.bot_data.get("settings")
    if settings is None or not is_bot_admin(settings, settings.database_path, update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /pay @username amount\nExample: /pay @frankgside 320"
        )
        return

    raw_user = args[0].lstrip("@")
    try:
        amount = float(args[1].replace("\u00a3", "").replace(",", ""))
    except ValueError:
        await update.message.reply_text("\u274c Invalid amount. Example: /pay @frankgside 320")
        return

    if amount <= 0:
        await update.message.reply_text("\u274c Amount must be greater than 0.")
        return

    note = " ".join(args[2:]) if len(args) > 2 else ""

    _record_pay(
        settings.database_path,
        username=raw_user,
        display_name=None,
        amount=amount,
        paid_by_user_id=update.effective_user.id,
        note=note,
    )

    note_line = f"\n\U0001f4dd Note: {html.escape(note)}" if note else ""
    await update.message.reply_text(
        f"\u2705 <b>Payment recorded</b>\n\n"
        f"\U0001f464 @{html.escape(raw_user)}\n"
        f"\U0001f4b7 <b>{html.escape(format_amount(amount))}</b> paid"
        f"{note_line}\n\n"
        f"<i>Use /payout to see remaining balances.</i>",
        parse_mode="HTML",
    )


async def paylog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent pay history."""
    if not update.effective_user or not update.message:
        return

    settings = context.bot_data.get("settings")
    if settings is None or not is_bot_admin(settings, settings.database_path, update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return

    logs = _list_pay_log(settings.database_path)
    if not logs:
        await update.message.reply_text("No payments recorded yet. Use /pay @user amount.")
        return

    lines = ["\U0001f4cb <b>Pay Log</b>\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"]
    for entry in logs[:20]:
        name = entry["display_name"] or f"@{entry['username']}" if entry["username"] else "Unknown"
        dt = entry["paid_at"][:10]
        note = f" · {html.escape(entry['note'])}" if entry.get("note") else ""
        lines.append(f"\u2705 <b>{html.escape(format_amount(entry['amount']))}</b> \u2192 {html.escape(name)} · {dt}{note}")
    lines.append("\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def build_payout_handlers() -> list:
    return [
        CommandHandler("payout", payout_command),
        CommandHandler("pay", pay_command),
        CommandHandler("paylog", paylog_command),
    ]
