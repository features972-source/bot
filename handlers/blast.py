"""/blast — admin-only broadcast message to all linked agents with key commands and domain."""
from __future__ import annotations

import html
import logging

from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from handlers.admin_access import is_bot_admin
from database import list_links

logger = logging.getLogger(__name__)

ASK_CONTENT = 0

_COMMANDS = (
    "/mystats — your call stats today & this week\n"
    "/mypay — your estimated earnings this week\n"
    "/remind 30m call back — set a reminder\n"
)


def _domain(settings) -> str:
    fqdn = getattr(settings, "threex_fqdn", None) or ""
    if fqdn:
        return fqdn
    pub = getattr(settings, "public_base_url", None) or ""
    return pub.replace("https://", "").replace("http://", "").strip("/") or "q1paym.my3cx.co.uk"


async def blast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    settings = context.bot_data.get("settings")
    if settings is None or not is_bot_admin(settings, settings.database_path, update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📣 <b>Blast message</b>\n\nWhat's the message content? (plain text)",
        parse_mode="HTML",
    )
    return ASK_CONTENT


async def blast_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ConversationHandler.END

    settings = context.bot_data.get("settings")
    content = update.message.text.strip()
    domain = _domain(settings)
    links = list_links(settings.database_path)

    text = (
        f"📣 <b>Message from management</b>\n\n"
        f"{html.escape(content)}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<b>Key commands:</b>\n"
        f"{_COMMANDS}\n"
        f"<b>Your portal:</b> <code>{html.escape(domain)}</code>"
    )

    sent = 0
    failed = 0
    for link in links:
        if not link.telegram_user_id:
            continue
        try:
            await context.bot.send_message(
                chat_id=link.telegram_user_id,
                text=text,
                parse_mode="HTML",
            )
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"✅ Blast sent to {sent} agent(s)." + (f" ({failed} failed)" if failed else ""),
    )
    return ConversationHandler.END


async def blast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def build_blast_handlers() -> list:
    conv = ConversationHandler(
        entry_points=[CommandHandler("blast", blast_command)],
        states={
            ASK_CONTENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, blast_content),
            ],
        },
        fallbacks=[CommandHandler("cancel", blast_cancel)],
        per_chat=True,
    )
    return [conv]
