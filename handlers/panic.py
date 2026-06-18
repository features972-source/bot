"""Emergency /panic — wipe all bot data after explicit confirmation."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import Bot, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import CommandHandler, ContextTypes
from handlers.chat_scope import PM_ONLY

from config import Settings
from database import (
    get_notify_chat_id,
    get_payment_notify_chat_id,
    get_payment_notify_message_id,
    summarize_bot_data,
)
from handlers.admin_access import is_primary_admin
from instance_registry import list_bots, list_instances
from panic_wipe import wipe_extra_paths_from_env, wipe_instance_storage

logger = logging.getLogger(__name__)

PENDING_PANIC_KEY = "pending_panic"
PANIC_CONFIRM_TEXT = "PANIC"


@dataclass(frozen=True)
class PendingPanic:
    admin_user_id: int


def build_panic_handlers() -> list:
    return [
        CommandHandler("panic", panic_command, filters=PM_ONLY),
    ]


def _pending_panic_map(bot_data: dict) -> dict[tuple[int, int], PendingPanic]:
    return bot_data.setdefault(PENDING_PANIC_KEY, {})


def _format_panic_summary(settings_list: list[Settings]) -> str:
    lines: list[str] = []
    for settings in settings_list:
        stats = summarize_bot_data(settings.database_path)
        payments = stats.get("payment_outs", 0)
        links = stats.get("extension_links", 0)
        credo = stats.get("credo_credit_cards", 0)
        lines.append(
            f"• <b>{settings.bot_display_name}</b>: "
            f"{payments} payment(s), {links} link(s), {credo} credo card(s)"
        )
    return "\n".join(lines)


async def panic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    if not is_primary_admin(settings, user.id):
        await message.reply_text(
            "Primary admin only. /panic wipes every bot instance and leaves configured groups."
        )
        return

    all_settings = [item[1] for item in list_instances()] or [settings]
    summary = _format_panic_summary(all_settings)
    prompt = await message.reply_text(
        "🚨 <b>EMERGENCY PANIC</b>\n\n"
        "This permanently:\n"
        "• Wipes <b>all</b> bot data (payments, links, credo, admins, settings)\n"
        "• Deletes live payment reports and leaves notify / payment groups\n"
        "• Clears cloud backups and exports on this server\n\n"
        f"{summary}\n\n"
        f"Reply to this message with <code>{PANIC_CONFIRM_TEXT}</code> "
        "(all capitals) to confirm.\n\n"
        "To wipe local copies on your laptop too, run "
        "<code>scripts\\panic-local-wipe.ps1</code> after this.",
        parse_mode="HTML",
    )
    _pending_panic_map(context.bot_data)[(message.chat_id, prompt.message_id)] = (
        PendingPanic(admin_user_id=user.id)
    )


async def _cleanup_telegram_for_instance(
    bot: Bot,
    database_path: str,
    *,
    bot_name: str,
) -> list[str]:
    actions: list[str] = []
    payment_chat_id = get_payment_notify_chat_id(database_path)
    payment_message_id = get_payment_notify_message_id(database_path)
    notify_chat_id = get_notify_chat_id(database_path)

    if payment_chat_id is not None and payment_message_id is not None:
        try:
            await bot.delete_message(payment_chat_id, payment_message_id)
            actions.append(f"{bot_name}: deleted live payment report")
        except BadRequest:
            actions.append(f"{bot_name}: live payment report already gone")
        except TelegramError as exc:
            logger.warning("Panic delete payment report failed: %s", exc)

    for chat_id in {cid for cid in (payment_chat_id, notify_chat_id) if cid is not None}:
        try:
            await bot.leave_chat(chat_id)
            actions.append(f"{bot_name}: left group {chat_id}")
        except BadRequest as exc:
            actions.append(f"{bot_name}: could not leave {chat_id} ({exc.message})")
        except TelegramError as exc:
            logger.warning("Panic leave_chat failed for %s: %s", chat_id, exc)

    return actions


async def _execute_panic(context: ContextTypes.DEFAULT_TYPE) -> str:
    telegram_actions: list[str] = []
    storage_actions: list[str] = []

    bots = list_bots()
    instances = list_instances()
    if not instances:
        instances = [("local", context.bot_data["settings"])]

    if bots:
        for _instance_id, bot, instance_settings in bots:
            telegram_actions.extend(
                await _cleanup_telegram_for_instance(
                    bot,
                    instance_settings.database_path,
                    bot_name=instance_settings.bot_display_name,
                )
            )
    else:
        settings: Settings = context.bot_data["settings"]
        telegram_actions.extend(
            await _cleanup_telegram_for_instance(
                context.bot,
                settings.database_path,
                bot_name=settings.bot_display_name,
            )
        )

    seen_db_paths: set[str] = set()
    for _instance_id, instance_settings in instances:
        db_path = instance_settings.database_path
        if db_path in seen_db_paths:
            continue
        seen_db_paths.add(db_path)
        storage_actions.extend(wipe_instance_storage(instance_settings))

    storage_actions.extend(wipe_extra_paths_from_env())
    context.bot_data.pop("notify_chat_id", None)

    lines = telegram_actions + storage_actions
    if not lines:
        return "Panic complete — nothing was stored."
    return "Panic complete:\n" + "\n".join(f"• {line}" for line in lines)


async def try_complete_pending_panic(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    user = update.effective_user
    if message is None or not message.text or message.reply_to_message is None:
        return False
    if user is None:
        return False

    reply = message.reply_to_message
    if not reply.from_user or not reply.from_user.is_bot:
        return False

    key = (message.chat_id, reply.message_id)
    pending = _pending_panic_map(context.bot_data).pop(key, None)
    if pending is None:
        return False

    settings: Settings = context.bot_data["settings"]
    if user.id != pending.admin_user_id:
        _pending_panic_map(context.bot_data)[key] = pending
        await message.reply_text("Only the admin who started /panic can confirm.")
        return True

    if not is_primary_admin(settings, user.id):
        await message.reply_text("Primary admin only.")
        return True

    if message.text.strip() != PANIC_CONFIRM_TEXT:
        _pending_panic_map(context.bot_data)[key] = pending
        await message.reply_text(
            f"Not confirmed. Reply to the warning with {PANIC_CONFIRM_TEXT} "
            "(all capitals) to wipe everything."
        )
        return True

    try:
        result = await _execute_panic(context)
    except Exception:
        logger.exception("Panic wipe failed")
        await message.reply_text(
            "Panic failed partway through — check Render logs. "
            "Some data may already be deleted."
        )
        return True

    await message.reply_text(result)
    logger.critical(
        "PANIC executed by user %s in chat %s",
        user.id,
        message.chat_id,
    )
    return True
