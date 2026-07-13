"""Telegram reminder bot."""

from __future__ import annotations

import html
import logging
import os
import re
import asyncio
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dateutil import parser as date_parser
from telegram import BotCommand, Update
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db


def load_env_file() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {
    int(x.strip())
    for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

HELP = (
    "<blockquote expandable>🔔 <b>REMINDER BOT</b>\n"
    "\n"
    "▪️ <b>/add</b> @user reason\n"
    "▪️ <b>/adduser</b> @user reason\n"
    "     Add a reminder (repeats every 7 days).\n"
    "     e.g. /add @john call back\n"
    "     e.g. /adduser @sarah payment follow up\n"
    "\n"
    "▪️ <b>/list</b>\n"
    "     Show all active reminders.\n"
    "\n"
    "▪️ <b>/newsale</b>\n"
    "     Log a sale + set a 7-day follow-up reminder\n"
    "     (asks who and what).\n"
    "\n"
    "▪️ <b>/sales</b>\n"
    "     Show logged sales.\n"
    "\n"
    "▪️ <b>/setsaledate</b> #id date\n"
    "     Change when a sale was purchased.\n"
    "     e.g. /setsaledate 1 10 Jul 2026\n"
    "\n"
    "▪️ <b>/deletesale</b> #id\n"
    "     Remove a logged sale.\n"
    "     e.g. /deletesale 1</blockquote>"
)


# Conversation states for /newsale
AWAITING_BUYER, AWAITING_PRODUCT = range(2)

REMINDER_INTERVAL = timedelta(days=7)


def allowed(user_id: int) -> bool:
    return not ALLOWED or user_id in ALLOWED


def parse_add_args(text: str) -> tuple[str, str]:
    """Parse: @user reason"""
    text = text.strip()
    if not text:
        raise ValueError("Usage: /add @user <reason>")

    match = re.match(r"(@\w+)\s+(.+)", text, flags=re.IGNORECASE)
    if not match:
        raise ValueError("Start with @username, e.g. /add @john call back")

    username, reason = match.group(1), match.group(2).strip()
    if not reason:
        raise ValueError("Add a reason after the username.")
    return username, reason


def default_remind_at() -> datetime:
    """First reminder fires 7 days from now at 9:00 local time."""
    now = datetime.now().astimezone()
    target = now + REMINDER_INTERVAL
    return target.replace(hour=9, minute=0, second=0, microsecond=0)


def next_reminder_at(remind_at: datetime) -> datetime:
    """Schedule the next occurrence after remind_at, skipping past slots."""
    now = datetime.now(timezone.utc)
    next_at = remind_at.astimezone(timezone.utc) + REMINDER_INTERVAL
    while next_at <= now:
        next_at += REMINDER_INTERVAL
    return next_at


def format_friendly_date(dt: datetime) -> str:
    """Human-readable date, e.g. Fri 10 Jul 2026, 3:30 PM."""
    local = dt.astimezone()
    day = local.strftime("%a %d %b %Y")
    hour = local.strftime("%I").lstrip("0") or "12"
    minute = local.strftime("%M")
    ampm = local.strftime("%p")
    return f"{day}, {hour}:{minute} {ampm}"


def format_date_only(dt: datetime) -> str:
    """Date without time, e.g. Sat 04 Jul 2026."""
    return dt.astimezone().strftime("%a %d %b %Y")


def parse_sale_date(date_text: str) -> datetime:
    """Parse a purchase date (no time required)."""
    text = date_text.strip()
    lower = text.lower()
    now = datetime.now().astimezone()
    default = now.replace(hour=12, minute=0, second=0, microsecond=0)

    if lower == "today":
        return default
    if lower == "yesterday":
        return (default - timedelta(days=1))

    parsed = date_parser.parse(text, fuzzy=False, default=default)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed.replace(hour=12, minute=0, second=0, microsecond=0)


def format_time_ago(dt: datetime) -> str:
    """Elapsed time since dt, e.g. '3 hours ago'."""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = int((now - dt.astimezone(timezone.utc)).total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return "1 minute ago" if minutes == 1 else f"{minutes} minutes ago"
    hours = minutes // 60
    if hours < 24:
        return "1 hour ago" if hours == 1 else f"{hours} hours ago"
    days = hours // 24
    if days < 7:
        return "1 day ago" if days == 1 else f"{days} days ago"
    weeks = days // 7
    if weeks < 5:
        return "1 week ago" if weeks == 1 else f"{weeks} weeks ago"
    months = days // 30
    if months < 12:
        return "1 month ago" if months == 1 else f"{months} months ago"
    years = days // 365
    return "1 year ago" if years == 1 else f"{years} years ago"


def format_purchase_age(dt: datetime) -> str:
    """Calendar-day age for purchases, e.g. Jul 1 -> Jul 4 is 3 days ago."""
    local_date = dt.astimezone().date()
    today = datetime.now().astimezone().date()
    days = (today - local_date).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    weeks = days // 7
    if weeks < 5:
        return "1 week ago" if weeks == 1 else f"{weeks} weeks ago"
    months = days // 30
    if months < 12:
        return "1 month ago" if months == 1 else f"{months} months ago"
    years = days // 365
    return "1 year ago" if years == 1 else f"{years} years ago"


def format_sale_line(sale: db.Sale) -> str:
    ago = format_purchase_age(sale.created_at)
    when = format_date_only(sale.created_at)
    lines = [
        f"💷 <b>#{sale.id} · {html.escape(sale.buyer)}</b>",
        f"▪️ Product: {html.escape(sale.product)}",
        f"▪️ Purchased: {ago} ({when})",
    ]
    if sale.remind_at:
        lines.append(f"▪️ Next reminder: {format_friendly_date(sale.remind_at)} (every 7 days)")
    return "<blockquote>" + "\n".join(lines) + "</blockquote>"


def format_reminder_line(reminder: db.Reminder) -> str:
    when = format_friendly_date(reminder.remind_at)
    return (
        "<blockquote>"
        f"🔔 <b>#{reminder.id} · {html.escape(reminder.username)}</b>\n"
        f"▪️ Reason: {html.escape(reminder.reason)}\n"
        f"▪️ Next: {when}\n"
        f"▪️ Repeats: every 7 days"
        "</blockquote>"
    )


async def guard(update: Update) -> bool:
    user = update.effective_user
    if not user or not allowed(user.id):
        if update.effective_message:
            await update.effective_message.reply_text("You are not allowed to use this bot.")
        return False
    return True


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.effective_message.reply_text(HELP, parse_mode="HTML")


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return

    message = update.effective_message
    user = update.effective_user
    assert message and user

    text = " ".join(context.args) if context.args else ""
    try:
        username, reason = parse_add_args(text)
    except ValueError as exc:
        await message.reply_text(str(exc))
        return

    remind_at = default_remind_at()

    reminder_id = db.add_reminder(
        chat_id=message.chat_id,
        created_by=user.id,
        username=username,
        reason=reason,
        remind_at=remind_at,
    )
    schedule_reminder(context.application, reminder_id, remind_at)

    when = format_friendly_date(remind_at)
    await message.reply_text(
        "<blockquote>"
        f"✅ <b>REMINDER #{reminder_id} SET</b>\n"
        f"▪️ User: {html.escape(username)}\n"
        f"▪️ Reason: {html.escape(reason)}\n"
        f"▪️ Next: {when}\n"
        f"▪️ Repeats: every 7 days"
        "</blockquote>",
        parse_mode="HTML",
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return

    message = update.effective_message
    assert message

    reminders = db.list_active()
    if not reminders:
        await message.reply_text("No active reminders.")
        return

    lines = [format_reminder_line(r) for r in reminders]
    await message.reply_text(
        "🔔 <b>ACTIVE REMINDERS</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


async def newsale_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END

    await update.effective_message.reply_text(
        "<blockquote>💷 <b>NEW SALE</b>\n"
        "▪️ Who was it?\n"
        "     (e.g. @username or their name)</blockquote>",
        parse_mode="HTML",
    )
    return AWAITING_BUYER


async def newsale_buyer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END

    buyer = (update.effective_message.text or "").strip()
    if not buyer:
        await update.effective_message.reply_text("Please enter who the sale was to.")
        return AWAITING_BUYER

    context.user_data["newsale_buyer"] = buyer
    await update.effective_message.reply_text(
        "<blockquote>💷 <b>NEW SALE</b>\n"
        "▪️ What did they buy?</blockquote>",
        parse_mode="HTML",
    )
    return AWAITING_PRODUCT


async def newsale_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard(update):
        return ConversationHandler.END

    message = update.effective_message
    user = update.effective_user
    assert message and user

    product = (message.text or "").strip()
    if not product:
        await message.reply_text("Please enter what they bought.")
        return AWAITING_PRODUCT

    buyer = context.user_data.pop("newsale_buyer", "Unknown")
    reason = f"Sale follow-up: {product}"
    remind_at = default_remind_at()

    reminder_id = db.add_reminder(
        chat_id=message.chat_id,
        created_by=user.id,
        username=buyer,
        reason=reason,
        remind_at=remind_at,
    )
    schedule_reminder(context.application, reminder_id, remind_at)

    sale_id = db.add_sale(
        chat_id=message.chat_id,
        created_by=user.id,
        buyer=buyer,
        product=product,
        remind_at=remind_at,
        reminder_id=reminder_id,
    )

    when = format_friendly_date(remind_at)
    await message.reply_text(
        "<blockquote>"
        f"✅ <b>SALE #{sale_id} LOGGED</b>\n"
        f"▪️ Buyer: {html.escape(buyer)}\n"
        f"▪️ Product: {html.escape(product)}\n"
        f"▪️ Next reminder: {when}\n"
        f"▪️ Repeats: every 7 days"
        "</blockquote>",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def sales_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return

    message = update.effective_message
    assert message

    sales = db.list_sales()
    if not sales:
        await message.reply_text("No sales logged yet.")
        return

    lines = [format_sale_line(s) for s in sales]
    await message.reply_text(
        "💷 <b>SALES</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


async def setsaledate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return

    message = update.effective_message
    assert message

    if len(context.args) < 2:
        await message.reply_text(
            "<blockquote>💷 <b>SET SALE DATE</b>\n"
            "▪️ Usage: /setsaledate #id date\n"
            "▪️ e.g. /setsaledate 1 10 Jul 2026\n"
            "▪️ e.g. /setsaledate 2 yesterday</blockquote>",
            parse_mode="HTML",
        )
        return

    raw_id = context.args[0].lstrip("#")
    if not raw_id.isdigit():
        await message.reply_text("Sale id must be a number, e.g. /setsaledate 1 10 Jul 2026")
        return

    sale_id = int(raw_id)
    date_text = " ".join(context.args[1:])
    try:
        purchased_at = parse_sale_date(date_text)
    except (ValueError, TypeError, OverflowError):
        await message.reply_text(
            "Could not read that date. Try: /setsaledate 1 10 Jul 2026"
        )
        return

    sale = db.get_sale(sale_id)
    if sale is None:
        await message.reply_text(f"Sale #{sale_id} not found.")
        return

    db.update_sale_created_at(sale_id, purchased_at)
    updated = db.get_sale(sale_id)
    assert updated is not None
    when = format_date_only(updated.created_at)
    await message.reply_text(
        "<blockquote>"
        f"✅ <b>SALE #{sale_id} UPDATED</b>\n"
        f"▪️ Buyer: {html.escape(updated.buyer)}\n"
        f"▪️ Product: {html.escape(updated.product)}\n"
        f"▪️ Purchased: {when}"
        "</blockquote>",
        parse_mode="HTML",
    )


def _parse_sale_id(args: list[str]) -> int | None:
    if not args:
        return None
    raw_id = args[0].lstrip("#")
    if not raw_id.isdigit():
        return None
    return int(raw_id)


def _cancel_linked_reminder(app: Application, reminder_id: int | None) -> None:
    if reminder_id is None:
        return
    db.mark_sent(reminder_id)
    if app.job_queue is None:
        return
    for job in app.job_queue.get_jobs_by_name(f"reminder-{reminder_id}"):
        job.schedule_removal()


async def deletesale_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return

    message = update.effective_message
    assert message

    sale_id = _parse_sale_id(context.args or [])
    if sale_id is None:
        await message.reply_text(
            "<blockquote>💷 <b>DELETE SALE</b>\n"
            "▪️ Usage: /deletesale #id\n"
            "▪️ e.g. /deletesale 1</blockquote>",
            parse_mode="HTML",
        )
        return

    sale = db.delete_sale(sale_id)
    if sale is None:
        await message.reply_text(f"Sale #{sale_id} not found.")
        return

    _cancel_linked_reminder(context.application, sale.reminder_id)
    await message.reply_text(
        "<blockquote>"
        f"🗑️ <b>SALE #{sale_id} DELETED</b>\n"
        f"▪️ Buyer: {html.escape(sale.buyer)}\n"
        f"▪️ Product: {html.escape(sale.product)}"
        "</blockquote>",
        parse_mode="HTML",
    )


async def newsale_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("newsale_buyer", None)
    await update.effective_message.reply_text("Sale cancelled.")
    return ConversationHandler.END


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        log.error("Another bot instance is polling this token — duplicate replies possible")
        return
    log.error("Unhandled exception", exc_info=err)


async def fire_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    reminder_id = context.job.data["reminder_id"]
    reminder = db.get_reminder(reminder_id)
    if not reminder or reminder.sent:
        return

    when = format_friendly_date(reminder.remind_at)
    text = (
        "<blockquote>"
        f"🔔 <b>REMINDER #{reminder.id}</b>\n"
        f"▪️ User: {html.escape(reminder.username)}\n"
        f"▪️ Reason: {html.escape(reminder.reason)}\n"
        f"▪️ Due: {when}"
        "</blockquote>"
    )
    await context.bot.send_message(
        chat_id=reminder.chat_id, text=text, parse_mode="HTML"
    )

    next_at = next_reminder_at(reminder.remind_at)
    db.update_reminder_remind_at(reminder_id, next_at)
    db.update_sale_remind_at_by_reminder(reminder_id, next_at)
    schedule_reminder(context.application, reminder_id, next_at)
    log.info("Sent reminder #%s, next at %s", reminder_id, next_at)


def schedule_reminder(app: Application, reminder_id: int, remind_at: datetime) -> None:
    if app.job_queue is None:
        return
    for job in app.job_queue.get_jobs_by_name(f"reminder-{reminder_id}"):
        job.schedule_removal()
    when = remind_at.astimezone(timezone.utc)
    app.job_queue.run_once(
        fire_reminder,
        when=when,
        data={"reminder_id": reminder_id},
        name=f"reminder-{reminder_id}",
    )


def load_pending_reminders(app: Application) -> None:
    now = datetime.now(timezone.utc)
    for reminder in db.list_active():
        if reminder.remind_at <= now:
            app.job_queue.run_once(
                fire_reminder,
                when=0,
                data={"reminder_id": reminder.id},
                name=f"reminder-{reminder.id}",
            )
        else:
            schedule_reminder(app, reminder.id, reminder.remind_at)
    log.info("Loaded %s pending reminders", len(db.list_active()))


async def post_init(app: Application) -> None:
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Help"),
            BotCommand("add", "Add reminder: @user reason"),
            BotCommand("adduser", "Same as /add"),
            BotCommand("list", "List active reminders"),
            BotCommand("newsale", "Log sale + 7-day reminder"),
            BotCommand("sales", "Show logged sales"),
            BotCommand("setsaledate", "Change sale purchase date"),
            BotCommand("deletesale", "Remove a logged sale"),
        ]
    )
    load_pending_reminders(app)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true, "service": "sales-bot", "bot": "@dolphinsiptrunkbot"}')

    def log_message(self, *args) -> None:  # silence per-request logging
        return


def start_health_server() -> None:
    """Serve a tiny HTTP endpoint so Render's web service has a port to bind."""
    port = int(os.getenv("PORT", "10000"))
    httpd = HTTPServer(("0.0.0.0", port), _HealthHandler)
    log.info("Health server on port %s", port)
    httpd.serve_forever()


def main() -> None:
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in your environment or .env file.")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    if os.getenv("PORT"):
        threading.Thread(target=start_health_server, daemon=True).start()

    db.init_db()
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("adduser", add_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("sales", sales_cmd))
    app.add_handler(CommandHandler("setsaledate", setsaledate_cmd))
    app.add_handler(CommandHandler("deletesale", deletesale_cmd))
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("newsale", newsale_cmd)],
            states={
                AWAITING_BUYER: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, newsale_buyer),
                ],
                AWAITING_PRODUCT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, newsale_product),
                ],
            },
            fallbacks=[CommandHandler("cancel", newsale_cancel)],
            per_user=True,
            per_chat=True,
        )
    )
    app.add_error_handler(on_error)

    log.info("Reminder bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
