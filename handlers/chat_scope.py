"""Restrict bot slash commands to private chats except credo picker commands."""

from __future__ import annotations

from telegram import MessageEntity, Update
from telegram.ext import (
    ApplicationHandlerStop,
    ContextTypes,
    MessageHandler,
    filters,
)

GROUP_PAYMENT_COMMANDS = frozenset(
    {
        "out",
        "payments",
        "sent",
        "alltimepayments",
        "alltime",
    }
)

GROUP_ALLOWED_COMMANDS = frozenset(
    {
        "cc",
        "creditcard",
        "credo",
        "credos",
        "activeccs",
        "usingccs",
        "usingcc",
        "finished",
        "setnotify",
        "setnotifypayments",
        "setexpenses",
    }
) | GROUP_PAYMENT_COMMANDS

CREDO_START_ARGS = frozenset({"cc", "creditcard", "credo", "credos"})

PM_ONLY = filters.ChatType.PRIVATE
GROUP_ONLY = filters.ChatType.GROUPS

GROUP_DM_HINT = (
    "Use other bot commands in a **private chat** with me.\n\n"
    "In this group you can use **/payments**, **/out**, **/cc**, **/finished**, "
    "**/usingcc**, and log outs by replying with amounts (e.g. `5182 out`)."
)


def command_name(message, bot_username: str | None) -> str | None:
    text = message.text or message.caption or ""
    if not text:
        return None
    entities = message.entities or message.caption_entities or ()
    for entity in entities:
        if entity.type != MessageEntity.BOT_COMMAND:
            continue
        cmd = text[entity.offset : entity.offset + entity.length]
        name, _, at_bot = cmd.partition("@")
        if at_bot and bot_username and at_bot.lower() != bot_username.lower():
            continue
        return name.lstrip("/").lower()
    return None


def command_arg(message) -> str | None:
    text = (message.text or message.caption or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    return parts[1].split("@")[0].lower()


def is_allowed_group_command(message, bot_username: str | None) -> bool:
    cmd = command_name(message, bot_username)
    if cmd is None:
        return True
    if cmd in GROUP_ALLOWED_COMMANDS:
        return True
    if cmd == "start":
        return command_arg(message) in CREDO_START_ARGS
    return False


class DisallowedGroupCommandFilter(filters.MessageFilter):
    """Match slash commands in groups that are not credo/setup commands."""

    def filter(self, message) -> bool:
        chat = message.chat
        if chat.type not in ("group", "supergroup"):
            return False
        text = message.text or message.caption or ""
        if not text.startswith("/"):
            return False
        entities = message.entities or message.caption_entities or ()
        if not any(entity.type == MessageEntity.BOT_COMMAND for entity in entities):
            return False
        return not is_allowed_group_command(message, bot_username=None)


DISALLOWED_GROUP_COMMAND = filters.COMMAND & filters.ChatType.GROUPS & DisallowedGroupCommandFilter()


def build_group_command_guard_handler() -> MessageHandler:
    return MessageHandler(
        DISALLOWED_GROUP_COMMAND,
        group_command_guard,
        block=True,
    )


async def group_command_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return

    bot_username = getattr(context.bot, "username", None)
    if is_allowed_group_command(message, bot_username):
        return

    await message.reply_text(GROUP_DM_HINT, parse_mode="Markdown")
    raise ApplicationHandlerStop


async def reject_group_command(update: Update) -> bool:
    """Return True when a non-credo command was blocked in a group."""
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return False
    if chat.type not in ("group", "supergroup"):
        return False
    bot = update.get_bot()
    bot_username = getattr(bot, "username", None) if bot else None
    if is_allowed_group_command(message, bot_username):
        return False
    await message.reply_text(GROUP_DM_HINT, parse_mode="Markdown")
    return True
