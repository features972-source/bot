"""Shared admin checks and delegated admin management."""

from __future__ import annotations

import logging
import re

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    Update,
)
from telegram.error import BadRequest, NetworkError
from telegram.ext import CommandHandler, ContextTypes

from config import Settings
from database import (
    add_bot_admin,
    list_bot_admins,
    list_credo_whitelist,
    list_links,
    remove_bot_admin,
)

logger = logging.getLogger(__name__)

USERNAME_TOKEN = re.compile(r"^@?([A-Za-z0-9_]{4,})$", re.IGNORECASE)

MENU_BOT_COMMANDS = [
    BotCommand("start", "Bot info"),
    BotCommand("help", "Command list"),
]

ADMIN_BOT_COMMANDS = MENU_BOT_COMMANDS + [
    BotCommand("activeccs", "See which cards are in use"),
    BotCommand("usingcc", "See which cards are in use (short)"),
]

CREDO_USER_COMMANDS = MENU_BOT_COMMANDS + [
    BotCommand("cc", "View credo cards & capacity"),
    BotCommand("activeccs", "See which cards are in use"),
    BotCommand("usingcc", "See which cards are in use (short)"),
    BotCommand("finished", "End active credo session"),
]

CREDO_ONLY_GROUP_COMMANDS = [
    BotCommand("help", "Command list"),
    BotCommand("start", "Bot info"),
    BotCommand("cc", "Pick a credo card"),
    BotCommand("credos", "Pick a credo card"),
    BotCommand("usingcc", "See which cards are in use"),
    BotCommand("finished", "End active credo session"),
]

CREDO_ONLY_ADMIN_COMMANDS = CREDO_USER_COMMANDS + [
    BotCommand("addcredo", "Add a credo card (DM)"),
    BotCommand("listcredocards", "List credo cards"),
    BotCommand("setlimit", "Set card amount left"),
    BotCommand("removecredo", "Remove a credo card"),
    BotCommand("addcredouser", "Whitelist user for credos"),
    BotCommand("removecredouser", "Remove credo whitelist"),
    BotCommand("credousers", "List credo whitelist"),
]

CREDO_GROUP_COMMANDS = [
    BotCommand("help", "Command list"),
    BotCommand("start", "Bot info"),
    BotCommand("out", "Log payment (reply + /out 5182)"),
    BotCommand("payments", "This week's payments (resets Sunday)"),
    BotCommand("alltimepayments", "All-time payment totals"),
    BotCommand("alltime", "All-time payment totals (short)"),
    BotCommand("leaderboard", "Opener & closer rankings"),
    BotCommand("nemesis", "Head-to-head payment rivalry"),
    BotCommand("cc", "Pick a credo card"),
    BotCommand("credos", "Pick a credo card"),
    BotCommand("usingcc", "See which cards are in use"),
    BotCommand("finished", "End active credo session"),
]

EXPENSE_GROUP_COMMANDS = CREDO_GROUP_COMMANDS + [
    BotCommand("expense", "Log expense (who · amount · where)"),
    BotCommand("removeexpense", "Remove an expense (# or reply)"),
]


def _payment_group_chat_ids(settings: Settings) -> set[int]:
    from database import get_payment_notify_chat_id

    ids: set[int] = set()
    if settings.notify_chat_id is not None:
        ids.add(settings.notify_chat_id)
    payment_notify_id = get_payment_notify_chat_id(settings.database_path)
    if payment_notify_id is not None:
        ids.add(payment_notify_id)
    if settings.copy_to_chat_id is not None:
        ids.add(settings.copy_to_chat_id)
    return ids


def _expense_group_chat_ids(settings: Settings) -> set[int]:
    from database import get_expense_logging_chat_id, get_expense_report_chat_id

    ids: set[int] = set()
    for chat_id in (
        get_expense_logging_chat_id(settings.database_path),
        get_expense_report_chat_id(settings.database_path),
    ):
        if chat_id is not None:
            ids.add(chat_id)
    return ids


def build_admin_access_handlers() -> list:
    return [
        CommandHandler("admin", admin_command),
        CommandHandler("admins", admins_command),
        CommandHandler("addadmin", addadmin_command),
        CommandHandler("removeadmin", removeadmin_command),
    ]


def is_bot_admin(settings: Settings, database_path: str, user_id: int) -> bool:
    if settings.admin_chat_id is not None and user_id == settings.admin_chat_id:
        return True
    return any(admin.telegram_user_id == user_id for admin in list_bot_admins(database_path))


def is_primary_admin(settings: Settings, user_id: int) -> bool:
    return settings.admin_chat_id is not None and user_id == settings.admin_chat_id


def iter_credo_only_user_ids(settings: Settings, database_path: str) -> list[int]:
    admin_ids = set(iter_bot_admin_user_ids(settings, database_path))
    credo_ids: set[int] = set(settings.credo_whitelist_user_ids)
    for entry in list_credo_whitelist(database_path):
        credo_ids.add(entry.telegram_user_id)
    return sorted(user_id for user_id in credo_ids if user_id not in admin_ids)


def iter_bot_admin_user_ids(settings: Settings, database_path: str) -> list[int]:
    ids: list[int] = []
    if settings.admin_chat_id is not None:
        ids.append(settings.admin_chat_id)
    for admin in list_bot_admins(database_path):
        if admin.telegram_user_id not in ids:
            ids.append(admin.telegram_user_id)
    return ids


async def _clear_command_scope(bot: Bot, scope) -> None:
    try:
        await bot.delete_my_commands(scope=scope)
        await bot.set_my_commands([], scope=scope)
    except (BadRequest, NetworkError) as exc:
        logger.warning("Could not clear command scope %s: %s", scope, exc)


async def sync_bot_command_menu(bot: Bot, settings: Settings) -> None:
    """Private chats get full menus; groups show the main command set."""
    if settings.credo_only_mode:
        await sync_credo_only_bot_command_menu(bot, settings)
        return
    try:
        scopes_to_clear = [
            BotCommandScopeDefault(),
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllGroupChats(),
            BotCommandScopeAllChatAdministrators(),
        ]
        if settings.copy_to_chat_id is not None:
            scopes_to_clear.append(BotCommandScopeChat(chat_id=settings.copy_to_chat_id))

        if settings.notify_chat_id is not None:
            scopes_to_clear.append(BotCommandScopeChat(chat_id=settings.notify_chat_id))
        for chat_id in _payment_group_chat_ids(settings):
            scopes_to_clear.append(BotCommandScopeChat(chat_id=chat_id))
        for chat_id in _expense_group_chat_ids(settings):
            scopes_to_clear.append(BotCommandScopeChat(chat_id=chat_id))

        for scope in scopes_to_clear:
            await _clear_command_scope(bot, scope)

        await bot.set_my_commands(
            MENU_BOT_COMMANDS
            + [
                BotCommand("mail", f"Open {settings.mailer_display_name}"),
                BotCommand("maildone", "End mailer session"),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )

        await bot.set_my_commands(
            CREDO_GROUP_COMMANDS,
            scope=BotCommandScopeAllGroupChats(),
        )

        for chat_id in _payment_group_chat_ids(settings):
            cmds = (
                EXPENSE_GROUP_COMMANDS
                if chat_id in _expense_group_chat_ids(settings)
                else CREDO_GROUP_COMMANDS
            )
            await bot.set_my_commands(
                cmds,
                scope=BotCommandScopeChat(chat_id=chat_id),
            )
        for chat_id in _expense_group_chat_ids(settings):
            if chat_id in _payment_group_chat_ids(settings):
                continue
            await bot.set_my_commands(
                EXPENSE_GROUP_COMMANDS,
                scope=BotCommandScopeChat(chat_id=chat_id),
            )
        try:
            await bot.set_my_commands(
                CREDO_GROUP_COMMANDS,
                scope=BotCommandScopeAllChatAdministrators(),
            )
        except BadRequest:
            logger.warning("Could not set group command menu")

        for user_id in iter_bot_admin_user_ids(settings, settings.database_path):
            scope = BotCommandScopeChat(chat_id=user_id)
            try:
                await bot.set_my_commands(ADMIN_BOT_COMMANDS, scope=scope)
            except BadRequest:
                logger.warning("Could not set admin commands for user %s", user_id)

        for user_id in iter_credo_only_user_ids(settings, settings.database_path):
            scope = BotCommandScopeChat(chat_id=user_id)
            try:
                await bot.set_my_commands(CREDO_USER_COMMANDS, scope=scope)
            except BadRequest:
                logger.warning("Could not set credo commands for user %s", user_id)
    except NetworkError as exc:
        logger.warning("Telegram network error syncing command menu (will retry on next deploy): %s", exc)


async def sync_credo_only_bot_command_menu(bot: Bot, settings: Settings) -> None:
    """Command menus for the credo-only bot (no payments, mailer, or call features)."""
    try:
        scopes_to_clear = [
            BotCommandScopeDefault(),
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllGroupChats(),
            BotCommandScopeAllChatAdministrators(),
        ]
        if settings.notify_chat_id is not None:
            scopes_to_clear.append(BotCommandScopeChat(chat_id=settings.notify_chat_id))

        for scope in scopes_to_clear:
            await _clear_command_scope(bot, scope)

        await bot.set_my_commands(
            CREDO_USER_COMMANDS,
            scope=BotCommandScopeAllPrivateChats(),
        )
        await bot.set_my_commands(
            CREDO_ONLY_GROUP_COMMANDS,
            scope=BotCommandScopeAllGroupChats(),
        )
        try:
            await bot.set_my_commands(
                CREDO_ONLY_GROUP_COMMANDS,
                scope=BotCommandScopeAllChatAdministrators(),
            )
        except BadRequest:
            logger.warning("Could not set credo-only group command menu")

        for user_id in iter_bot_admin_user_ids(settings, settings.database_path):
            scope = BotCommandScopeChat(chat_id=user_id)
            try:
                await bot.set_my_commands(CREDO_ONLY_ADMIN_COMMANDS, scope=scope)
            except BadRequest:
                logger.warning("Could not set credo-only admin commands for user %s", user_id)

        for user_id in iter_credo_only_user_ids(settings, settings.database_path):
            scope = BotCommandScopeChat(chat_id=user_id)
            try:
                await bot.set_my_commands(CREDO_USER_COMMANDS, scope=scope)
            except BadRequest:
                logger.warning("Could not set credo-only user commands for user %s", user_id)
    except NetworkError as exc:
        logger.warning(
            "Telegram network error syncing credo-only command menu (will retry on next deploy): %s",
            exc,
        )


async def revoke_bot_command_menu(bot: Bot, user_id: int) -> None:
    scope = BotCommandScopeChat(chat_id=user_id)
    await bot.delete_my_commands(scope=scope)


async def require_admin(
    update: Update, settings: Settings, *, deny_message: str | None = None
) -> bool:
    user = update.effective_user
    if user and is_bot_admin(settings, settings.database_path, user.id):
        return True
    if deny_message and update.effective_message:
        await update.effective_message.reply_text(deny_message)
    return False


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await admins_command(update, context)


async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    target = _resolve_target_user(update, context.args)
    if target is None:
        await update.effective_message.reply_text(
            "Grant admin to someone:\n"
            "• Reply to their message with /addadmin\n"
            "• Or: /addadmin <telegram_user_id>"
        )
        return

    if is_bot_admin(settings, settings.database_path, target.id):
        await update.effective_message.reply_text(f"{_user_label(target)} is already an admin.")
        return

    add_bot_admin(
        settings.database_path,
        telegram_user_id=target.id,
        telegram_username=target.username,
        display_name=_display_name(target),
    )
    await sync_bot_command_menu(context.bot, settings)
    await update.effective_message.reply_text(
        f"✅ {_user_label(target)} can now use all admin commands."
    )


async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    target = _resolve_target_user(update, context.args)
    if target is None:
        await update.effective_message.reply_text(
            "Remove admin access:\n"
            "• Reply to their message with /removeadmin\n"
            "• Or: /removeadmin <telegram_user_id>"
        )
        return

    if is_primary_admin(settings, target.id):
        await update.effective_message.reply_text(
            "The primary admin from ADMIN_CHAT_ID in .env cannot be removed here."
        )
        return

    if not remove_bot_admin(settings.database_path, target.id):
        await update.effective_message.reply_text(f"{_user_label(target)} is not a delegated admin.")
        return

    await revoke_bot_command_menu(context.bot, target.id)
    await update.effective_message.reply_text(
        f"❌ Removed admin access for {_user_label(target)}."
    )


async def admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    lines = ["👑 **Bot admins:**", ""]
    if settings.admin_chat_id is not None:
        lines.append(f"• Primary admin · id `{settings.admin_chat_id}` (from .env)")

    delegated = list_bot_admins(settings.database_path)
    if not delegated:
        lines.append("• No delegated admins yet.")
    else:
        for admin in delegated:
            label = _stored_user_label(admin.telegram_username, admin.display_name, admin.telegram_user_id)
            lines.append(f"• {label} · id `{admin.telegram_user_id}`")

    lines.extend(
        [
            "",
            "Reply to someone with /addadmin or /removeadmin to change access.",
        ]
    )
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


def _resolve_target_user(
    update: Update, args: list[str], *, database_path: str | None = None
):
    message = update.effective_message
    if message and message.reply_to_message:
        user = message.reply_to_message.from_user
        if user and not user.is_bot:
            return user

    if len(args) == 1:
        arg = args[0].strip()
        if arg.lstrip("-").isdigit():
            return _MinimalUser(int(arg))

        if database_path:
            match = USERNAME_TOKEN.match(arg)
            if match:
                username = match.group(1).lower()
                for link in list_links(database_path):
                    if (
                        link.telegram_username
                        and link.telegram_username.lower().lstrip("@") == username
                    ):
                        return _MinimalUser(
                            link.telegram_user_id,
                            username=link.telegram_username,
                            first_name=link.display_name or "",
                        )
                for entry in list_credo_whitelist(database_path):
                    if (
                        entry.telegram_username
                        and entry.telegram_username.lower().lstrip("@") == username
                    ):
                        return _MinimalUser(
                            entry.telegram_user_id,
                            username=entry.telegram_username,
                            first_name=entry.display_name or "",
                        )

    return None


class _MinimalUser:
    def __init__(
        self,
        user_id: int,
        *,
        username: str | None = None,
        first_name: str = "",
        last_name: str | None = None,
    ) -> None:
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name


def _display_name(user) -> str:
    parts = [getattr(user, "first_name", None) or "", getattr(user, "last_name", None) or ""]
    name = " ".join(part for part in parts if part).strip()
    return name or "Unknown"


def _user_label(user) -> str:
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    name = _display_name(user)
    if name != "Unknown":
        return name
    return str(user.id)


def _stored_user_label(username: str | None, display_name: str | None, user_id: int) -> str:
    if username:
        label = f"@{username.lstrip('@')}"
        if display_name:
            return f"{label} ({display_name})"
        return label
    if display_name:
        return display_name
    return str(user_id)
