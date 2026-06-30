"""/blast <message> — admin-only broadcast pinned in group. Type 'content' to resend."""
from __future__ import annotations

import html
import logging
import re

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from handlers.admin_access import is_bot_admin
from notify import _notify_chat_ids

logger = logging.getLogger(__name__)

LAST_BLAST_KEY = "last_blast_text"
LAST_BLAST_CONTENT_KEY = "last_blast_content"

_COMMANDS = (
    "/remind [time] [note] — set a personal reminder and the bot will ping you when the time is up\n"
    "Example: /remind 30m call back John\n"
)

_CONTENT_TRIGGER = re.compile(r"\bcontent", re.IGNORECASE)


def _domain(settings) -> str:
    fqdn = getattr(settings, "threex_fqdn", None) or ""
    if fqdn:
        return fqdn
    pub = getattr(settings, "public_base_url", None) or ""
    return pub.replace("https://", "").replace("http://", "").strip("/") or "q1paym.my3cx.co.uk"


def _build_group_text(content: str, domain: str) -> str:
    return (
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


async def _post_and_pin(bot, chat_ids: list[int], text: str) -> None:
    for chat_id in chat_ids:
        try:
            msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            try:
                await bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id, disable_notification=True)
            except Exception:
                pass
        except Exception:
            logger.exception("Blast: failed to post to chat %s", chat_id)


async def blast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    settings = context.bot_data.get("settings")
    if settings is None or not is_bot_admin(settings, settings.database_path, update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    content = " ".join(context.args or []).strip()
    if not content:
        await update.message.reply_text(
            "Usage: /blast Your message here\nExample: /blast Phones are now open, let's go!"
        )
        return

    domain = _domain(settings)
    group_text = _build_group_text(content, domain)
    context.bot_data[LAST_BLAST_KEY] = group_text
    context.bot_data[LAST_BLAST_CONTENT_KEY] = content

    chat_ids = _notify_chat_ids(settings, context.bot_data)
    await _post_and_pin(context.bot, chat_ids, group_text)
    await update.message.reply_text("✅ Blast pinned in group.")


async def blast_content_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with just the blast content when anyone types 'content' in the group."""
    if not update.message or not update.message.text:
        return
    if not _CONTENT_TRIGGER.search(update.message.text):
        return

    last_content = context.bot_data.get(LAST_BLAST_CONTENT_KEY)
    if not last_content:
        return

    await update.message.reply_text(
        f"⚠️🚨 <b>Content:</b> {html.escape(last_content)} 🚨⚠️",
        parse_mode="HTML",
    )


def build_blast_handlers() -> list:
    return [
        CommandHandler("blast", blast_command),
        MessageHandler(filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND, blast_content_trigger),
    ]
