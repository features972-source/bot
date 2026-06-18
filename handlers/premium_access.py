"""Q1 Premium user list and quiet-win eligibility."""

from __future__ import annotations

import re

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from handlers.chat_scope import PM_ONLY

from config import Settings
from database import (
    add_q1_premium_user,
    list_q1_premium_users,
    remove_q1_premium_user,
)
from handlers.admin_access import _display_name, require_admin

USERNAME_TOKEN = re.compile(r"^@?([A-Za-z0-9_]{4,})$", re.IGNORECASE)


def build_premium_access_handlers() -> list:
    return [
        CommandHandler("addpremium", addpremium_command, filters=PM_ONLY),
        CommandHandler("removepremium", removepremium_command, filters=PM_ONLY),
        CommandHandler("premiumusers", premiumusers_command, filters=PM_ONLY),
    ]


async def addpremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    message = update.effective_message
    user = update.effective_user
    if not message or not user:
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
    elif context.args:
        match = USERNAME_TOKEN.match(context.args[0].strip())
        if match:
            target_username = match.group(1)
            await message.reply_text(
                f"To add @{target_username}, reply to one of their messages with /addpremium."
            )
            return

    if target_id is None:
        await message.reply_text(
            "Reply to a user's message with /addpremium, or use /addpremium &lt;telegram_user_id&gt;.",
            parse_mode="HTML",
        )
        return

    add_q1_premium_user(
        settings.database_path,
        telegram_user_id=target_id,
        telegram_username=target_username,
        display_name=target_display,
    )
    label = f"@{target_username}" if target_username else (target_display or str(target_id))
    await message.reply_text(f"Added {label} to Q1 Premium (quiet wins enabled).")


async def removepremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
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
            "Reply to a user with /removepremium, or use /removepremium &lt;telegram_user_id&gt;.",
            parse_mode="HTML",
        )
        return

    if remove_q1_premium_user(settings.database_path, target_id):
        await message.reply_text(f"Removed user {target_id} from Q1 Premium.")
    else:
        await message.reply_text("That user is not on the Q1 Premium list.")


async def premiumusers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    entries = list_q1_premium_users(settings.database_path)
    env_ids = sorted(settings.q1_premium_user_ids)
    lines = ["<b>Q1 Premium users</b> (quiet wins)\n"]
    if env_ids:
        lines.append("<b>From .env:</b> " + ", ".join(str(uid) for uid in env_ids))
    if entries:
        lines.append("<b>In database:</b>")
        for entry in entries:
            label = (
                f"@{entry.telegram_username}"
                if entry.telegram_username
                else (entry.display_name or str(entry.telegram_user_id))
            )
            lines.append(f"• {label} (<code>{entry.telegram_user_id}</code>)")
    elif not env_ids:
        lines.append("No premium users yet. Reply to someone with /addpremium.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")
