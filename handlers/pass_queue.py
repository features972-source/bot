"""Pass queue — finishers join with /joinqueue; starters post notes for handoff."""

from __future__ import annotations

import html
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from config import Settings
from database import (
    PassOffer,
    PassQueueEntry,
    create_pass_offer,
    get_pass_offer,
    get_pass_queue_position,
    join_pass_queue,
    leave_pass_queue,
    list_pass_queue,
    pass_offer_for_notes,
    rotate_pass_queue_user_to_back,
    update_pass_offer,
)
from notes_detect import looks_like_notes

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "pass:"
PASS_STATUS_PENDING = "pending"
PASS_STATUS_TAKEN = "taken"
PASS_STATUS_BRUSHED = "brushed"

PASS_NOTES_FILTER = filters.ChatType.GROUPS & ~filters.COMMAND


def build_pass_queue_handlers() -> list:
    return [
        CommandHandler("joinqueue", joinqueue_command),
        CommandHandler("leavequeue", leavequeue_command),
        CommandHandler("queue", queue_command),
        CallbackQueryHandler(pass_callback, pattern=rf"^{re.escape(CALLBACK_PREFIX)}"),
    ]


def build_pass_queue_notes_handler() -> MessageHandler:
    """Registered early (group -2) so notes are detected before payment/expense handlers."""
    return MessageHandler(
        PASS_NOTES_FILTER,
        notes_message_handler,
        block=False,
    )


def _display_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    return name or "Unknown"


def _user_label(
    user_id: int,
    username: str | None,
    display_name: str | None,
) -> str:
    if username:
        return f"@{username.lstrip('@')}"
    if display_name:
        return display_name
    return str(user_id)


def _mention_html(
    user_id: int,
    username: str | None,
    display_name: str | None,
) -> str:
    label = html.escape(_user_label(user_id, username, display_name))
    return f'<a href="tg://user?id={user_id}">{label}</a>'


def _pass_keyboard(offer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Take pass",
                    callback_data=f"{CALLBACK_PREFIX}take:{offer_id}",
                ),
                InlineKeyboardButton(
                    "Brush pass",
                    callback_data=f"{CALLBACK_PREFIX}brush:{offer_id}",
                ),
            ]
        ]
    )


def _format_queue_line(entry: PassQueueEntry, *, position: int) -> str:
    return f"{position}. {html.escape(_user_label(entry.user_id, entry.telegram_username, entry.display_name))}"


def _pass_queue_chat_allowed(chat) -> bool:
    return chat is not None and chat.type in ("group", "supergroup")


async def joinqueue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    path = settings.database_path
    joined = join_pass_queue(
        path,
        telegram_user_id=user.id,
        telegram_username=user.username,
        display_name=_display_name(user),
    )
    position = get_pass_queue_position(path, user.id)
    if joined:
        await message.reply_text(
            f"✅ You're in the pass queue (#{position}).\n"
            "You'll get @mentioned when a starter posts notes.",
            parse_mode="HTML",
        )
    else:
        await message.reply_text(
            f"You're already in the queue (#{position}).",
            parse_mode="HTML",
        )


async def leavequeue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    if leave_pass_queue(settings.database_path, user.id):
        await message.reply_text("Left the pass queue.")
    else:
        await message.reply_text("You're not in the pass queue.")


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    if not message:
        return

    entries = list_pass_queue(settings.database_path)
    if not entries:
        await message.reply_text(
            "Pass queue is empty.\n\nFinishers: /joinqueue to wait for the next pass."
        )
        return

    lines = ["<b>Pass queue</b>", ""]
    for index, entry in enumerate(entries, start=1):
        lines.append(_format_queue_line(entry, position=index))
    await message.reply_text("\n".join(lines), parse_mode="HTML")


async def notes_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or user.is_bot:
        return
    if not _pass_queue_chat_allowed(chat):
        return

    text = (message.text or message.caption or "").strip()
    if not text or text.startswith("/"):
        return

    queue = list_pass_queue(settings.database_path)
    queue_waiting = bool(queue)
    if not queue_waiting:
        return

    if not looks_like_notes(text, queue_waiting=True):
        return
    if pass_offer_for_notes(settings.database_path, chat.id, message.message_id):
        return

    await _offer_pass(
        update,
        context,
        notes_text=text,
        notes_message=message,
        starter=user,
    )


async def _offer_pass(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notes_text: str,
    notes_message,
    starter,
) -> None:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or starter is None:
        return

    if pass_offer_for_notes(
        settings.database_path, chat.id, notes_message.message_id
    ):
        return

    queue = list_pass_queue(settings.database_path)
    if not queue:
        await message.reply_text(
            "📝 Notes detected but the pass queue is empty.\n\n"
            "Finishers: /joinqueue to take the next pass."
        )
        return

    assigned = queue[0]
    try:
        offer_id = create_pass_offer(
            settings.database_path,
            chat_id=chat.id,
            notes_message_id=notes_message.message_id,
            starter_user_id=starter.id,
            starter_username=getattr(starter, "username", None),
            starter_display_name=_display_name(starter),
            assigned_user_id=assigned.user_id,
            assigned_username=assigned.telegram_username,
            assigned_display_name=assigned.display_name,
            notes_text=notes_text.strip(),
        )
        mention = _mention_html(
            assigned.user_id,
            assigned.telegram_username,
            assigned.display_name,
        )
        offer_message = await notes_message.reply_text(
            f"{mention} — <b>take this pass</b>",
            parse_mode="HTML",
            reply_markup=_pass_keyboard(offer_id),
        )
        update_pass_offer(
            settings.database_path,
            offer_id,
            offer_message_id=offer_message.message_id,
        )
        logger.info(
            "pass offer chat=%s notes_msg=%s starter=%s assigned=%s offer=%s",
            chat.id,
            notes_message.message_id,
            starter.id,
            assigned.user_id,
            offer_id,
        )
    except Exception:
        logger.exception(
            "Failed to create pass offer chat=%s notes_msg=%s",
            chat.id,
            notes_message.message_id,
        )


async def pass_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not query.data:
        return

    parts = query.data.removeprefix(CALLBACK_PREFIX).split(":", 1)
    if len(parts) != 2:
        await query.answer()
        return

    action, raw_id = parts
    try:
        offer_id = int(raw_id)
    except ValueError:
        await query.answer("Invalid pass.")
        return

    offer = get_pass_offer(settings.database_path, offer_id)
    if offer is None:
        await query.answer("This pass is no longer available.")
        return
    if offer.status != PASS_STATUS_PENDING:
        await query.answer("This pass was already handled.")
        return
    if user.id != offer.assigned_user_id:
        await query.answer("This pass is assigned to someone else.", show_alert=True)
        return

    if action == "take":
        await _handle_take_pass(update, context, settings, offer, user)
    elif action == "brush":
        await _handle_brush_pass(update, context, settings, offer, user)
    else:
        await query.answer()


async def _handle_take_pass(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    offer: PassOffer,
    user,
) -> None:
    query = update.callback_query
    if not query:
        return

    starter_label = _user_label(
        offer.starter_user_id,
        offer.starter_username,
        offer.starter_display_name,
    )
    dm_text = (
        f"📝 <b>Pass notes</b> from {html.escape(starter_label)}\n\n"
        f"{html.escape(offer.notes_text)}"
    )
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=dm_text,
            parse_mode="HTML",
        )
    except Forbidden:
        await query.answer(
            "Start a private chat with the bot first, then tap Take pass again.",
            show_alert=True,
        )
        return
    except Exception:
        logger.exception("Failed to DM pass notes to user %s", user.id)
        await query.answer("Could not DM you — try /start in private chat.", show_alert=True)
        return

    update_pass_offer(settings.database_path, offer.id, status=PASS_STATUS_TAKEN)
    leave_pass_queue(settings.database_path, user.id)

    taker = _mention_html(user.id, user.username, _display_name(user))
    try:
        await query.edit_message_text(
            f"{taker} — <b>took this pass</b> ✅",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    await query.answer("Notes sent to your DMs.")


async def _handle_brush_pass(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    offer: PassOffer,
    user,
) -> None:
    query = update.callback_query
    if not query:
        return

    path = settings.database_path
    rotate_pass_queue_user_to_back(path, user.id)
    queue = list_pass_queue(path)
    if not queue:
        update_pass_offer(path, offer.id, status=PASS_STATUS_BRUSHED)
        try:
            await query.edit_message_text(
                "Pass brushed — queue is now empty.\n\nFinishers: /joinqueue",
                parse_mode="HTML",
            )
        except BadRequest:
            pass
        await query.answer("Brushed — no one else in queue.")
        return

    next_user = queue[0]
    update_pass_offer(
        path,
        offer.id,
        assigned_user_id=next_user.user_id,
        assigned_username=next_user.telegram_username,
        assigned_display_name=next_user.display_name,
    )
    mention = _mention_html(
        next_user.user_id,
        next_user.telegram_username,
        next_user.display_name,
    )
    try:
        await query.edit_message_text(
            f"{mention} — <b>take this pass</b>",
            parse_mode="HTML",
            reply_markup=_pass_keyboard(offer.id),
        )
    except BadRequest:
        pass
    await query.answer("Brushed — offered to next in queue.")
