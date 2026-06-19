"""Per-group blacklist: /blacklist @user reason, remove from group."""

from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import CommandHandler, ContextTypes

from config import Settings
from database import (
    add_chat_blacklist,
    get_chat_blacklist_entry,
    list_chat_blacklist,
    list_links,
    remove_chat_blacklist,
)
from handlers.admin_access import _display_name, _stored_user_label, require_admin

logger = logging.getLogger(__name__)

USERNAME_TOKEN = re.compile(r"^@?([A-Za-z0-9_]{4,})$", re.IGNORECASE)


def build_chat_blacklist_handlers() -> list:
    return [
        CommandHandler("blacklist", blacklist_command),
        CommandHandler("unblacklist", unblacklist_command),
        CommandHandler("blacklisted", blacklisted_command),
        CommandHandler("blocklist", blacklisted_command),
    ]


def _parse_blacklist_args(args: list[str]) -> tuple[str | None, str | None]:
    """Require: /blacklist @username reason text"""
    if len(args) < 2:
        return None, None
    first = args[0].strip()
    match = USERNAME_TOKEN.match(first)
    if not match:
        return None, None
    username = match.group(1)
    reason = " ".join(args[1:]).strip()
    if not reason:
        return None, None
    return username, reason


def _user_id_for_username(database_path: str, username: str) -> tuple[int | None, str | None]:
    normalized = username.lower().lstrip("@")
    for link in list_links(database_path):
        if link.telegram_username and link.telegram_username.lower() == normalized:
            return link.telegram_user_id, link.display_name
    entry = get_chat_blacklist_entry(
        database_path, 0, telegram_username=username
    )
    # get_chat_blacklist_entry needs chat_id - skip wrong call

    return None, None


def _resolve_blacklist_target(
    update: Update, args: list[str], *, database_path: str, chat_id: int
) -> tuple[int | None, str | None, str | None, str | None]:
    username, reason = _parse_blacklist_args(args)
    if not username:
        return None, None, None, None

    user_id: int | None = None
    display_name: str | None = None

    message = update.effective_message
    if message and message.reply_to_message:
        user = message.reply_to_message.from_user
        if (
            user
            and not user.is_bot
            and user.username
            and user.username.lower() == username.lower()
        ):
            user_id = user.id
            display_name = _display_name(user)

    if user_id is None:
        for link in list_links(database_path):
            if link.telegram_username and link.telegram_username.lower() == username.lower():
                user_id = link.telegram_user_id
                display_name = link.display_name
                break

    if user_id is None:
        entry = get_chat_blacklist_entry(
            database_path, chat_id, telegram_username=username
        )
        if entry and entry.telegram_user_id is not None:
            user_id = entry.telegram_user_id
            display_name = entry.display_name

    return user_id, username, display_name, reason


def _parse_unblacklist_args(args: list[str]) -> str | None:
    if not args:
        return None
    match = USERNAME_TOKEN.match(args[0].strip())
    if not match:
        return None
    return match.group(1)


async def _ban_from_group(
    update: Update, *, chat_id: int, user_id: int
) -> tuple[bool, str | None]:
    try:
        await update.get_bot().ban_chat_member(chat_id=chat_id, user_id=user_id)
        return True, None
    except Forbidden:
        return False, (
            "Could not remove them from the group — the bot needs to be a "
            "group admin with permission to ban users."
        )
    except BadRequest as exc:
        logger.warning("ban_chat_member failed chat=%s user=%s: %s", chat_id, user_id, exc)
        return False, f"Could not remove them from the group: {exc.message}"


async def _unban_from_group(
    update: Update, *, chat_id: int, user_id: int
) -> tuple[bool, str | None]:
    try:
        await update.get_bot().unban_chat_member(
            chat_id=chat_id, user_id=user_id, only_if_banned=True
        )
        return True, None
    except Forbidden:
        return False, "Bot lacks permission to unban in this group."
    except BadRequest as exc:
        logger.warning("unban_chat_member failed chat=%s user=%s: %s", chat_id, user_id, exc)
        return False, str(exc.message)


async def blacklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    chat = update.effective_chat
    actor = update.effective_user
    if not message or not chat or not actor:
        return
    if chat.type not in ("group", "supergroup"):
        await message.reply_text("Use /blacklist in your announcement group.")
        return

    user_id, username, display_name, reason = _resolve_blacklist_target(
        update, context.args, database_path=settings.database_path, chat_id=chat.id
    )
    if not username or not reason:
        bot_username = getattr(context.bot, "username", None) or "Q1CallManagerBot"
        await message.reply_text(
            "Format:\n"
            f"`/blacklist@{bot_username} @username reason here`\n\n"
            "Example:\n"
            f"`/blacklist@{bot_username} @NmbrsB posting client details`",
            parse_mode="Markdown",
        )
        return

    result = add_chat_blacklist(
        settings.database_path,
        chat_id=chat.id,
        telegram_username=username,
        telegram_user_id=user_id,
        display_name=display_name,
        reason=reason,
        blocked_by_user_id=actor.id,
        blocked_by_username=actor.username,
    )
    label = _stored_user_label(username, display_name, user_id or 0)

    removed_from_group = False
    kick_note = ""
    if user_id is not None:
        removed_from_group, kick_note = await _ban_from_group(
            update, chat_id=chat.id, user_id=user_id
        )
    else:
        kick_note = (
            "Saved on the block list. Could not remove from group (no user id) — "
            "they may not be linked; ban them manually or reply to their message "
            "and run the same /blacklist @user reason again."
        )

    if result == "unchanged" and not removed_from_group:
        entry = get_chat_blacklist_entry(
            settings.database_path, chat.id, telegram_username=username
        )
        await message.reply_text(
            f"{label} is already blacklisted.\nReason on file: {_reason_line(entry)}"
        )
        return

    lines = [f"Blacklisted {label} in this chat.", f"Reason: {reason}"]
    if removed_from_group:
        lines.append("Removed from the group.")
    elif kick_note:
        lines.append(kick_note)
    lines.append("No on-phone posts; payments cannot use them as starter.")
    await message.reply_text("\n".join(lines))


async def unblacklist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if chat.type not in ("group", "supergroup"):
        await message.reply_text("Use /unblacklist in your announcement group.")
        return

    username = _parse_unblacklist_args(context.args)
    if not username:
        bot_username = getattr(context.bot, "username", None) or "Q1CallManagerBot"
        await message.reply_text(
            f"Format: `/unblacklist@{bot_username} @username`",
            parse_mode="Markdown",
        )
        return

    entry = get_chat_blacklist_entry(
        settings.database_path, chat.id, telegram_username=username
    )
    removed = remove_chat_blacklist(
        settings.database_path,
        chat_id=chat.id,
        telegram_username=username,
        telegram_user_id=entry.telegram_user_id if entry else None,
    )
    if not removed:
        await message.reply_text(f"@{username.lstrip('@')} is not blacklisted here.")
        return

    unban_id = entry.telegram_user_id if entry else None
    unban_note = ""
    if unban_id is not None:
        _, unban_note = await _unban_from_group(
            update, chat_id=chat.id, user_id=unban_id
        )

    label = _stored_user_label(username, entry.display_name if entry else None, unban_id or 0)
    text = f"Removed {label} from the blacklist."
    if unban_note:
        text += f"\n{unban_note}"
    else:
        text += " They can be re-invited to the group if needed."
    await message.reply_text(text)


def _reason_line(entry) -> str:
    if entry is None or not entry.reason:
        return "(none)"
    return entry.reason


async def blacklisted_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if chat.type not in ("group", "supergroup"):
        await message.reply_text("Use /blacklisted in your announcement group.")
        return

    entries = list_chat_blacklist(settings.database_path, chat.id)
    if not entries:
        await message.reply_text("No blacklisted users in this chat.")
        return

    lines = ["Blacklisted in this chat:", ""]
    for entry in entries:
        label = _stored_user_label(
            entry.telegram_username,
            entry.display_name,
            entry.telegram_user_id or 0,
        )
        lines.append(f"• {label}")
        if entry.reason:
            lines.append(f"  Reason: {entry.reason}")
        if entry.blocked_by_username or entry.blocked_by_user_id:
            by = _stored_user_label(
                entry.blocked_by_username,
                None,
                entry.blocked_by_user_id or 0,
            )
            lines.append(f"  By: {by}")
    await message.reply_text("\n".join(lines))
