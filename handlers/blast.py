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
from notify import send_to_notify_chats, _notify_chat_ids

logger = logging.getLogger(__name__)

ASK_CONTENT = 0

_COMMANDS = (
    "/remind [time] [note] — set a personal reminder and the bot will ping you when the time is up\n"
    "Example: /remind 30m call back John\n"
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

    group_text = (
        f"⚠️ <b>WARNING — READ IMMEDIATELY</b> ⚠️\n"
        f"🚨🚨🚨 <b>ATTENTION ALL AGENTS</b> 🚨🚨🚨\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{html.escape(content)}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>📌 Key commands:</b>\n"
        f"{_COMMANDS}\n"
        f"🌐 <b>Portal:</b> <code>{html.escape(domain)}</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"‼️ <i>This message requires your immediate attention.</i>\n"
        f"<i>— Management</i>"
    )

    dm_text = (
        f"� <b>Message from management</b>\n\n"
        f"{html.escape(content)}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<b>Key commands:</b>\n"
        f"{_COMMANDS}\n"
        f"<b>Your portal:</b> <code>{html.escape(domain)}</code>"
    )

    # Post + pin in notify chats (the group)
    bot_data = context.bot_data
    chat_ids = _notify_chat_ids(settings, bot_data)
    for chat_id in chat_ids:
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=group_text,
                parse_mode="HTML",
            )
            try:
                await context.bot.pin_chat_message(
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    disable_notification=True,
                )
            except Exception:
                pass
        except Exception:
            logger.exception("Blast: failed to post to chat %s", chat_id)

    # DM every linked agent
    sent = 0
    failed = 0
    for link in links:
        if not link.telegram_user_id:
            continue
        try:
            await context.bot.send_message(
                chat_id=link.telegram_user_id,
                text=dm_text,
                parse_mode="HTML",
            )
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"✅ Blast pinned in group + sent to {sent} agent(s)." + (f" ({failed} failed)" if failed else ""),
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
