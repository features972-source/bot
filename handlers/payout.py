"""/payout — cleared payments owed per agent. /setpaid @user — mark paid."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from database import list_all_payments, _connect
from handlers.admin_access import is_bot_admin
from money_format import format_amount

logger = logging.getLogger(__name__)

STARTER_PCT = 0.05
FINISHER_PCT = 0.15
CB = "payout:"


# ---------------------------------------------------------------------------
# DB helpers — store paid_at timestamp per agent so new payments after that
# timestamp are always counted as owed again
# ---------------------------------------------------------------------------

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


def _set_paid_now(database_path: str, username: str, amount: float, paid_by: int) -> None:
    """Record that agent was fully paid at this moment."""
    _ensure_pay_log_table(database_path)
    now = datetime.now(timezone.utc).isoformat()
    with _connect(database_path) as conn:
        # Replace any existing entry
        conn.execute("DELETE FROM agent_pay_log WHERE LOWER(username) = LOWER(?)", (username,))
        conn.execute(
            "INSERT INTO agent_pay_log (paid_at, username, display_name, amount, paid_by_user_id, note) "
            "VALUES (?, ?, NULL, ?, ?, '')",
            (now, username, amount, paid_by),
        )
        conn.commit()


def _clear_paid(database_path: str, username: str) -> None:
    _ensure_pay_log_table(database_path)
    with _connect(database_path) as conn:
        conn.execute("DELETE FROM agent_pay_log WHERE LOWER(username) = LOWER(?)", (username,))
        conn.commit()


def _get_paid_timestamps(database_path: str) -> dict[str, str]:
    """Returns {username_lower: paid_at ISO string}"""
    _ensure_pay_log_table(database_path)
    with _connect(database_path) as conn:
        rows = conn.execute(
            "SELECT username, paid_at FROM agent_pay_log"
        ).fetchall()
    return {(r[0] or "").lower(): r[1] for r in rows}


def _get_paid_amounts(database_path: str) -> dict[str, float]:
    _ensure_pay_log_table(database_path)
    with _connect(database_path) as conn:
        rows = conn.execute(
            "SELECT username, amount FROM agent_pay_log"
        ).fetchall()
    return {(r[0] or "").lower(): r[1] for r in rows}


def _list_pay_log(database_path: str) -> list[dict]:
    _ensure_pay_log_table(database_path)
    with _connect(database_path) as conn:
        rows = conn.execute(
            "SELECT username, display_name, amount, paid_at FROM agent_pay_log ORDER BY paid_at DESC"
        ).fetchall()
    return [{"username": r[0], "display_name": r[1], "amount": r[2], "paid_at": r[3]} for r in rows]


# ---------------------------------------------------------------------------
# Agent building — only count cleared payments AFTER the last paid_at stamp
# ---------------------------------------------------------------------------

def _build_agents_owed(records, paid_timestamps: dict[str, str]) -> dict[str, dict]:
    """
    For each agent, sum cleared payment amounts that arrived AFTER their last
    paid_at timestamp. If never paid, all cleared payments count.
    Returns {ukey: {label, uname, owed}}
    """
    agents: dict[str, dict] = {}

    def _ukey(username, user_id) -> str:
        return (username or str(user_id)).lower().lstrip("@")

    def _label(display_name, username, user_id) -> str:
        name = (display_name or "").strip()
        uname = (username or "").lstrip("@")
        if name and uname:
            return f"{name} (@{uname})"
        return name or (f"@{uname}" if uname else str(user_id))

    def _after_paid(record, ukey: str) -> bool:
        paid_at_str = paid_timestamps.get(ukey)
        if paid_at_str is None:
            return True  # never paid, always counts
        created_str = getattr(record, "created_at", None)
        if not created_str:
            return True  # can't determine, include it
        try:
            # Normalise both to naive UTC datetimes for safe comparison
            def _parse(s: str):
                s = s.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None) + (dt.utcoffset() or __import__("datetime").timedelta(0))
                    dt = dt.replace(tzinfo=None)
                return dt
            return _parse(created_str) > _parse(paid_at_str)
        except Exception:
            return True  # on parse error, include it

    for r in records:
        # Starter
        if r.starter_user_id is not None:
            uk = _ukey(r.starter_username, r.starter_user_id)
            if uk not in agents:
                agents[uk] = {
                    "label": _label(r.starter_display_name, r.starter_username, r.starter_user_id),
                    "uname": (r.starter_username or "").lstrip("@"),
                    "owed": 0.0,
                }
            if _after_paid(r, uk):
                agents[uk]["owed"] += r.amount * STARTER_PCT

        # Finisher
        uk = _ukey(r.finisher_username, r.finisher_user_id)
        if uk not in agents:
            agents[uk] = {
                "label": _label(
                    r.finisher_display_name or r.display_name,
                    r.finisher_username,
                    r.finisher_user_id,
                ),
                "uname": (r.finisher_username or "").lstrip("@"),
                "owed": 0.0,
            }
        if _after_paid(r, uk):
            agents[uk]["owed"] += r.amount * FINISHER_PCT

    return agents


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

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
            "<i>Mark payments as cleared via /paybuttons or /setcleared first.</i>",
            parse_mode="HTML",
        )
        return

    paid_timestamps = _get_paid_timestamps(settings.database_path)
    agents = _build_agents_owed(records, paid_timestamps)

    # Remove agents with nothing owed
    agents_owed = {k: v for k, v in agents.items() if v["owed"] > 0.01}

    remaining_total = sum(d["owed"] for d in agents_owed.values())

    lines = [
        f"💷 <b>Payout — {int(STARTER_PCT*100)}% starter · {int(FINISHER_PCT*100)}% finisher</b>",
        f"Total still owed: <b>{html.escape(format_amount(remaining_total))}</b>",
        "──────────────",
    ]

    if not agents_owed:
        lines.append("✅ Everyone has been paid! Nothing outstanding.")
    else:
        for ukey, data in sorted(agents_owed.items(), key=lambda x: -x[1]["owed"]):
            lines.append(
                f"👤 <b>{html.escape(data['label'])}</b>\n"
                f"   💷 <b>{html.escape(format_amount(data['owed']))}</b> owed"
            )

    lines.append("──────────────")
    lines.append("<i>/setpaid @user to mark paid · /paylog for history</i>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def setpaid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Usage: /setpaid @username"""
    if not update.effective_user or not update.message:
        return
    settings = context.bot_data.get("settings")
    if settings is None or not is_bot_admin(settings, settings.database_path, update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /setpaid @username\nExample: /setpaid @frankgside")
        return

    raw_user = args[0].lstrip("@")

    # Calculate what they were owed right now
    all_records = list_all_payments(settings.database_path)
    cleared = [r for r in all_records if r.cleared is True]
    paid_timestamps = _get_paid_timestamps(settings.database_path)
    agents = _build_agents_owed(cleared, paid_timestamps)
    ukey = raw_user.lower()
    owed = agents.get(ukey, {}).get("owed", 0.0)

    _set_paid_now(settings.database_path, raw_user, owed, update.effective_user.id)

    await update.message.reply_text(
        f"✅ <b>@{html.escape(raw_user)}</b> marked as paid\n"
        f"💷 Amount: <b>{html.escape(format_amount(owed))}</b>\n\n"
        f"<i>Any new cleared payments after now will show up in /payout again.</i>",
        parse_mode="HTML",
    )


async def paylog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    settings = context.bot_data.get("settings")
    if settings is None or not is_bot_admin(settings, settings.database_path, update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    logs = _list_pay_log(settings.database_path)
    if not logs:
        await update.message.reply_text("No payments recorded yet. Use /setpaid @user.")
        return

    lines = ["📋 <b>Pay Log</b>\n──────────────"]
    for entry in logs[:20]:
        name = entry["display_name"] or f"@{entry['username']}" if entry["username"] else "Unknown"
        dt = (entry["paid_at"] or "")[:10]
        lines.append(f"✅ <b>{html.escape(format_amount(entry['amount']))}</b> → {html.escape(name)} · {dt}")
    lines.append("──────────────")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def build_payout_handlers() -> list:
    return [
        CommandHandler("payout", payout_command),
        CommandHandler("setpaid", setpaid_command),
        CommandHandler("paylog", paylog_command),
    ]
