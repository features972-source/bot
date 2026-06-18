"""Credo cards: capacity limits, shared balance, and DM card delivery."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import IntEnum, auto

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import Settings
from database import (
    add_credo_whitelist_user,
    get_credo_credit_card,
    is_on_credo_whitelist,
    list_credo_credit_cards,
    list_credo_card_usage,
    list_credo_whitelist,
    record_credo_card_usage,
    remove_credo_credit_card,
    remove_credo_whitelist_user,
    save_credo_profile,
    sum_credo_card_usage,
    upsert_credo_credit_card,
)
from database import CredoCreditCard
from handlers.admin_access import (
    _display_name,
    _resolve_target_user,
    _stored_user_label,
    _user_label,
    is_bot_admin,
    require_admin,
)
from handlers.payments import parse_payment_amount
from money_format import format_amount
from telegram.error import Forbidden

logger = logging.getLogger(__name__)

PHOTO_FILTER = filters.PHOTO | filters.Document.IMAGE
CALLBACK_CARD_PREFIX = "credocard:"
CREDO_LOG_PROMPT_KEY = "credo_log_prompts"
CREDO_LOG_ORIGIN_KEY = "credo_log_origins"
CREDO_ACTIVE_SESSIONS_KEY = "credo_active_sessions"
CREDO_PICKER_COMMANDS = ("cc", "creditcard", "credo", "credos")
CREDO_START_ARGS = frozenset(CREDO_PICKER_COMMANDS)
CREDO_REMINDER_INTERVAL_SECONDS = 15 * 60
CREDO_ACTIVE_ALLOWED_COMMANDS = frozenset({"mail", "finished"})

UNAUTHORIZED = (
    "You are not on the credo whitelist. Ask an admin to add you with /addcredouser."
)
CANCELLED = "Credo cancelled. Send /credos when you want to try again."
NO_CARDS = (
    "No credit cards are set up yet.\n\n"
    "An admin can add one with /addcredo (name → limit → bank logo → card photo)."
)


class State(IntEnum):
    CHOOSE = auto()


class AddCardState(IntEnum):
    NAME = auto()
    CAPACITY = auto()
    LOGO = auto()
    PHOTO = auto()


@dataclass
class CredoActiveSession:
    card_name: str
    origin_chat_id: int | None
    started_at: float
    last_reminder_at: float


def _active_credo_sessions(bot_data: dict) -> dict[int, CredoActiveSession]:
    return bot_data.setdefault(CREDO_ACTIVE_SESSIONS_KEY, {})


def has_active_credo_session(bot_data: dict, user_id: int) -> bool:
    return user_id in _active_credo_sessions(bot_data)


def get_active_credo_session(bot_data: dict, user_id: int) -> CredoActiveSession | None:
    return _active_credo_sessions(bot_data).get(user_id)


def start_credo_session(
    bot_data: dict,
    *,
    user_id: int,
    card_name: str,
    origin_chat_id: int | None,
) -> None:
    now = time.time()
    _active_credo_sessions(bot_data)[user_id] = CredoActiveSession(
        card_name=card_name,
        origin_chat_id=origin_chat_id,
        started_at=now,
        last_reminder_at=now,
    )


def end_credo_session(bot_data: dict, user_id: int) -> CredoActiveSession | None:
    return _active_credo_sessions(bot_data).pop(user_id, None)


def _command_name(text: str) -> str:
    part = (text or "").strip().split()[0]
    if "@" in part:
        part = part.split("@", 1)[0]
    return part.lstrip("/").lower()


def is_credo_active_command_allowed(command_text: str) -> bool:
    return _command_name(command_text) in CREDO_ACTIVE_ALLOWED_COMMANDS


def is_credo_allowed(settings: Settings, database_path: str, user_id: int) -> bool:
    if user_id in settings.credo_whitelist_user_ids:
        return True
    if is_bot_admin(settings, database_path, user_id):
        return True
    return is_on_credo_whitelist(database_path, user_id)


def get_credo_credit_cards(settings: Settings) -> list[str]:
    seen: set[str] = set()
    cards: list[str] = []
    for record in list_credo_credit_cards(settings.database_path):
        if not record.photo_file_id:
            continue
        key = record.name.strip()
        if not key:
            continue
        lowered = key.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cards.append(key)
    return cards


def list_credo_picker_cards(settings: Settings) -> list[CredoCreditCard]:
    cards: list[CredoCreditCard] = []
    seen: set[str] = set()
    for record in list_credo_credit_cards(settings.database_path):
        if not record.photo_file_id or not record.logo_file_id:
            continue
        key = record.name.strip()
        if not key:
            continue
        lowered = key.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cards.append(record)
    return cards


def _card_balance(settings: Settings, card_name: str) -> tuple[float, float, float]:
    """Return (used, capacity, remaining) for a card."""
    card = get_credo_credit_card(settings.database_path, card_name)
    capacity = float(card.capacity if card else 0)
    used = sum_credo_card_usage(settings.database_path, card_name)
    remaining = max(0.0, capacity - used)
    return used, capacity, remaining


def _credo_log_prompts(bot_data: dict) -> dict[tuple[int, int], str]:
    return bot_data.setdefault(CREDO_LOG_PROMPT_KEY, {})


def _register_credo_log_prompt(
    bot_data: dict, *, chat_id: int, message_id: int, card_name: str
) -> None:
    _credo_log_prompts(bot_data)[(chat_id, message_id)] = card_name


def _lookup_credo_log_prompt(bot_data: dict, *, chat_id: int, message_id: int) -> str | None:
    return _credo_log_prompts(bot_data).get((chat_id, message_id))


def _credo_log_origins(bot_data: dict) -> dict[tuple[int, str], int]:
    return bot_data.setdefault(CREDO_LOG_ORIGIN_KEY, {})


def _register_credo_log_origin(
    bot_data: dict, *, user_id: int, card_name: str, origin_chat_id: int
) -> None:
    _credo_log_origins(bot_data)[(user_id, card_name.strip().lower())] = origin_chat_id


def _get_credo_log_origin(bot_data: dict, *, user_id: int, card_name: str) -> int | None:
    return _credo_log_origins(bot_data).get((user_id, card_name.strip().lower()))


def _resolve_public_credo_chat(
    settings: Settings, bot_data: dict, *, user_id: int, card_name: str
) -> int | None:
    origin = _get_credo_log_origin(bot_data, user_id=user_id, card_name=card_name)
    if origin is not None:
        return origin
    notify = bot_data.get("notify_chat_id") or settings.notify_chat_id
    return notify


def _count_credo_users(database_path: str, card_name: str) -> int:
    usages = list_credo_card_usage(database_path, card_name, limit=500)
    return len({entry.telegram_user_id for entry in usages})


def _format_usage_people_count(database_path: str, card_name: str) -> str:
    count = _count_credo_users(database_path, card_name)
    if count == 1:
        return "Used by 1 person"
    return f"Used by {count} people"


def _format_dm_session_notice(card_name: str, limit_block: str) -> str:
    return (
        f"Sent **{card_name}** to your DMs get worksy! 🔥\n\n"
        f"{limit_block}\n\n"
        "Type any payment amount here (e.g. `500` or `£500`) — **no need to reply** to a message.\n\n"
        "When you're finished with this card, send **/finished**.\n"
        "While a card is active, only **/mail** and **/finished** work."
    )


def _format_card_limit_block(settings: Settings, card_name: str) -> str:
    used, capacity, remaining = _card_balance(settings, card_name)
    usage_line = _format_usage_people_count(settings.database_path, card_name)
    if capacity <= 0:
        return f"{usage_line}\n(No limit set on this account.)"
    return (
        f"Don't send more than {format_amount(remaining)} in payments into this account.\n"
        f"{usage_line}"
    )


def _format_card_capacity(settings: Settings, card_name: str) -> str:
    _, capacity, remaining = _card_balance(settings, card_name)
    if capacity <= 0:
        return f"{card_name} — no limit set"
    usage_line = _format_usage_people_count(settings.database_path, card_name)
    return (
        f"{card_name} — don't send more than {format_amount(remaining)}. "
        f"{usage_line.lower()}"
    )


def _format_cards_list(settings: Settings, cards: list[str]) -> str:
    return "\n".join(_format_card_capacity(settings, name) for name in cards)


def _logo_select_keyboard(index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("\u200b", callback_data=f"{CALLBACK_CARD_PREFIX}{index}")]]
    )


async def _prompt_choose_card(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    user,
) -> int:
    picker_cards = list_credo_picker_cards(settings)
    if not picker_cards:
        if get_credo_credit_cards(settings):
            await message.reply_text(
                "Cards are being set up — bank logos are missing. Ask an admin to /addcredo again."
            )
        else:
            await message.reply_text(NO_CARDS)
        return ConversationHandler.END

    context.user_data["credo_cards"] = [card.name for card in picker_cards]
    for index, card in enumerate(picker_cards):
        await message.reply_photo(
            photo=card.logo_file_id,
            reply_markup=_logo_select_keyboard(index),
        )
    return State.CHOOSE


def _parse_usage_amount(text: str) -> float | None:
    parsed = parse_payment_amount(text.strip())
    if parsed is not None:
        return parsed[0]
    return None


async def _deliver_credo_card(
    *,
    bot,
    settings: Settings,
    sender_user,
    reply_target,
    context: ContextTypes.DEFAULT_TYPE,
    card_name: str,
) -> int:
    if sender_user is None:
        return ConversationHandler.END

    card = get_credo_credit_card(settings.database_path, card_name)
    if card is None or not card.photo_file_id:
        await reply_target.reply_text(
            f"**{card_name}** is not set up. An admin can add it with /addcredo.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    used, capacity, remaining = _card_balance(settings, card_name)
    limit_block = _format_card_limit_block(settings, card_name)
    dm_caption = f"💳 **{card.name}**\n\n{limit_block}"
    sent_to_dm = False

    try:
        await bot.send_photo(
            chat_id=sender_user.id,
            photo=card.photo_file_id,
            caption=dm_caption,
            parse_mode="Markdown",
        )
        sent_to_dm = True
    except (Forbidden, Exception):
        sent_to_dm = False

    context.user_data.pop("credo_selected_card", None)

    dm_prompt_text = _format_dm_session_notice(card.name, limit_block)
    origin_chat_id = (
        reply_target.chat.id if reply_target.chat.type != "private" else None
    )

    if sent_to_dm:
        save_credo_profile(
            settings.database_path,
            name=card.name,
            photo_file_id=card.photo_file_id,
            created_by_user_id=sender_user.id,
            created_by_username=sender_user.username,
        )
        start_credo_session(
            context.application.bot_data,
            user_id=sender_user.id,
            card_name=card.name,
            origin_chat_id=origin_chat_id,
        )
        if origin_chat_id is not None:
            await reply_target.reply_text(
                f"Sent **{card.name}** to your DMs get worksy! 🔥\n\nCheck your **DMs**",
                parse_mode="Markdown",
            )
            prompt = await bot.send_message(
                chat_id=sender_user.id,
                text=dm_prompt_text,
                parse_mode="Markdown",
            )
        else:
            prompt = await reply_target.reply_text(
                dm_prompt_text, parse_mode="Markdown"
            )
    else:
        pending = context.application.bot_data.setdefault("credo_pending", {})
        pending[sender_user.id] = card.name

        keyboard = None
        try:
            bot_info = await bot.get_me()
            if bot_info.username:
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Open bot in private chat",
                                url=f"https://t.me/{bot_info.username}?start=credos",
                            )
                        ]
                    ]
                )
        except Exception:
            pass
        prompt = None
        await reply_target.reply_text(
            f"Could not DM **{card.name}** — the card is **not** posted in this chat.\n\n"
            f"{limit_block}\n\n"
            "Tap the button below, press **Start**, then send /credos again.\n"
            "You must log the amount **in your DMs** with the bot — not in this chat.",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    if prompt:
        _register_credo_log_prompt(
            context.application.bot_data,
            chat_id=prompt.chat_id,
            message_id=prompt.message_id,
            card_name=card.name,
        )
        if origin_chat_id is not None:
            _register_credo_log_origin(
                context.application.bot_data,
                user_id=sender_user.id,
                card_name=card.name,
                origin_chat_id=origin_chat_id,
            )

    return ConversationHandler.END


async def _log_card_usage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    card_name: str,
    amount: float,
) -> None:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    if not is_credo_allowed(settings, settings.database_path, user.id):
        await message.reply_text(UNAUTHORIZED)
        return

    used_before, capacity, remaining_before = _card_balance(settings, card_name)
    if capacity > 0 and amount > remaining_before:
        await message.reply_text(
            f"That would exceed what's left on **{card_name}** "
            f"({format_amount(remaining_before)} remaining).\n"
            f"Send a lower amount or ask an admin.",
            parse_mode="Markdown",
        )
        return

    record_credo_card_usage(
        settings.database_path,
        card_name=card_name,
        telegram_user_id=user.id,
        telegram_username=user.username,
        display_name=_display_name(user),
        amount=amount,
    )
    user_label = _user_label(user)
    limit_block = _format_card_limit_block(settings, card_name)
    summary = (
        f"📊 **{card_name}** — {user_label} logged {format_amount(amount)}.\n\n"
        f"{limit_block}"
    )
    await message.reply_text(
        f"✅ Logged {format_amount(amount)} on **{card_name}**.\n\n{limit_block}",
        parse_mode="Markdown",
    )

    public_chat_id = _resolve_public_credo_chat(
        settings,
        context.application.bot_data,
        user_id=user.id,
        card_name=card_name,
    )
    session = get_active_credo_session(context.application.bot_data, user.id)
    if session and session.origin_chat_id is not None:
        public_chat_id = session.origin_chat_id
    if public_chat_id is not None and message.chat_id != public_chat_id:
        await context.bot.send_message(
            chat_id=public_chat_id,
            text=summary,
            parse_mode="Markdown",
        )


async def try_log_active_credo_amount(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Log a plain-text amount for the user's active credo session. Returns True if handled."""
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text or message.chat.type != "private":
        return False

    session = get_active_credo_session(context.application.bot_data, user.id)
    if session is None:
        return False

    amount = _parse_usage_amount(message.text)
    if amount is None:
        return False

    await _log_card_usage(update, context, card_name=session.card_name, amount=amount)
    return True


async def credo_active_dm_amount_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await try_log_active_credo_amount(update, context):
        raise ApplicationHandlerStop


async def credo_active_command_guard(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return
    if not message.text.startswith("/"):
        return
    if not has_active_credo_session(context.application.bot_data, user.id):
        return
    if is_credo_active_command_allowed(message.text):
        return

    session = get_active_credo_session(context.application.bot_data, user.id)
    card_label = session.card_name if session else "a card"
    await message.reply_text(
        f"**{card_label}** is active — only **/mail** and **/finished** work until you're done.",
        parse_mode="Markdown",
    )
    raise ApplicationHandlerStop


async def credo_finished_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    session = end_credo_session(context.application.bot_data, user.id)
    if session is None:
        await message.reply_text("No active card session. Send /cc to pick one.")
        return

    await message.reply_text(
        f"✅ Finished with **{session.card_name}**. You can use other commands again.",
        parse_mode="Markdown",
    )

    public_chat_id = session.origin_chat_id
    if public_chat_id is None:
        settings: Settings = context.bot_data["settings"]
        public_chat_id = context.application.bot_data.get("notify_chat_id") or settings.notify_chat_id
    if public_chat_id is not None and message.chat_id != public_chat_id:
        user_label = _user_label(user)
        await context.bot.send_message(
            chat_id=public_chat_id,
            text=f"✅ {user_label} finished using **{session.card_name}**.",
            parse_mode="Markdown",
        )


async def credo_reminder_loop(bot, settings: Settings, bot_data: dict) -> None:
    """DM users every 15 minutes while they have an active credo session."""
    while True:
        try:
            await asyncio.sleep(60)
            now = time.time()
            for user_id, session in list(_active_credo_sessions(bot_data).items()):
                if now - session.last_reminder_at < CREDO_REMINDER_INTERVAL_SECONDS:
                    continue
                limit_block = _format_card_limit_block(settings, session.card_name)
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"⏰ **Payment check** — have you sent anything on "
                            f"**{session.card_name}**?\n\n"
                            f"{limit_block}\n\n"
                            "Type the amount here (e.g. `500`) or send **/finished** when you're done."
                        ),
                        parse_mode="Markdown",
                    )
                    session.last_reminder_at = now
                except Forbidden:
                    end_credo_session(bot_data, user_id)
                except Exception:
                    logger.exception(
                        "Failed credo reminder for user %s card %s",
                        user_id,
                        session.card_name,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("credo reminder loop error")


def build_credo_handlers() -> list:
    from handlers.bot_commands import help_conversation_fallback

    menu_fallbacks = [
        CommandHandler("cancel", credo_cancel),
        CommandHandler("start", help_conversation_fallback),
        CommandHandler("help", help_conversation_fallback),
    ]
    user_conversation = ConversationHandler(
        entry_points=[
            CommandHandler(command, credos_start)
            for command in CREDO_PICKER_COMMANDS
        ],
        states={
            State.CHOOSE: [
                CallbackQueryHandler(credos_choose_callback, pattern=r"^credocard:\d+$"),
            ],
        },
        fallbacks=menu_fallbacks,
        allow_reentry=True,
        name="credo_user",
    )
    add_card_conversation = ConversationHandler(
        entry_points=[CommandHandler("addcredo", addcredocard_start)],
        states={
            AddCardState.NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcredocard_receive_name)
            ],
            AddCardState.CAPACITY: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, addcredocard_receive_capacity
                )
            ],
            AddCardState.LOGO: [
                MessageHandler(PHOTO_FILTER, addcredocard_receive_logo),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, addcredocard_receive_logo_text
                ),
            ],
            AddCardState.PHOTO: [
                MessageHandler(PHOTO_FILTER, addcredocard_receive_photo),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, addcredocard_receive_photo_text
                ),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", addcredocard_cancel),
            CommandHandler("start", help_conversation_fallback),
            CommandHandler("help", help_conversation_fallback),
        ],
        allow_reentry=True,
        name="credo_add_card",
    )
    return [
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            credo_active_dm_amount_text,
            block=False,
        ),
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.REPLY,
            credo_reply_log_amount,
            block=False,
        ),
        CommandHandler("finished", credo_finished_command),
        user_conversation,
        add_card_conversation,
        CallbackQueryHandler(credos_standalone_callback, pattern=r"^credocard:\d+$"),
        CommandHandler("addcredouser", addcredouser_command),
        CommandHandler("removecredouser", removecredouser_command),
        CommandHandler("credousers", credousers_command),
        CommandHandler("removecredo", removecredocard_command),
        CommandHandler("listcredocards", listcredocards_command),
    ]


async def credos_start_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deliver pending card after user opens bot via t.me/Bot?start=credos."""
    if not context.args or context.args[0] not in CREDO_START_ARGS:
        return

    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    if not is_credo_allowed(settings, settings.database_path, user.id):
        await message.reply_text(UNAUTHORIZED)
        return

    pending_map = context.application.bot_data.get("credo_pending", {})
    pending_card = pending_map.pop(user.id, None)
    if pending_card:
        await _deliver_credo_card(
            bot=context.bot,
            settings=settings,
            sender_user=user,
            reply_target=message,
            context=context,
            card_name=pending_card,
        )
        return

    await message.reply_text("Send /credos to view cards and pick one.")


async def open_credo_picker(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    user,
) -> None:
    if not is_credo_allowed(settings, settings.database_path, user.id):
        await message.reply_text(UNAUTHORIZED)
        return
    context.user_data.clear()
    await _prompt_choose_card(message, context, settings, user)


async def credos_standalone_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if "credo_cards" not in context.user_data:
        return
    await credos_choose_callback(update, context)


async def credos_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return ConversationHandler.END

    if not is_credo_allowed(settings, settings.database_path, user.id):
        await message.reply_text(UNAUTHORIZED)
        return ConversationHandler.END

    active = get_active_credo_session(context.application.bot_data, user.id)
    if active is not None:
        await message.reply_text(
            f"You already have **{active.card_name}** active.\n\n"
            "Type payment amounts in your DMs or send **/finished** when you're done.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    await open_credo_picker(message, context, settings, user)
    return State.CHOOSE


async def credos_choose_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return State.CHOOSE

    await query.answer()
    settings: Settings = context.bot_data["settings"]
    cards = context.user_data.get("credo_cards") or get_credo_credit_cards(settings)
    context.user_data["credo_cards"] = cards

    try:
        index = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        return State.CHOOSE

    if index < 0 or index >= len(cards):
        await query.edit_message_text("That card is no longer available. Send /credos again.")
        return ConversationHandler.END

    card_name = cards[index]
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    target = query.message
    if not target:
        return ConversationHandler.END
    return await _deliver_credo_card(
        bot=context.bot,
        settings=settings,
        sender_user=query.from_user,
        reply_target=target,
        context=context,
        card_name=card_name,
    )


async def credo_reply_log_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text or not message.reply_to_message:
        return

    if message.chat.type != "private":
        reply = message.reply_to_message
        if (
            reply.from_user
            and reply.from_user.is_bot
            and reply.from_user.id == context.bot.id
        ):
            await message.reply_text(
                "Log the amount **in your DMs** with the bot — reply there to the bot's "
                "private message, **not in this group**.",
                parse_mode="Markdown",
            )
            raise ApplicationHandlerStop
        return

    reply = message.reply_to_message
    if not reply.from_user or not reply.from_user.is_bot:
        return
    if reply.from_user.id != context.bot.id:
        return

    card_name = _lookup_credo_log_prompt(
        context.application.bot_data,
        chat_id=reply.chat_id,
        message_id=reply.message_id,
    )
    if not card_name:
        return

    amount = _parse_usage_amount(message.text)
    if amount is None:
        await message.reply_text(
            "🚨 **Reply to the bot's message above** with the amount you sent "
            "(e.g. `500` or `£500`). You must do this — don't skip it.",
            parse_mode="Markdown",
        )
        raise ApplicationHandlerStop

    await _log_card_usage(update, context, card_name=card_name, amount=amount)
    raise ApplicationHandlerStop


def _photo_file_id(update: Update) -> str | None:
    message = update.message
    if not message:
        return None
    if message.photo:
        return message.photo[-1].file_id
    if message.document and message.document.mime_type and message.document.mime_type.startswith(
        "image/"
    ):
        return message.document.file_id
    return None


async def credo_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text(CANCELLED)
    return ConversationHandler.END


async def addcredocard_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    if not message:
        return ConversationHandler.END
    if not await require_admin(update, settings):
        return ConversationHandler.END

    context.user_data.pop("add_card_name", None)
    context.user_data.pop("add_card_capacity", None)
    context.user_data.pop("add_card_logo_file_id", None)
    context.user_data["add_card_active"] = True

    await message.reply_text(
        "💳 **Add credo card** (4 steps)\n\n"
        "**Step 1 of 4** — Send the **credit card name** (e.g. Lloyds).",
        parse_mode="Markdown",
    )
    return AddCardState.NAME


async def addcredocard_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if len(name) < 2:
        await update.message.reply_text(
            "**Step 1 of 4** — Please send a valid name (at least 2 characters).",
            parse_mode="Markdown",
        )
        return AddCardState.NAME

    context.user_data["add_card_name"] = name
    await update.message.reply_text(
        f"✅ Name: **{name}**\n\n"
        "**Step 2 of 4** — Send the **£ limit** for this card (e.g. `5000` or `5k`).",
        parse_mode="Markdown",
    )
    return AddCardState.CAPACITY


async def addcredocard_receive_capacity(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    amount = _parse_usage_amount(update.message.text or "")
    if amount is None:
        await update.message.reply_text(
            "**Step 2 of 4** — Send a valid limit like `5000`, `£5000`, or `5k`.",
            parse_mode="Markdown",
        )
        return AddCardState.CAPACITY

    context.user_data["add_card_capacity"] = amount
    name = context.user_data.get("add_card_name", "")
    await update.message.reply_text(
        f"✅ Limit: **{format_amount(amount)}** for **{name}**\n\n"
        "**Step 3 of 4** — Send the **bank logo** photo (shown when users pick a card).",
        parse_mode="Markdown",
    )
    return AddCardState.LOGO


async def addcredocard_receive_logo_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    await update.message.reply_text(
        "**Step 3 of 4** — Please send a **bank logo photo** (not text).",
        parse_mode="Markdown",
    )
    return AddCardState.LOGO


async def addcredocard_receive_logo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    file_id = _photo_file_id(update)
    if not file_id:
        await update.message.reply_text("Could not read that image. Try sending the logo again.")
        return AddCardState.LOGO

    context.user_data["add_card_logo_file_id"] = file_id
    name = context.user_data.get("add_card_name", "")
    await update.message.reply_text(
        f"✅ Logo saved for **{name}**\n\n"
        "**Step 4 of 4** — Send a **photo** of the card (sent privately to users).",
        parse_mode="Markdown",
    )
    return AddCardState.PHOTO


async def addcredocard_receive_photo_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "**Step 4 of 4** — Please send a **photo** of the card (not text).",
        parse_mode="Markdown",
    )
    return AddCardState.PHOTO


async def addcredocard_receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings: Settings = context.bot_data["settings"]
    message = update.message
    if not message:
        return ConversationHandler.END

    file_id = _photo_file_id(update)
    if not file_id:
        await message.reply_text("Could not read that image. Try sending a photo again.")
        return AddCardState.PHOTO

    name = context.user_data.get("add_card_name", "").strip()
    if not name:
        await message.reply_text(
            "**Step 1 of 4** — Send the **credit card name** first.",
            parse_mode="Markdown",
        )
        return AddCardState.NAME

    logo_file_id = context.user_data.get("add_card_logo_file_id", "").strip()
    if not logo_file_id:
        await message.reply_text(
            "**Step 3 of 4** — Send the **bank logo** first.",
            parse_mode="Markdown",
        )
        return AddCardState.LOGO

    capacity = float(context.user_data.get("add_card_capacity") or 0)
    upsert_credo_credit_card(
        settings.database_path,
        name,
        file_id,
        capacity=capacity,
        logo_file_id=logo_file_id,
    )
    context.user_data.pop("add_card_name", None)
    context.user_data.pop("add_card_capacity", None)
    context.user_data.pop("add_card_logo_file_id", None)
    context.user_data.pop("add_card_active", None)
    cap_line = format_amount(capacity) if capacity > 0 else "no limit"
    limit_hint = (
        f"Don't send more than {format_amount(capacity)} in payments into this account."
        if capacity > 0
        else "No limit set on this account."
    )
    await message.reply_photo(
        photo=file_id,
        caption=f"✅ **{name}** added · limit {cap_line}\n{limit_hint}",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def addcredocard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("add_card_name", None)
    context.user_data.pop("add_card_capacity", None)
    context.user_data.pop("add_card_logo_file_id", None)
    context.user_data.pop("add_card_active", None)
    await update.effective_message.reply_text("Add card cancelled.")
    return ConversationHandler.END


async def removecredocard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /removecredo Visa")
        return

    if not remove_credo_credit_card(settings.database_path, name):
        await update.effective_message.reply_text(
            f"**{name}** was not on the list.", parse_mode="Markdown"
        )
        return

    await update.effective_message.reply_text(
        f"❌ Removed **{name}** from credo cards.", parse_mode="Markdown"
    )


async def listcredocards_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    cards = get_credo_credit_cards(settings)
    if not cards:
        await update.effective_message.reply_text(NO_CARDS)
        return

    await update.effective_message.reply_text(
        "💳 **Credo credit cards**\n\n" + _format_cards_list(settings, cards),
        parse_mode="Markdown",
    )


async def addcredouser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    target = _resolve_target_user(update, context.args)
    if target is None:
        await update.effective_message.reply_text(
            "Add credo access:\n"
            "• Reply to their message with /addcredouser\n"
            "• Or: /addcredouser <telegram_user_id>"
        )
        return

    if is_credo_allowed(settings, settings.database_path, target.id):
        await update.effective_message.reply_text(
            f"{_user_label(target)} already has credo access."
        )
        return

    add_credo_whitelist_user(
        settings.database_path,
        telegram_user_id=target.id,
        telegram_username=getattr(target, "username", None),
        display_name=_display_name(target),
    )
    from handlers.admin_access import sync_bot_command_menu

    await sync_bot_command_menu(context.bot, settings)
    await update.effective_message.reply_text(
        f"✅ {_user_label(target)} can now use /credos."
    )


async def removecredouser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    target = _resolve_target_user(update, context.args)
    if target is None:
        await update.effective_message.reply_text(
            "Remove credo access:\n"
            "• Reply to their message with /removecredouser\n"
            "• Or: /removecredouser <telegram_user_id>"
        )
        return

    if target.id in settings.credo_whitelist_user_ids:
        await update.effective_message.reply_text(
            "That user is whitelisted in CREDO_WHITELIST_USER_IDS in .env — remove them there."
        )
        return

    if is_bot_admin(settings, settings.database_path, target.id):
        await update.effective_message.reply_text(
            "Bot admins always have credo access. Use /removeadmin if you want to remove them entirely."
        )
        return

    if not remove_credo_whitelist_user(settings.database_path, target.id):
        await update.effective_message.reply_text(
            f"{_user_label(target)} is not on the credo whitelist."
        )
        return

    from handlers.admin_access import revoke_bot_command_menu, sync_bot_command_menu

    await revoke_bot_command_menu(context.bot, target.id)
    await sync_bot_command_menu(context.bot, settings)
    await update.effective_message.reply_text(
        f"❌ Removed credo access for {_user_label(target)}."
    )


async def credousers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    lines = ["📋 **Credo whitelist**", ""]
    if settings.credo_whitelist_user_ids:
        lines.append("From .env (CREDO_WHITELIST_USER_IDS):")
        for user_id in sorted(settings.credo_whitelist_user_ids):
            lines.append(f"• id `{user_id}`")
        lines.append("")

    users = list_credo_whitelist(settings.database_path)
    if users:
        lines.append("In database:")
        for entry in users:
            label = _stored_user_label(
                entry.telegram_username, entry.display_name, entry.telegram_user_id
            )
            lines.append(f"• {label} · id `{entry.telegram_user_id}`")
    elif not settings.credo_whitelist_user_ids:
        lines.append("No credo users yet (bot admins can still use /credos).")

    lines.extend(
        [
            "",
            "Bot admins always have access.",
            "Reply with /addcredouser or /removecredouser to change access.",
        ]
    )
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")
