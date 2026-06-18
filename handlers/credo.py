"""Credo: whitelisted users pick a credit card then send a photo."""

from __future__ import annotations

from enum import IntEnum, auto

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
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
    list_credo_whitelist,
    remove_credo_credit_card,
    remove_credo_whitelist_user,
    save_credo_profile,
    upsert_credo_credit_card,
)
from telegram.error import Forbidden
from handlers.admin_access import (
    _display_name,
    _resolve_target_user,
    _stored_user_label,
    _user_label,
    is_bot_admin,
    require_admin,
)

PHOTO_FILTER = filters.PHOTO | filters.Document.IMAGE
CALLBACK_CARD_PREFIX = "credocard:"

UNAUTHORIZED = "You are not on the credo whitelist. Ask an admin to add you with /addcredouser."
CANCELLED = "Credo cancelled. Send /credo when you want to try again."
NO_CARDS = (
    "No credit cards are set up yet.\n\n"
    "An admin can add one with /addcredocard (name, then photo)."
)


class State(IntEnum):
    CHOOSE = auto()


class AddCardState(IntEnum):
    NAME = auto()
    PHOTO = auto()


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


def _format_cards_list(cards: list[str]) -> str:
    return "\n".join(f"{index}. {name}" for index, name in enumerate(cards, start=1))


def _card_keyboard(cards: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for index, name in enumerate(cards):
        label = name if len(name) <= 32 else f"{name[:29]}…"
        row.append(
            InlineKeyboardButton(label, callback_data=f"{CALLBACK_CARD_PREFIX}{index}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def _prompt_choose_card(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    user,
) -> int:
    cards = get_credo_credit_cards(settings)
    if not cards:
        await message.reply_text(NO_CARDS)
        return ConversationHandler.END

    context.user_data["credo_cards"] = cards
    await message.reply_text(
        f"💳 **Available credit cards:**\n\n{_format_cards_list(cards)}\n\n"
        "Pick one — the **name and photo** are sent **only to your DMs** (not here).\n"
        "Tap a button or reply with a **number** (e.g. `1`).",
        parse_mode="Markdown",
        reply_markup=_card_keyboard(cards),
    )
    return State.CHOOSE


def _card_from_choice(context: ContextTypes.DEFAULT_TYPE, choice: str | int) -> str | None:
    cards: list[str] = context.user_data.get("credo_cards") or []
    if not cards:
        return None
    if isinstance(choice, int):
        if 1 <= choice <= len(cards):
            return cards[choice - 1]
        return None
    text = str(choice).strip()
    if text.isdigit():
        return _card_from_choice(context, int(text))
    lowered = text.lower()
    for card in cards:
        if card.lower() == lowered:
            return card
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
            f"**{card_name}** is not set up. An admin can add it with /addcredocard.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    dm_caption = f"💳 **{card.name}**"
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

    if sent_to_dm:
        save_credo_profile(
            settings.database_path,
            name=card.name,
            photo_file_id=card.photo_file_id,
            created_by_user_id=sender_user.id,
            created_by_username=sender_user.username,
        )
        await reply_target.reply_text(
            f"✅ Sent **{card.name}** to your DMs.",
            parse_mode="Markdown",
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
                                url=f"https://t.me/{bot_info.username}?start=credo",
                            )
                        ]
                    ]
                )
        except Exception:
            pass
        await reply_target.reply_text(
            f"Could not DM **{card.name}** — the card is **not** posted in this chat.\n\n"
            "Tap the button below, press **Start**, then send /credo again to receive it "
            "in your private messages.",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    context.user_data.clear()
    return ConversationHandler.END


def build_credo_handlers() -> list:
    from handlers.bot_commands import help_conversation_fallback

    menu_fallbacks = [
        CommandHandler("cancel", credo_cancel),
        CommandHandler("start", help_conversation_fallback),
        CommandHandler("help", help_conversation_fallback),
    ]
    user_conversation = ConversationHandler(
        entry_points=[CommandHandler("credo", credo_start)],
        states={
            State.CHOOSE: [
                CallbackQueryHandler(credo_choose_callback, pattern=r"^credocard:\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, credo_choose_text),
            ],
        },
        fallbacks=menu_fallbacks,
        allow_reentry=True,
        name="credo_user",
    )
    add_card_conversation = ConversationHandler(
        entry_points=[CommandHandler("addcredocard", addcredocard_start)],
        states={
            AddCardState.NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, addcredocard_receive_name)
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
        user_conversation,
        add_card_conversation,
        CallbackQueryHandler(credo_standalone_callback, pattern=r"^credocard:\d+$"),
        CommandHandler("addcredouser", addcredouser_command),
        CommandHandler("removecredouser", removecredouser_command),
        CommandHandler("credousers", credousers_command),
        CommandHandler("removecredocard", removecredocard_command),
        CommandHandler("listcredocards", listcredocards_command),
    ]


async def credo_start_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deliver pending card after user opens bot via t.me/Bot?start=credo."""
    if not context.args or context.args[0] != "credo":
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

    await message.reply_text("Send /credo to pick a credit card.")


async def open_credo_picker(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    user,
) -> None:
    """Show credo card buttons (from /credo or the menu)."""
    if not is_credo_allowed(settings, settings.database_path, user.id):
        await message.reply_text(UNAUTHORIZED)
        return
    context.user_data.clear()
    await _prompt_choose_card(message, context, settings, user)


async def credo_standalone_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if "credo_cards" not in context.user_data:
        return
    await credo_choose_callback(update, context)


async def credo_standalone_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if "credo_cards" not in context.user_data:
        return
    await credo_choose_text(update, context)


async def credo_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return ConversationHandler.END

    if not is_credo_allowed(settings, settings.database_path, user.id):
        await message.reply_text(UNAUTHORIZED)
        return ConversationHandler.END

    await open_credo_picker(message, context, settings, user)
    return State.CHOOSE


async def credo_choose_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        await query.edit_message_text("That card is no longer available. Send /credo again.")
        return ConversationHandler.END

    card_name = cards[index]
    await query.edit_message_text(f"Selected: **{card_name}**", parse_mode="Markdown")
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


async def credo_choose_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings: Settings = context.bot_data["settings"]
    if "credo_cards" not in context.user_data:
        context.user_data["credo_cards"] = get_credo_credit_cards(settings)

    card_name = _card_from_choice(context, update.message.text or "")
    if card_name is None:
        cards = context.user_data["credo_cards"]
        await update.message.reply_text(
            f"Pick a number from 1–{len(cards)}, tap a button, or type the card name exactly."
        )
        return State.CHOOSE

    return await _deliver_credo_card(
        bot=context.bot,
        settings=settings,
        sender_user=update.effective_user,
        reply_target=update.message,
        context=context,
        card_name=card_name,
    )


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
    name_from_args = " ".join(context.args).strip() if context.args else ""
    if name_from_args:
        if len(name_from_args) < 2:
            await message.reply_text("Please send a name with at least 2 characters.")
            return AddCardState.NAME
        context.user_data["add_card_name"] = name_from_args
        await message.reply_text(
            f"Got it: **{name_from_args}**\n\nNow send a **photo** for this card.",
            parse_mode="Markdown",
        )
        return AddCardState.PHOTO

    await message.reply_text(
        "Send the **credit card name** to add (e.g. Visa).",
        parse_mode="Markdown",
    )
    return AddCardState.NAME


async def addcredocard_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if len(name) < 2:
        await update.message.reply_text("Please send a valid name (at least 2 characters).")
        return AddCardState.NAME

    context.user_data["add_card_name"] = name
    await update.message.reply_text(
        f"Got it: **{name}**\n\nNow send a **photo** for this card.",
        parse_mode="Markdown",
    )
    return AddCardState.PHOTO


async def addcredocard_receive_photo_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Please send a **photo** (not text).",
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
            "Send the **credit card name** first.",
            parse_mode="Markdown",
        )
        return AddCardState.NAME

    upsert_credo_credit_card(settings.database_path, name, file_id)
    context.user_data.pop("add_card_name", None)
    await message.reply_photo(
        photo=file_id,
        caption=f"✅ Added **{name}** to credo cards.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def addcredocard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("add_card_name", None)
    await update.effective_message.reply_text("Add card cancelled.")
    return ConversationHandler.END


async def removecredocard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /removecredocard Visa")
        return

    if not remove_credo_credit_card(settings.database_path, name):
        await update.effective_message.reply_text(f"**{name}** was not on the list.", parse_mode="Markdown")
        return

    await update.effective_message.reply_text(f"❌ Removed **{name}** from credo cards.", parse_mode="Markdown")


async def listcredocards_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    cards = get_credo_credit_cards(settings)
    if not cards:
        await update.effective_message.reply_text(NO_CARDS)
        return

    await update.effective_message.reply_text(
        "💳 **Credo credit cards**\n\n" + _format_cards_list(cards),
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
        f"✅ {_user_label(target)} can now use /credo."
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
        lines.append("No credo users yet (bot admins can still use /credo).")

    lines.extend(
        [
            "",
            "Bot admins always have access.",
            "Reply with /addcredouser or /removecredouser to change access.",
        ]
    )
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")
