"""Pass queue — finishers join with /joinqueue; starters post notes for handoff."""

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
    PassQueueEntry,
    add_pass_queue_vip,
    assign_pending_pass_to_user,
    clear_circulating_pass_notes,
    create_pass_offer,
    delete_pending_pass_note,
    get_pass_offer,
    get_pass_offer_brushed_user_ids,
    get_pass_queue_position,
    is_pass_queue_vip,
    join_pass_queue,
    leave_pass_queue,
    list_pass_queue,
    list_pending_pass_offers,
    pass_offer_for_notes,
    pending_pass_assignee_user_ids,
    pending_pass_offer_for_notes,
    record_pass_offer_brush,
    remove_pass_queue_vip,
    rotate_pass_queue_user_to_back,
    update_pass_offer,
    upsert_pending_pass_note,
)
from handlers.admin_access import require_admin
from notes_detect import (
    looks_like_notes,
    notes_balance_only,
    notes_has_balance,
)

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "pass:"
PASS_STATUS_PENDING = "pending"
PASS_STATUS_TAKEN = "taken"
PASS_STATUS_BRUSHED = "brushed"
PASS_STATUS_EXPIRED = "expired"
PASS_REMINDER_SECONDS = 60
PASS_REMINDER_POLL_SECONDS = 15
PASS_EXPIRE_SECONDS = 600

PASS_NOTES_FILTER = filters.ChatType.GROUPS & ~filters.COMMAND


def _pass_offer_text(offer: PassOffer, *, reminder: bool = False) -> str:
    suffix = " ⏰" if reminder else ""
    if offer.manual_override:
        return (
            f"🚨 <b>Manual override open</b>{suffix}\n"
            "Anyone can take this pass — no queue needed."
        )
    mention = _mention_html(
        offer.assigned_user_id,
        offer.assigned_username,
        offer.assigned_display_name,
    )
    return f"{mention} — 📞 <b>Take this pass</b>{suffix}"


def _manual_override_text(*, reminder: bool = False) -> str:
    suffix = " ⏰" if reminder else ""
    return (
        f"🚨 <b>Manual override open</b>{suffix}\n"
        "Anyone can take this pass — no queue needed."
    )


def _pass_brushed_text(user) -> str:
    mention = _mention_html(user.id, user.username, _display_name(user))
    return f"❌ {mention} — <b>has brushed pass</b>"


def _parse_iso_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def pass_reminder_due(offer: PassOffer, *, now: datetime | None = None) -> bool:
    if offer.status != PASS_STATUS_PENDING or pass_offer_expired(offer, now=now):
        return False
    now = now or datetime.now(timezone.utc)
    anchor = offer.last_reminder_at or offer.created_at
    elapsed = (now - _parse_iso_datetime(anchor)).total_seconds()
    return elapsed >= PASS_REMINDER_SECONDS


def pass_offer_expired(offer: PassOffer, *, now: datetime | None = None) -> bool:
    if offer.status != PASS_STATUS_PENDING:
        return False
    now = now or datetime.now(timezone.utc)
    elapsed = (now - _parse_iso_datetime(offer.created_at)).total_seconds()
    return elapsed >= PASS_EXPIRE_SECONDS


async def pass_reminder_loop(bot, settings: Settings, bot_data: dict) -> None:
    """Ping assigned users every minute until they take or brush the pass."""
    while True:
        try:
            await asyncio.sleep(PASS_REMINDER_POLL_SECONDS)
            now = datetime.now(timezone.utc)
            for offer in list_pending_pass_offers(settings.database_path):
                if pass_offer_expired(offer, now=now):
                    await _expire_pass_offer(bot, settings, offer.id)
                    continue
                if not pass_reminder_due(offer, now=now):
                    continue
                await _send_pass_reminder(bot, settings, offer.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pass reminder loop error")


async def _send_pass_reminder(bot, settings: Settings, offer_id: int) -> None:
    offer = get_pass_offer(settings.database_path, offer_id)
    if offer is None or offer.status != PASS_STATUS_PENDING:
        return
    if pass_offer_expired(offer):
        await _expire_pass_offer(bot, settings, offer_id)
        return
    if not pass_reminder_due(offer):
        return

    try:
        if offer.manual_override:
            await _ping_manual_override(bot, settings, offer, reminder=True)
        else:
            await _ping_assignee(bot, offer, reminder=True)
        update_pass_offer(
            settings.database_path,
            offer.id,
            last_reminder_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info(
            "pass reminder chat=%s offer=%s assigned=%s",
            offer.chat_id,
            offer.id,
            offer.assigned_user_id,
        )
    except BadRequest:
        logger.warning("Pass reminder failed for offer %s (message gone?)", offer.id)
    except Exception:
        logger.exception("Pass reminder failed for offer %s", offer.id)


def build_pass_queue_handlers() -> list:
    return [
        CommandHandler("joinqueue", joinqueue_command),
        CommandHandler("leavequeue", leavequeue_command),
        CommandHandler("queue", queue_command),
        CommandHandler("addvip", addvip_command),
        CommandHandler("removevip", removevip_command),
        CommandHandler("clearnotes", clearnotes_command),
        CallbackQueryHandler(pass_callback, pattern=rf"^{re.escape(CALLBACK_PREFIX)}"),
    ]


def build_pass_queue_notes_handler() -> MessageHandler:
    """Registered early (group -2) so notes are detected before payment/expense handlers."""
    notes_filter = PASS_NOTES_FILTER | filters.UpdateType.EDITED_MESSAGE
    return MessageHandler(
        notes_filter,
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
    vip = " ⭐" if entry.is_vip else ""
    return (
        f"{position}. {html.escape(_user_label(entry.user_id, entry.telegram_username, entry.display_name))}{vip}"
    )


def _pass_queue_chat_allowed(chat) -> bool:
    return chat is not None and chat.type in ("group", "supergroup")


def _next_queue_assignee(
    queue: list[PassQueueEntry],
    *,
    exclude_user_id: int | None = None,
    exclude_user_ids: set[int] | frozenset[int] | None = None,
    busy_user_ids: set[int] | frozenset[int] | None = None,
) -> PassQueueEntry | None:
    busy = busy_user_ids or frozenset()
    excluded = set(exclude_user_ids or ())
    if exclude_user_id is not None:
        excluded.add(exclude_user_id)
    for entry in queue:
        if entry.user_id in excluded:
            continue
        if entry.user_id in busy:
            continue
        return entry
    return None


def _queue_finisher_ids(queue: list[PassQueueEntry], *, starter_user_id: int) -> set[int]:
    return {entry.user_id for entry in queue if entry.user_id != starter_user_id}


def _busy_pass_assignees(settings: Settings, chat_id: int, *, exclude_offer_id: int | None = None) -> set[int]:
    return pending_pass_assignee_user_ids(
        settings.database_path,
        chat_id=chat_id,
        exclude_offer_id=exclude_offer_id,
    )


async def _send_pass_offer_message(
    bot,
    offer: PassOffer,
    *,
    reply_to_message_id: int | None = None,
    reminder: bool = False,
    text: str | None = None,
):
    return await bot.send_message(
        chat_id=offer.chat_id,
        text=text or _pass_offer_text(offer, reminder=reminder),
        parse_mode="HTML",
        reply_markup=_pass_keyboard(offer.id),
        reply_to_message_id=reply_to_message_id,
    )


async def _deliver_pass_offer(
    bot,
    path: str,
    offer: PassOffer,
    *,
    reply_to_message_id: int | None = None,
    text: str | None = None,
    reminder: bool = False,
) -> bool:
    """Post a pass offer to the group; retry without reply if the notes message is gone."""
    try:
        offer_message = await _send_pass_offer_message(
            bot,
            offer,
            reply_to_message_id=reply_to_message_id,
            text=text,
            reminder=reminder,
        )
    except BadRequest:
        if reply_to_message_id is None:
            logger.warning(
                "Failed to deliver pass offer %s to chat %s",
                offer.id,
                offer.chat_id,
            )
            return False
        try:
            offer_message = await _send_pass_offer_message(
                bot,
                offer,
                text=text,
                reminder=reminder,
            )
        except BadRequest:
            logger.warning(
                "Failed to deliver pass offer %s to chat %s (no reply target)",
                offer.id,
                offer.chat_id,
            )
            return False
    except Exception:
        logger.exception("Failed to deliver pass offer %s", offer.id)
        return False

    update_pass_offer(
        path,
        offer.id,
        offer_message_id=offer_message.message_id,
    )
    return True


async def _try_deliver_pending_to_free_finisher(
    bot,
    path: str,
    *,
    chat_id: int | None = None,
) -> bool:
    """When a finisher frees up, offer the oldest waiting notes to the next free person."""
    queue = list_pass_queue(path)
    busy = pending_pass_assignee_user_ids(path, chat_id=chat_id)
    for entry in queue:
        if entry.user_id in busy:
            continue
        offer = assign_pending_pass_to_user(
            path,
            assigned_user_id=entry.user_id,
            assigned_username=entry.telegram_username,
            assigned_display_name=entry.display_name,
        )
        if offer is None:
            continue
        if chat_id is not None and offer.chat_id != chat_id:
            continue
        if await _deliver_pass_offer(
            bot,
            path,
            offer,
            reply_to_message_id=offer.notes_message_id,
        ):
            logger.info(
                "pending pass auto-assigned chat=%s notes_msg=%s assigned=%s offer=%s",
                offer.chat_id,
                offer.notes_message_id,
                entry.user_id,
                offer.id,
            )
            return True
    return False


async def _ping_manual_override(
    bot,
    settings: Settings,
    offer: PassOffer,
    *,
    reminder: bool = False,
) -> None:
    text = _manual_override_text(reminder=reminder)
    reply_to = offer.offer_message_id or offer.notes_message_id
    if offer.offer_message_id is not None:
        try:
            await bot.edit_message_text(
                text,
                chat_id=offer.chat_id,
                message_id=offer.offer_message_id,
                parse_mode="HTML",
                reply_markup=_pass_keyboard(offer.id),
            )
        except BadRequest:
            logger.warning("Could not edit manual override message %s", offer.offer_message_id)
    await _send_pass_offer_message(
        bot,
        offer,
        reply_to_message_id=reply_to,
        reminder=reminder,
        text=text,
    )


async def _expire_pass_offer(bot, settings: Settings, offer_id: int) -> None:
    offer = get_pass_offer(settings.database_path, offer_id)
    if offer is None or offer.status != PASS_STATUS_PENDING:
        return
    update_pass_offer(settings.database_path, offer.id, status=PASS_STATUS_EXPIRED)
    try:
        await bot.send_message(
            chat_id=offer.chat_id,
            text="⏱ <b>Pass timed out</b> — no one took it within 10 minutes.",
            parse_mode="HTML",
            reply_to_message_id=offer.notes_message_id,
        )
    except BadRequest:
        logger.warning("Could not send pass expiry notice for offer %s", offer.id)
    except Exception:
        logger.exception("Failed to expire pass offer %s", offer.id)
    logger.info("pass expired chat=%s offer=%s", offer.chat_id, offer.id)


async def _activate_manual_override(
    bot,
    settings: Settings,
    offer: PassOffer,
    *,
    brushed_text: str | None = None,
    reply_to_message_id: int | None = None,
) -> None:
    path = settings.database_path
    update_pass_offer(path, offer.id, manual_override=True, reset_reminder=True)
    refreshed = get_pass_offer(path, offer.id)
    assert refreshed is not None
    text = _manual_override_text()
    if brushed_text:
        text = f"{brushed_text}\n\n{text}"
    reply_to = reply_to_message_id or offer.notes_message_id
    await _deliver_pass_offer(
        bot,
        path,
        refreshed,
        reply_to_message_id=reply_to,
        text=text,
    )


async def _ping_assignee(
    bot,
    offer: PassOffer,
    *,
    reminder: bool = False,
) -> None:
    text = _pass_offer_text(offer, reminder=reminder)
    reply_to = offer.offer_message_id or offer.notes_message_id
    if offer.offer_message_id is not None:
        try:
            await bot.edit_message_text(
                text,
                chat_id=offer.chat_id,
                message_id=offer.offer_message_id,
                parse_mode="HTML",
                reply_markup=_pass_keyboard(offer.id),
            )
        except BadRequest:
            logger.warning("Could not edit pass offer message %s", offer.offer_message_id)
    await _send_pass_offer_message(
        bot,
        offer,
        reply_to_message_id=reply_to,
        reminder=reminder,
    )


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
    vip_note = " ⭐ VIP — ahead of standard finishers." if is_pass_queue_vip(path, user.id) else ""

    pending_offer = assign_pending_pass_to_user(
        path,
        assigned_user_id=user.id,
        assigned_username=user.username,
        assigned_display_name=_display_name(user),
    )
    if pending_offer is not None:
        sent = await _deliver_pass_offer(
            context.bot,
            path,
            pending_offer,
            reply_to_message_id=pending_offer.notes_message_id,
        )
        if sent:
            logger.info(
                "pending pass assigned chat=%s notes_msg=%s assigned=%s offer=%s",
                pending_offer.chat_id,
                pending_offer.notes_message_id,
                user.id,
                pending_offer.id,
            )
            if joined:
                await message.reply_text(
                    f"✅ You're in the pass queue (#{position}).{vip_note}\n"
                    "A waiting pass was sent to the group.",
                    parse_mode="HTML",
                )
            else:
                await message.reply_text(
                    f"You're already in the queue (#{position}).{vip_note}\n"
                    "A waiting pass was sent to the group.",
                    parse_mode="HTML",
                )
            return
        await message.reply_text(
            f"{'✅ ' if joined else ''}You're in the pass queue (#{position}).{vip_note}\n"
            "Waiting pass assigned but could not post to the group — try /joinqueue again.",
            parse_mode="HTML",
        )
        return

    if joined:
        await message.reply_text(
            f"✅ You're in the pass queue (#{position}).{vip_note}\n"
            "You'll get @mentioned when a starter posts notes.",
            parse_mode="HTML",
        )
    else:
        await message.reply_text(
            f"You're already in the queue (#{position}).{vip_note}",
            parse_mode="HTML",
        )


async def addvip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    if not message:
        return

    target_id: int | None = None
    target_username: str | None = None
    target_display: str | None = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if not target.is_bot:
            target_id = target.id
            target_username = target.username
            target_display = _display_name(target)
    elif context.args and context.args[0].lstrip("-").isdigit():
        target_id = int(context.args[0])

    if target_id is None:
        await message.reply_text(
            "Reply to a user's message with /addvip, or use /addvip &lt;telegram_user_id&gt;.",
            parse_mode="HTML",
        )
        return

    path = settings.database_path
    add_pass_queue_vip(
        path,
        telegram_user_id=target_id,
        telegram_username=target_username,
        display_name=target_display,
    )
    label = (
        f"@{target_username}"
        if target_username
        else (target_display or str(target_id))
    )
    in_queue = any(entry.user_id == target_id for entry in list_pass_queue(path))
    queue_note = " Moved to VIP front of queue." if in_queue else ""
    await message.reply_text(
        f"⭐ {label} is now a pass-queue VIP.{queue_note}\n"
        "They join ahead of standard finishers but stay behind other VIPs.",
        parse_mode="HTML",
    )


async def removevip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    if not message:
        return

    target_id: int | None = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if not target.is_bot:
            target_id = target.id
    elif context.args and context.args[0].lstrip("-").isdigit():
        target_id = int(context.args[0])

    if target_id is None:
        await message.reply_text(
            "Reply to a user with /removevip, or use /removevip &lt;telegram_user_id&gt;.",
            parse_mode="HTML",
        )
        return

    if remove_pass_queue_vip(settings.database_path, target_id):
        await message.reply_text(f"Removed VIP status for user {target_id}.")
    else:
        await message.reply_text("That user is not a pass-queue VIP.")


async def clearnotes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
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
        f"🧹 Cleared {' and '.join(parts)}.\n"
        "Reminders stopped — starters can post fresh notes.",
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

    queue_waiting = bool(list_pass_queue(settings.database_path))
    parent = message.reply_to_message
    if parent and parent.from_user and not parent.from_user.is_bot:
        parent_text = (parent.text or parent.caption or "").strip()
        if parent_text and looks_like_notes(parent_text, queue_waiting=True):
            if pending_pass_offer_for_notes(settings.database_path, chat.id, parent.message_id):
                return
            await _offer_pass(
                update,
                context,
                notes_text=f"{parent_text}\n{text}".strip(),
                notes_message=parent,
                starter=parent.from_user,
            )
            return

    if not looks_like_notes(text, queue_waiting=queue_waiting):
        return
    if pending_pass_offer_for_notes(settings.database_path, chat.id, message.message_id):
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

    if pending_pass_offer_for_notes(
        settings.database_path, chat.id, notes_message.message_id
    ):
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

    queue = list_pass_queue(settings.database_path)
    busy = _busy_pass_assignees(settings, chat.id) if queue else set()
    assigned = (
        _next_queue_assignee(
            queue,
            exclude_user_id=starter.id,
            busy_user_ids=busy,
        )
        if queue
        else None
    )
    if assigned is None:
        upsert_pending_pass_note(
            settings.database_path,
            chat_id=chat.id,
            notes_message_id=notes_message.message_id,
            starter_user_id=starter.id,
            starter_username=getattr(starter, "username", None),
            starter_display_name=_display_name(starter),
            notes_text=notes_text.strip(),
        )
        if not queue:
            await message.reply_text(
                "📝 Notes saved — finishers: /joinqueue to take this pass.",
                parse_mode="HTML",
            )
        elif busy:
            await message.reply_text(
                "📝 Notes saved — everyone in queue already has a pass pending.\n"
                "Pass will offer when a finisher is free or joins with /joinqueue.",
                parse_mode="HTML",
            )
        else:
            await message.reply_text(
                "📝 Notes saved — you can't take your own pass.\n\n"
                "Another finisher needs /joinqueue — pass will offer when they join.",
                parse_mode="HTML",
            )
        return

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
        delete_pending_pass_note(
            settings.database_path, chat.id, notes_message.message_id
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
    if offer.status == PASS_STATUS_EXPIRED:
        await query.answer("This pass timed out.", show_alert=True)
        return
    if offer.status != PASS_STATUS_PENDING:
        await query.answer("This pass was already handled.")
        return
    if pass_offer_expired(offer):
        await _expire_pass_offer(context.bot, settings, offer.id)
        await query.answer("This pass timed out.", show_alert=True)
        return

    if action == "brush" and offer.manual_override:
        await query.answer("Manual override — take the pass or wait for timeout.", show_alert=True)
        return

    if action == "take" and offer.manual_override:
        if user.id == offer.starter_user_id:
            await query.answer("You can't take your own pass.", show_alert=True)
            return
    elif user.id != offer.assigned_user_id:
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
    if offer.manual_override:
        taken_text = f"{taker} — <b>has taken the manual override</b> ✅"
        answer_text = "Manual override taken — notes sent to your DMs."
    else:
        taken_text = f"{taker} — <b>took this pass</b> ✅"
        answer_text = "Notes sent to your DMs."
    try:
        await query.edit_message_text(
            taken_text,
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    await query.answer(answer_text)
    await _try_deliver_pending_to_free_finisher(
        context.bot,
        settings.database_path,
        chat_id=offer.chat_id,
    )


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
    record_pass_offer_brush(path, offer.id, user.id)
    brushed_text = _pass_brushed_text(user)
    try:
        await query.edit_message_text(
            brushed_text,
            parse_mode="HTML",
        )
    except BadRequest:
        pass

    queue = list_pass_queue(path)
    busy = _busy_pass_assignees(settings, offer.chat_id, exclude_offer_id=offer.id)
    brushed_ids = get_pass_offer_brushed_user_ids(path, offer.id)
    next_user = _next_queue_assignee(
        queue,
        exclude_user_id=offer.starter_user_id,
        exclude_user_ids=brushed_ids,
        busy_user_ids=busy,
    )
    if next_user is not None:
        update_pass_offer(
            path,
            offer.id,
            assigned_user_id=next_user.user_id,
            assigned_username=next_user.telegram_username,
            assigned_display_name=next_user.display_name,
            reset_reminder=True,
        )
        refreshed = get_pass_offer(path, offer.id)
        assert refreshed is not None
        reply_to = offer.notes_message_id or query.message.message_id
        await _deliver_pass_offer(
            context.bot,
            path,
            refreshed,
            reply_to_message_id=reply_to,
        )
        await _try_deliver_pending_to_free_finisher(
            context.bot,
            path,
            chat_id=offer.chat_id,
        )
        await query.answer("Brushed — offered to next in queue.")
        return

    finisher_ids = _queue_finisher_ids(queue, starter_user_id=offer.starter_user_id)
    if finisher_ids and finisher_ids <= brushed_ids:
        await _activate_manual_override(
            context.bot,
            settings,
            offer,
            brushed_text=brushed_text,
            reply_to_message_id=offer.notes_message_id or query.message.message_id,
        )
        await query.answer("Brushed — manual override open for everyone.")
        return

    update_pass_offer(path, offer.id, status=PASS_STATUS_BRUSHED)
    try:
        await query.edit_message_text(
            f"{brushed_text}\n\nNo one else free in queue.\n\nFinishers: /joinqueue",
            parse_mode="HTML",
        )
    except BadRequest:
        pass
    await query.answer("Brushed — no one else free.")
