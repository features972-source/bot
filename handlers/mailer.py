"""Q1 Mailer bridge via Telethon userbot."""

from __future__ import annotations

import logging
import re

from telegram import ReplyKeyboardRemove, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Settings
from handlers.admin_access import is_bot_admin
from mailer_audit import recent_mailer_log_rows
from mailer_bridge import CALLBACK_PREFIX, get_mailer_bridge

logger = logging.getLogger(__name__)


def build_mailer_handlers() -> list:
    from handlers.credo import credo_active_command_guard

    return [
        MessageHandler(filters.COMMAND, credo_active_command_guard, block=False),
        CallbackQueryHandler(mailer_callback, pattern=rf"^{re.escape(CALLBACK_PREFIX)}"),
        CommandHandler("mail", mail_command),
        CommandHandler("maildone", maildone_command),
        CommandHandler("maillogs", maillogs_command),
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            private_text_router,
            block=False,
        ),
    ]


def _bridge(context: ContextTypes.DEFAULT_TYPE):
    return get_mailer_bridge(context.application.bot_data)


async def mail_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    username = f"@{user.username}" if user.username else None
    logger.info(
        "/mail from user %s (%s) in chat %s args=%r",
        user.id,
        username or user.full_name,
        message.chat_id,
        " ".join(context.args).strip(),
    )

    bridge = _bridge(context)
    if bridge is None or not bridge.configured:
        await message.reply_text(
            "Mailer bridge is not enabled on this bot instance.\n\n"
            "Use Q1 Call Manager (not Q2) and ensure TELETHON_API_ID / "
            "TELETHON_API_HASH are set in .env."
        )
        return

    if message.chat.type != "private":
        await message.reply_text("Use /mail in a private chat with the bot.")
        return

    start_args = " ".join(context.args).strip()
    display = (
        f"@{user.username}"
        if user.username
        else (user.full_name or str(user.id))
    )
    ok, detail = await bridge.start_for_user(
        user_id=user.id,
        chat_id=message.chat_id,
        user_display=display,
        telegram_username=user.username,
        start_args=start_args,
    )
    await message.reply_text(detail, parse_mode="HTML")


async def maillogs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    if not is_bot_admin(settings, settings.database_path, user.id):
        await message.reply_text("You are not authorized to view mail logs.")
        return

    limit = 25
    if context.args and context.args[0].isdigit():
        limit = max(1, min(int(context.args[0]), 100))

    rows = recent_mailer_log_rows(settings.database_path, limit=limit)
    if not rows:
        await message.reply_text("No /mail activity logged yet.")
        return

    header = f"**Recent /mail activity** (last {len(rows)} events)\n\n"
    body = "\n".join(rows)
    text = header + body
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    await message.reply_text(text, parse_mode="Markdown")


async def maildone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    bridge = _bridge(context)
    if bridge is None:
        await message.reply_text("Mailer bridge is not enabled.")
        return

    ok, detail = await bridge.end_for_user(user.id)
    await message.reply_text(detail, reply_markup=ReplyKeyboardRemove())


async def mailer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    raw_data = query.data
    if raw_data is None or type(raw_data).__name__ == "InvalidCallbackData":
        await query.answer(
            "Button expired — send /maildone then /mail for a fresh menu.",
            show_alert=True,
        )
        return

    logger.info("Mailer button from user %s: %s", user.id, raw_data)

    bridge = _bridge(context)
    if bridge is None or not bridge.configured:
        await query.answer("Mailer bridge offline.", show_alert=True)
        return

    raw = query.data or ""
    if hasattr(raw, "decode"):
        raw = raw.decode("utf-8", errors="ignore")
    data = str(raw).removeprefix(CALLBACK_PREFIX)
    parts = data.split(":")
    message_id: int | None = None
    row: int | None = None
    col: int | None = None

    if len(parts) == 1 and parts[0].isdigit():
        resolved = bridge.resolve_button_ref(parts[0])
        if resolved is None:
            await query.answer(
                "Button expired — send /mail again for a fresh menu.",
                show_alert=True,
            )
            return
        message_id, row, col = resolved
    elif len(parts) == 2:
        message_id = bridge.last_mailer_message_id
        try:
            row, col = int(parts[0]), int(parts[1])
        except ValueError:
            await query.answer("Invalid button.", show_alert=True)
            return
        if message_id is None:
            await query.answer("Session expired. Send /mail again.", show_alert=True)
            return
    elif len(parts) == 3:
        try:
            message_id, row, col = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            await query.answer("Invalid button.", show_alert=True)
            return
    else:
        await query.answer("Invalid button.", show_alert=True)
        return

    await query.answer("Processing…")
    ok, detail = await bridge.click_button(user.id, message_id, row, col)
    if not ok:
        await query.message.reply_text(detail or "Button click failed.")


def _credo_conversation_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True when credo add-card wizard or card picker is in progress."""
    data = context.user_data
    return bool(
        data.get("add_card_active")
        or data.get("add_card_name")
        or data.get("add_card_capacity")
        or data.get("credo_cards")
    )


async def private_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route private DM text: mailer session, then credo hints."""
    from handlers.credo import get_active_credo_session

    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not message.text:
        return

    if _credo_conversation_active(context):
        return

    bridge = _bridge(context)
    if (
        bridge
        and message.chat.type == "private"
        and bridge.active_user_id == user.id
    ):
        button_action = bridge.match_button_label(message.text)
        if button_action is not None:
            msg_id, row, col = button_action
            logger.info(
                "Mailer button tap from user %s: %r",
                user.id,
                message.text[:80],
            )
            ok, detail = await bridge.click_button(user.id, msg_id, row, col)
            if ok:
                await message.reply_text(
                    f"↪️ Sent to {settings.mailer_display_name}"
                )
            else:
                await message.reply_text(detail or "Button click failed.")
            raise ApplicationHandlerStop

        logger.info(
            "Forwarding mailer text from user %s: %r",
            user.id,
            message.text[:120],
        )
        ok, detail = await bridge.forward_user_text(user.id, message.text)
        if ok:
            await message.reply_text(f"↪️ Sent to {settings.mailer_display_name}")
        elif detail:
            await message.reply_text(detail)
        raise ApplicationHandlerStop

    session = get_active_credo_session(context.application.bot_data, user.id)
    if session is not None and message.chat.type == "private":
        from handlers.credo import _format_card_label

        card_label = _format_card_label(settings.database_path, session.card_name)
        await message.reply_text(
            f"**{card_label}** is active — type a payment amount (e.g. `500`) "
            "or send **/finished** when you're done.",
            parse_mode="Markdown",
        )
        raise ApplicationHandlerStop

    if "credo_cards" in context.user_data:
        return
