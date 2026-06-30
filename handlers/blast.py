"""/blast <message> — admin-only broadcast to group + all linked agents."""
from __future__ import annotations

import html
import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from handlers.admin_access import is_bot_admin
from database import list_links
from notify import _notify_chat_ids

logger = logging.getLogger(__name__)

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
        f"📣 <b>Message from management</b>\n\n"
        f"{html.escape(content)}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<b>Key commands:</b>\n"
        f"{_COMMANDS}\n"
        f"<b>Your portal:</b> <code>{html.escape(domain)}</code>"
    )

    # Post + pin in notify group
    chat_ids = _notify_chat_ids(settings, context.bot_data)
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


def build_blast_handlers() -> list:
    return [CommandHandler("blast", blast_command)]
