"""License keys and subscription gate for the credo-only bot."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Settings
from database import (
    ADMIN_LICENSE_DAYS,
    ADMIN_LICENSE_WEEKS,
    create_admin_license_key,
    extend_credo_subscription,
    get_credo_subscription_active_until,
    get_delegated_admin_expires_at,
    is_credo_subscription_active,
    list_unredeemed_admin_license_keys,
    redeem_admin_license_key,
)
from handlers.admin_access import is_primary_admin

logger = logging.getLogger(__name__)

AWAITING_LICENSE_KEY = "credo_awaiting_license_key"


def build_credo_subscription_handlers() -> list:
    return [
        CommandHandler("genkey", genkey_command),
        CommandHandler("redeemkey", redeemkey_command),
        CommandHandler("subscription", subscription_command),
        CommandHandler("keys", keys_command),
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            license_key_entry,
            block=False,
        ),
        MessageHandler(filters.ALL, subscription_guard, block=False),
        CallbackQueryHandler(subscription_guard, block=False),
    ]


async def prompt_for_license_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[AWAITING_LICENSE_KEY] = True
    await update.effective_message.reply_text(
        f"💳 <b>Welcome</b>\n\n"
        f"Send your license key to activate <b>{ADMIN_LICENSE_WEEKS} weeks</b> of admin access.\n\n"
        "The key looks like: <code>credo-xxxxxxxxxxxx</code>",
        parse_mode="HTML",
    )


async def license_key_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not settings.credo_only_mode:
        return

    user = update.effective_user
    message = update.effective_message
    if not user or not message or not message.text:
        return
    if is_primary_admin(settings, user.id):
        return

    awaiting = context.user_data.get(AWAITING_LICENSE_KEY)
    text = message.text.strip()
    if not awaiting and not text.lower().startswith("credo-"):
        return

    try:
        subscription_until, admin_until = redeem_admin_license_key(
            settings.database_path,
            key=text,
            redeemed_by_user_id=user.id,
            grant_admin=True,
            telegram_username=user.username,
            display_name=_display_name(user),
        )
    except ValueError:
        await message.reply_text(
            "Invalid or already used key.\n\nSend /start to try again."
        )
        context.user_data.pop(AWAITING_LICENSE_KEY, None)
        return

    context.user_data.pop(AWAITING_LICENSE_KEY, None)
    from handlers.admin_access import sync_bot_command_menu

    await sync_bot_command_menu(context.bot, settings)
    await message.reply_text(
        f"✅ Admin access activated until "
        f"{admin_until.astimezone().strftime('%d %b %Y')}.\n"
        f"Bot active until {subscription_until.astimezone().strftime('%d %b %Y')}.\n\n"
        "Send /help for commands."
    )


def _format_until(dt: datetime | None) -> str:
    if dt is None:
        return "not set (open)"
    return dt.astimezone(timezone.utc).strftime("%d %b %Y %H:%M UTC")


async def subscription_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not settings.credo_only_mode:
        return

    user = update.effective_user
    if not user:
        return
    if is_primary_admin(settings, user.id):
        return
    if context.user_data.get(AWAITING_LICENSE_KEY):
        return

    message = update.effective_message
    if message and message.text:
        text = message.text.strip()
        if text.startswith("/start"):
            return
        if text.lower().startswith("credo-"):
            return

    if is_credo_subscription_active(settings.database_path):
        return

    if message:
        await message.reply_text(
            "This bot's subscription has expired.\n\n"
            "Ask the owner to generate a new key with /genkey and redeem it."
        )
    elif update.callback_query:
        await update.callback_query.answer(
            "Bot subscription expired.",
            show_alert=True,
        )
    raise ApplicationHandlerStop


async def genkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not settings.credo_only_mode:
        return
    if not is_primary_admin(settings, update.effective_user.id if update.effective_user else 0):
        await update.effective_message.reply_text("Only the primary admin can generate keys.")
        return

    user = update.effective_user
    key = create_admin_license_key(
        settings.database_path,
        created_by_user_id=user.id,
    )
    await update.effective_message.reply_text(
        f"New admin key (single use, {ADMIN_LICENSE_WEEKS} weeks):\n\n"
        f"`{key}`\n\n"
        f"Share it with them — they DM the bot, send /start, then paste the key.\n\n"
        f"Or extend bot only (owner): `/redeemkey {key}`",
        parse_mode="Markdown",
    )


async def redeemkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not settings.credo_only_mode:
        return
    if not is_primary_admin(settings, update.effective_user.id if update.effective_user else 0):
        await update.effective_message.reply_text("Only the primary admin can redeem keys.")
        return

    if not context.args:
        await update.effective_message.reply_text("Usage: /redeemkey <key>")
        return

    key = context.args[0].strip()
    user = update.effective_user
    try:
        subscription_until, _ = redeem_admin_license_key(
            settings.database_path,
            key=key,
            redeemed_by_user_id=user.id,
            grant_admin=False,
        )
    except ValueError:
        await update.effective_message.reply_text("Invalid or already used key.")
        return

    await update.effective_message.reply_text(
        f"Bot extended for {ADMIN_LICENSE_WEEKS} weeks.\n"
        f"Active until: {_format_until(subscription_until)}"
    )


async def subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not settings.credo_only_mode:
        return

    user = update.effective_user
    if not user:
        return

    active_until = get_credo_subscription_active_until(settings.database_path)
    active = is_credo_subscription_active(settings.database_path)
    status = "active" if active else "expired"

    lines = [
        f"Bot subscription: **{status}**",
        f"Active until: {_format_until(active_until)}",
        "",
        f"Each key adds **{ADMIN_LICENSE_WEEKS} weeks** ({ADMIN_LICENSE_DAYS} days).",
    ]

    if is_primary_admin(settings, user.id):
        pending = len(list_unredeemed_admin_license_keys(settings.database_path))
        lines.extend(["", f"Unused keys: {pending}", "Generate: /genkey"])
    else:
        admin_until = get_delegated_admin_expires_at(settings.database_path, user.id)
        if admin_until:
            lines.extend(["", f"Your admin access until: {_format_until(admin_until)}"])

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


async def keys_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not settings.credo_only_mode:
        return
    if not is_primary_admin(settings, update.effective_user.id if update.effective_user else 0):
        await update.effective_message.reply_text("Only the primary admin can list keys.")
        return

    pending = list_unredeemed_admin_license_keys(settings.database_path)
    if not pending:
        await update.effective_message.reply_text("No unused keys. Use /genkey to create one.")
        return

    lines = [f"Unused keys ({len(pending)}):", ""]
    for entry in pending[:20]:
        created = _format_until(_parse_iso(entry.created_at))
        lines.append(f"• …{entry.key_hint} · created {created}")
    if len(pending) > 20:
        lines.append(f"… and {len(pending) - 20} more")
    await update.effective_message.reply_text("\n".join(lines))


def _parse_iso(raw: str) -> datetime | None:
    try:
        text = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def redeem_key_for_admin(
    settings: Settings,
    *,
    key: str,
    target_user,
) -> tuple[datetime, datetime]:
    subscription_until, admin_until = redeem_admin_license_key(
        settings.database_path,
        key=key,
        redeemed_by_user_id=target_user.id,
        grant_admin=True,
        telegram_username=getattr(target_user, "username", None),
        display_name=_display_name(target_user),
    )
    if admin_until is None:
        raise ValueError("admin_grant_failed")
    return subscription_until, admin_until


def _display_name(user) -> str:
    parts = [getattr(user, "first_name", None) or "", getattr(user, "last_name", None) or ""]
    name = " ".join(part for part in parts if part).strip()
    return name or "Unknown"
