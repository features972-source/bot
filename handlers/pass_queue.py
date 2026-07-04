"""Pass handoff — notes auto-detected, reposted every 2 minutes until taken."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from config import Settings
from database import (
    PassOffer,
    clear_circulating_pass_notes,
    create_open_pass_offer,
    get_pass_offer,
    list_pending_pass_offers,
    pass_offer_for_notes,
    release_pass_claim,
    try_claim_pass_offer,
    try_mark_pass_taken,
    update_pass_offer,
)
from handlers.admin_access import require_admin
from notes_detect import looks_like_notes, notes_balance_only, notes_has_balance

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "pass:"
PASS_STATUS_PENDING = "pending"
PASS_STATUS_TAKEN = "taken"
PASS_STATUS_EXPIRED = "expired"
PASS_REPOST_SECONDS = 120
PASS_EXPIRE_SECONDS = 3600
PASS_POLL_SECONDS = 30

PASS_NOTES_FILTER = filters.ChatType.GROUPS & ~filters.COMMAND


def build_pass_queue_handlers() -> list:
    return [
        CommandHandler("clearnotes", clearnotes_command),
        CallbackQueryHandler(pass_callback, pattern=rf"^{re.escape(CALLBACK_PREFIX)}"),
    ]


def build_pass_queue_notes_handler() -> MessageHandler:
    notes_filter = PASS_NOTES_FILTER | filters.UpdateType.EDITED_MESSAGE
    return MessageHandler(notes_filter, notes_message_handler, block=False)


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


def _parse_iso_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _pass_keyboard(offer_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Take pass", callback_data=f"{CALLBACK_PREFIX}take:{offer_id}")]]
    )


def _open_pass_text(offer: PassOffer) -> str:
    starter = _mention_html(
        offer.starter_user_id,
        offer.starter_username,
        offer.starter_display_name,
    )
    return (
        "<blockquote>📞 <b>PASS AVAILABLE</b>\n"
        f"▪️ Starter: {starter}\n"
        "▪️ Full notes are private — sent to whoever takes the pass</blockquote>"
    )


def _claimed_pass_text(
    offer: PassOffer,
    user_id: int,
    username: str | None,
    display_name: str | None,
) -> str:
    mention = _mention_html(user_id, username, display_name)
    return (
        f"<blockquote>⏳ <b>PASS LOCKED</b>\n"
        f"▪️ {mention} is taking this pass</blockquote>"
    )


def _taken_pass_announcement(offer: PassOffer, user) -> str:
    taker = _mention_html(user.id, user.username, _display_name(user))
    starter = _mention_html(
        offer.starter_user_id,
        offer.starter_username,
        offer.starter_display_name,
    )
    return (
        f"🚨🚨🚨 <b>{taker} HAS TOOK THE PASS</b> 🚨🚨🚨\n\n"
        f"{starter} — <b>SEND HIM THE NUMBER IN PMs!</b>\n\n"
        "<blockquote>▪️ Full notes were sent privately to the finisher's DMs.</blockquote>"
    )


def _pass_queue_chat_allowed(chat) -> bool:
    return chat is not None and chat.type in ("group", "supergroup")


def pass_offer_expired(offer: PassOffer, *, now: datetime | None = None) -> bool:
    if offer.status != PASS_STATUS_PENDING:
        return False
    now = now or datetime.now(timezone.utc)
    elapsed = (now - _parse_iso_datetime(offer.created_at)).total_seconds()
    return elapsed >= PASS_EXPIRE_SECONDS


def pass_repost_due(offer: PassOffer, *, now: datetime | None = None) -> bool:
    if offer.status != PASS_STATUS_PENDING or offer.assigned_user_id != 0:
        return False
    now = now or datetime.now(timezone.utc)
    anchor = offer.last_reminder_at or offer.created_at
    elapsed = (now - _parse_iso_datetime(anchor)).total_seconds()
    return elapsed >= PASS_REPOST_SECONDS


def _offer_lock(bot_data: dict, offer_id: int) -> asyncio.Lock:
    locks = bot_data.setdefault("pass_offer_locks", {})
    if offer_id not in locks:
        locks[offer_id] = asyncio.Lock()
    return locks[offer_id]


async def _edit_pass_message(
    bot,
    *,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    try:
        await bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        return True
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return True
        return False


async def _sync_offer_message(
    bot,
    offer: PassOffer,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    preferred_message_id: int | None = None,
) -> None:
    """Keep the live pass card in one place — no duplicate Take pass buttons."""
    targets: list[int] = []
    if preferred_message_id is not None:
        targets.append(preferred_message_id)
    if offer.offer_message_id and offer.offer_message_id not in targets:
        targets.append(offer.offer_message_id)

    for message_id in targets:
        await _edit_pass_message(
            bot,
            chat_id=offer.chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )


async def _release_claim_and_reopen(bot, path: str, offer: PassOffer) -> None:
    release_pass_claim(path, offer.id)
    refreshed = get_pass_offer(path, offer.id)
    if refreshed is None or refreshed.status != PASS_STATUS_PENDING:
        return
    await _sync_offer_message(
        bot,
        refreshed,
        text=_open_pass_text(refreshed),
        reply_markup=_pass_keyboard(refreshed.id),
    )


async def pass_repost_loop(bot, settings: Settings, bot_data: dict) -> None:
    """Repost open passes every 2 minutes until taken."""
    while True:
        try:
            await asyncio.sleep(PASS_POLL_SECONDS)
            now = datetime.now(timezone.utc)
            for offer in list_pending_pass_offers(settings.database_path):
                if pass_offer_expired(offer, now=now):
                    await _expire_pass_offer(bot, settings, offer.id)
                    continue
                if pass_repost_due(offer, now=now):
                    await _repost_pass_offer(bot, settings, offer.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pass repost loop error")


async def _send_pass_offer_message(
    bot,
    offer: PassOffer,
    *,
    reply_to_message_id: int | None = None,
    text: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    return await bot.send_message(
        chat_id=offer.chat_id,
        text=text or _open_pass_text(offer),
        parse_mode="HTML",
        reply_markup=reply_markup if reply_markup is not None else _pass_keyboard(offer.id),
        reply_to_message_id=reply_to_message_id,
    )


async def _deliver_pass_offer(
    bot,
    path: str,
    offer: PassOffer,
    *,
    reply_to_message_id: int | None = None,
) -> bool:
    try:
        offer_message = await _send_pass_offer_message(
            bot,
            offer,
            reply_to_message_id=reply_to_message_id,
        )
    except BadRequest:
        if reply_to_message_id is None:
            return False
        try:
            offer_message = await _send_pass_offer_message(bot, offer)
        except BadRequest:
            return False
    except Exception:
        logger.exception("Failed to deliver pass offer %s", offer.id)
        return False

    update_pass_offer(
        path,
        offer.id,
        offer_message_id=offer_message.message_id,
        last_reminder_at=datetime.now(timezone.utc).isoformat(),
    )
    return True


async def _repost_pass_offer(bot, settings: Settings, offer_id: int) -> None:
    path = settings.database_path
    offer = get_pass_offer(path, offer_id)
    if offer is None or offer.status != PASS_STATUS_PENDING or offer.assigned_user_id != 0:
        return

    repost_text = (
        _open_pass_text(offer).removesuffix("</blockquote>")
        + "\n▪️ 🔄 <b>Still available</b> — tap Take pass</blockquote>"
    )
    if offer.offer_message_id and await _edit_pass_message(
        bot,
        chat_id=offer.chat_id,
        message_id=offer.offer_message_id,
        text=repost_text,
        reply_markup=_pass_keyboard(offer.id),
    ):
        update_pass_offer(
            path,
            offer.id,
            last_reminder_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info("Reposted pass offer %s in chat %s (edited)", offer_id, offer.chat_id)
        return

    if await _deliver_pass_offer(
        bot,
        path,
        offer,
        reply_to_message_id=offer.notes_message_id,
    ):
        logger.info("Reposted pass offer %s in chat %s (new message)", offer_id, offer.chat_id)
    else:
        logger.warning("Could not repost pass offer %s", offer_id)


async def _expire_pass_offer(bot, settings: Settings, offer_id: int) -> None:
    offer = get_pass_offer(settings.database_path, offer_id)
    if offer is None or offer.status != PASS_STATUS_PENDING:
        return
    update_pass_offer(settings.database_path, offer.id, status=PASS_STATUS_EXPIRED)
    try:
        await bot.send_message(
            chat_id=offer.chat_id,
            text="⏱ <b>Pass timed out</b> — no one took it.",
            parse_mode="HTML",
            reply_to_message_id=offer.notes_message_id,
        )
    except BadRequest:
        pass


async def clearnotes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="❌ Admins only."):
        return

    message = update.effective_message
    if not message:
        return

    counts = clear_circulating_pass_notes(settings.database_path)
    offers = counts["pending_offers"]
    notes = counts["pending_notes"]
    if offers == 0 and notes == 0:
        await message.reply_text("No active pass notes to clear.")
        return

    parts: list[str] = []
    if offers:
        parts.append(f"{offers} active pass offer{'s' if offers != 1 else ''}")
    if notes:
        parts.append(f"{notes} waiting note{'s' if notes != 1 else ''}")
    await message.reply_text(
        f"🧹 Cleared {' and '.join(parts)}.",
        parse_mode="HTML",
    )


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

    parent = message.reply_to_message
    if parent and parent.from_user and not parent.from_user.is_bot:
        parent_text = (parent.text or parent.caption or "").strip()
        if parent_text and looks_like_notes(parent_text, queue_waiting=True):
            if pass_offer_for_notes(settings.database_path, chat.id, parent.message_id):
                return
            await _offer_pass(
                update,
                context,
                notes_text=f"{parent_text}\n{text}".strip(),
                notes_message=parent,
                starter=parent.from_user,
            )
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

    if pass_offer_for_notes(settings.database_path, chat.id, notes_message.message_id):
        return

    if notes_balance_only(notes_text):
        starter_mention = _mention_html(
            starter.id,
            getattr(starter, "username", None),
            _display_name(starter),
        )
        await notes_message.reply_text(
            f"📝 {starter_mention} — <b>send full notes, not just the balance</b>\n\n"
            "Include name, DOB, bank, and balance (e.g. current / savings).",
            parse_mode="HTML",
        )
        return

    if not notes_has_balance(notes_text):
        starter_mention = _mention_html(
            starter.id,
            getattr(starter, "username", None),
            _display_name(starter),
        )
        await notes_message.reply_text(
            f"📝 {starter_mention} — <b>add balance to your notes</b>\n\n"
            "e.g. <code>current 13004</code>, <code>savings £2834</code>, "
            "<code>£3737.38 current</code>, <code>£5000</code>, or <code>bala 3222</code>",
            parse_mode="HTML",
        )
        return

    try:
        offer_id = create_open_pass_offer(
            settings.database_path,
            chat_id=chat.id,
            notes_message_id=notes_message.message_id,
            starter_user_id=starter.id,
            starter_username=getattr(starter, "username", None),
            starter_display_name=_display_name(starter),
            notes_text=notes_text.strip(),
        )
        created = get_pass_offer(settings.database_path, offer_id)
        assert created is not None
        if not await _deliver_pass_offer(
            context.bot,
            settings.database_path,
            created,
            reply_to_message_id=notes_message.message_id,
        ):
            logger.warning(
                "Pass offer %s created but could not post to chat %s",
                offer_id,
                chat.id,
            )
            return
        logger.info(
            "pass offer chat=%s notes_msg=%s starter=%s offer=%s",
            chat.id,
            notes_message.message_id,
            starter.id,
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
    if not query or not user or not query.data or not query.message:
        return

    parts = query.data.removeprefix(CALLBACK_PREFIX).split(":", 1)
    if len(parts) != 2 or parts[0] != "take":
        await query.answer()
        return

    try:
        offer_id = int(parts[1])
    except ValueError:
        await query.answer("Invalid pass.")
        return

    async with _offer_lock(context.bot_data, offer_id):
        offer = get_pass_offer(settings.database_path, offer_id)
        if offer is None:
            await query.answer("This pass is no longer available.")
            return
        if offer.status == PASS_STATUS_EXPIRED:
            await query.answer("This pass timed out.", show_alert=True)
            return
        if offer.status != PASS_STATUS_PENDING:
            await query.answer("This pass was already taken.", show_alert=True)
            return
        if pass_offer_expired(offer):
            await _expire_pass_offer(context.bot, settings, offer.id)
            await query.answer("This pass timed out.", show_alert=True)
            return
        if user.id == offer.starter_user_id:
            await query.answer("You can't take your own pass.", show_alert=True)
            return

        path = settings.database_path
        clicked_message_id = query.message.message_id
        if offer.assigned_user_id not in (0, user.id):
            holder = _user_label(
                offer.assigned_user_id,
                offer.assigned_username,
                offer.assigned_display_name,
            )
            await query.answer(f"{holder} is already taking this pass.", show_alert=True)
            return

        if offer.assigned_user_id == 0:
            if not try_claim_pass_offer(
                path,
                offer.id,
                telegram_user_id=user.id,
                telegram_username=user.username,
                display_name=_display_name(user),
            ):
                await query.answer("Someone else just took this pass.", show_alert=True)
                return
            offer = get_pass_offer(path, offer.id)
            assert offer is not None
            locked_text = _claimed_pass_text(
                offer, user.id, user.username, _display_name(user)
            )
            await _sync_offer_message(
                context.bot,
                offer,
                text=locked_text,
                reply_markup=None,
                preferred_message_id=clicked_message_id,
            )

        await _complete_take_pass(
            update, context, settings, offer, user, clicked_message_id
        )


async def _complete_take_pass(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    offer: PassOffer,
    user,
    clicked_message_id: int,
) -> None:
    query = update.callback_query
    if not query:
        return

    path = settings.database_path
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
        await _release_claim_and_reopen(context.bot, path, offer)
        await query.answer(
            "Start a private chat with the bot first, then tap Take pass again.",
            show_alert=True,
        )
        return
    except Exception:
        logger.exception("Failed to DM pass notes to user %s", user.id)
        await _release_claim_and_reopen(context.bot, path, offer)
        await query.answer("Could not DM you — try /start in private chat.", show_alert=True)
        return

    if not try_mark_pass_taken(path, offer.id, user.id):
        await query.answer("This pass was already taken.", show_alert=True)
        return

    taker = _mention_html(user.id, user.username, _display_name(user))
    taken_text = f"{taker} — <b>took this pass</b> ✅"
    await _sync_offer_message(
        context.bot,
        offer,
        text=taken_text,
        reply_markup=None,
        preferred_message_id=clicked_message_id,
    )

    try:
        await context.bot.send_message(
            chat_id=offer.chat_id,
            text=_taken_pass_announcement(offer, user),
            parse_mode="HTML",
            reply_to_message_id=offer.notes_message_id,
        )
    except BadRequest:
        try:
            await context.bot.send_message(
                chat_id=offer.chat_id,
                text=_taken_pass_announcement(offer, user),
                parse_mode="HTML",
            )
        except BadRequest:
            pass

    await query.answer("Notes sent to your DMs.")
    logger.info(
        "pass taken chat=%s offer=%s user=%s",
        offer.chat_id,
        offer.id,
        user.id,
    )
