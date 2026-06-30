"""/payout — interactive buttons to mark agents as paid/unpaid."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from database import list_all_payments, _connect
from handlers.admin_access import is_bot_admin
from money_format import format_amount

logger = logging.getLogger(__name__)

STARTER_PCT = 0.05
FINISHER_PCT = 0.15
CB = "payout:"


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


def _set_paid(database_path: str, username: str, amount: float, paid_by: int) -> None:
    _ensure_pay_log_table(database_path)
    # Remove any existing entry for this username first (clean slate)
    with _connect(database_path) as conn:
        conn.execute("DELETE FROM agent_pay_log WHERE LOWER(username) = LOWER(?)", (username,))
        conn.execute(
            "INSERT INTO agent_pay_log (paid_at, username, display_name, amount, paid_by_user_id, note) "
            "VALUES (?, ?, NULL, ?, ?, '')",
            (datetime.now(timezone.utc).isoformat(), username, amount, paid_by),
        )
        conn.commit()


def _clear_paid(database_path: str, username: str) -> None:
    _ensure_pay_log_table(database_path)
    with _connect(database_path) as conn:
        conn.execute("DELETE FROM agent_pay_log WHERE LOWER(username) = LOWER(?)", (username,))
        conn.commit()


def _paid_amounts(database_path: str) -> dict[str, float]:
    _ensure_pay_log_table(database_path)
    with _connect(database_path) as conn:
        rows = conn.execute(
            "SELECT username, SUM(amount) FROM agent_pay_log GROUP BY LOWER(username)"
        ).fetchall()
    return {(r[0] or "").lower(): r[1] for r in rows}


def _build_agents(records) -> dict[str, dict]:
    """Returns {username_key: {label, uname, owed}}"""
    agents: dict[str, dict] = {}
    for r in records:
        amount = r.amount
        # Starter
        if r.starter_user_id is not None:
            ukey = (r.starter_username or str(r.starter_user_id)).lower().lstrip("@")
            if ukey not in agents:
                name = (r.starter_display_name or "").strip()
                uname = r.starter_username or ""
                label = f"{name} (@{uname.lstrip('@')})" if name and uname else name or f"@{uname}" or ukey
                agents[ukey] = {"label": label, "uname": uname.lstrip("@"), "owed": 0.0}
            agents[ukey]["owed"] += amount * STARTER_PCT
        # Finisher
        ukey = (r.finisher_username or str(r.finisher_user_id)).lower().lstrip("@")
        if ukey not in agents:
            name = (r.finisher_display_name or r.display_name or "").strip()
            uname = r.finisher_username or ""
            label = f"{name} (@{uname.lstrip('@')})" if name and uname else name or f"@{uname}" or ukey
            agents[ukey] = {"label": label, "uname": uname.lstrip("@"), "owed": 0.0}
        agents[ukey]["owed"] += amount * FINISHER_PCT
    return agents


def _agent_row_text(label: str, owed: float, paid: float) -> str:
    remaining = max(0.0, owed - paid)
    if remaining <= 0:
        status = "✅ PAID"
    else:
        status = f"💷 Owed: <b>{html.escape(format_amount(remaining))}</b>"
    return f"👤 <b>{html.escape(label)}</b> — {status}"


def _agent_keyboard(uname: str, owed: float, paid: float) -> InlineKeyboardMarkup:
    remaining = max(0.0, owed - paid)
    if remaining <= 0:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ Mark Unpaid", callback_data=f"{CB}unpaid:{uname}"),
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Mark Paid ({format_amount(remaining)})", callback_data=f"{CB}paid:{uname}:{owed:.2f}"),
        InlineKeyboardButton("↩️ Undo", callback_data=f"{CB}unpaid:{uname}"),
    ]])


async def payout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    settings = context.bot_data.get("settings")
    if settings is None or not is_bot_admin(settings, settings.database_path, update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    all_records = list_all_payments(settings.database_path)
    records = [r for r in all_records if r.cleared is True]

    if not records:
        await update.message.reply_text(
            "No cleared payments yet.\n\n"
            "<i>Mark payments as cleared via /paybuttons or /setcleared, then run /payout again.</i>",
            parse_mode="HTML",
        )
        return

    agents = _build_agents(records)
    paid_map = _paid_amounts(settings.database_path)

    total_owed = sum(d["owed"] for d in agents.values())
    total_paid = sum(paid_map.get(k, 0.0) for k in agents)
    remaining_total = max(0.0, total_owed - total_paid)

    lines = [
        f"💷 <b>Payout — {int(STARTER_PCT*100)}% starter · {int(FINISHER_PCT*100)}% finisher</b>",
        f"Total still owed: <b>{html.escape(format_amount(remaining_total))}</b>",
        "──────────────",
    ]

    for ukey, data in sorted(agents.items(), key=lambda x: -x[1]["owed"]):
        paid = paid_map.get(ukey, 0.0)
        remaining = max(0.0, data["owed"] - paid)
        status = "✅ PAID" if remaining <= 0 else f"💷 <b>{html.escape(format_amount(remaining))}</b> owed"
        lines.append(f"👤 <b>{html.escape(data['label'])}</b> — {status}")

    lines.append("──────────────")
    lines.append("<i>/paybuttons to mark cleared · /paylog for pay history</i>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def payout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    settings = context.bot_data.get("settings")
    if settings is None:
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "paid" and len(parts) >= 4:
        uname = parts[2]
        try:
            owed = float(parts[3])
        except ValueError:
            return
        paid_by = query.from_user.id if query.from_user else 0
        _set_paid(settings.database_path, uname, owed, paid_by)
        # Delete the message — agent is paid, remove from list
        if query.message:
            try:
                await query.message.delete()
            except Exception:
                pass
        return

    elif action == "unpaid" and len(parts) >= 3:
        uname = parts[2]
        _clear_paid(settings.database_path, uname)
        # Rebuild and re-show (cleared only)
        records = [r for r in list_all_payments(settings.database_path) if r.cleared is True]
        agents = _build_agents(records)
        ukey = uname.lower()
        data = agents.get(ukey, {"label": f"@{uname}", "uname": uname, "owed": 0.0})
        paid = 0.0
        if query.message:
            try:
                await query.message.edit_text(
                    _agent_row_text(data["label"], data["owed"], paid),
                    parse_mode="HTML",
                    reply_markup=_agent_keyboard(uname, data["owed"], paid),
                )
            except Exception:
                pass
    else:
        return


async def paylog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    settings = context.bot_data.get("settings")
    if settings is None or not is_bot_admin(settings, settings.database_path, update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    _ensure_pay_log_table(settings.database_path)
    with _connect(settings.database_path) as conn:
        rows = conn.execute(
            "SELECT username, display_name, amount, paid_at FROM agent_pay_log ORDER BY paid_at DESC"
        ).fetchall()

    if not rows:
        await update.message.reply_text("No payments recorded yet.")
        return

    lines = ["📋 <b>Pay Log</b>\n──────────────"]
    for r in rows:
        name = (r[1] or f"@{r[0]}") if r[0] else "Unknown"
        dt = r[3][:10]
        lines.append(f"✅ <b>{html.escape(format_amount(r[2]))}</b> → {html.escape(name)} · {dt}")
    lines.append("──────────────")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def build_payout_handlers() -> list:
    return [
        CommandHandler("payout", payout_command),
        CommandHandler("paylog", paylog_command),
        CallbackQueryHandler(payout_callback, pattern=rf"^{CB}"),
    ]
