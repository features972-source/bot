"""Build and run one Telegram bot instance (shared by bot.py and bot_cloud.py)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass

from telegram.error import Conflict
from telegram.ext import Application

from bot_message_format import apply_bot_bold_patch
from instance_lock import acquire_single_instance_lock
from instance_registry import register_instance, register_bot
from call_control_listener import start_call_control_listener
from config import Settings
from database import get_notify_chat_id, init_db, set_notify_chat_id
from handlers.admin_access import sync_bot_command_menu
from handlers.admin_panel import build_admin_handlers
from handlers.bot_commands import build_bot_handlers, build_credo_bot_handlers
from handlers.credo import build_add_card_handlers, build_credo_active_guard_handlers
from handlers.credo_subscription import build_credo_subscription_handlers
from handlers.mailer import build_mailer_handlers
from handlers.payments import build_payment_message_handlers
from handlers.ready_check import build_ready_check_handlers, ready_check_shift_loop
from mailer_bridge import init_mailer_bridge
from notify import (
    active_calls_digest_loop,
    daily_summary_loop,
    ensure_telegram_send_worker,
)
from queue_alert import queue_alert_loop
from milestone import milestone_loop
from handlers.blast import blast_content_trigger
from payments_excel_export import schedule_payments_excel_sync
from threex_token import get_token_holder
from threex_ws import ASYNCIO_LOOP_KEY

logger = logging.getLogger(__name__)


@dataclass
class BotRuntime:
    instance_id: str
    settings: Settings
    application: Application
    notify_chat_id: int | None


def prepare_bot_runtime(settings: Settings, *, instance_id: str) -> BotRuntime:
    apply_bot_bold_patch()
    if settings.cloud_deployed and not settings.persistent_data:
        logger.warning(
            "[%s] DATA NOT PERSISTENT — database is at %s.",
            instance_id,
            settings.database_path,
        )
    elif settings.persistent_data:
        logger.info("[%s] Using persistent database at %s", instance_id, settings.database_path)

    init_db(settings.database_path)
    stored_notify_chat_id = get_notify_chat_id(settings.database_path)
    if stored_notify_chat_id is not None:
        runtime_notify_chat_id = stored_notify_chat_id
    elif settings.notify_chat_id is not None:
        runtime_notify_chat_id = settings.notify_chat_id
        set_notify_chat_id(settings.database_path, settings.notify_chat_id)
    else:
        runtime_notify_chat_id = None

    if not settings.skip_instance_lock:
        acquire_single_instance_lock(settings.database_path)

    register_instance(instance_id, settings)

    async def on_startup(app: Application) -> None:
        app.bot_data[ASYNCIO_LOOP_KEY] = asyncio.get_running_loop()
        app.bot_data["instance_id"] = instance_id
        register_bot(instance_id, app.bot)
        ensure_telegram_send_worker(app.bot_data)
        get_token_holder(app.bot_data, settings)
        asyncio.create_task(
            sync_bot_command_menu(app.bot, settings),
            name=f"sync-bot-command-menu-{instance_id}",
        )
        asyncio.create_task(
            start_call_control_listener(settings, app.bot, app.bot_data),
            name=f"call-control-{instance_id}",
        )
        asyncio.create_task(
            active_calls_digest_loop(app.bot, settings, app.bot_data),
            name=f"active-calls-{instance_id}",
        )
        asyncio.create_task(
            daily_summary_loop(app.bot, settings, app.bot_data),
            name=f"daily-summary-{instance_id}",
        )
        asyncio.create_task(
            queue_alert_loop(app.bot, settings, app.bot_data),
            name=f"queue-alert-{instance_id}",
        )
        asyncio.create_task(
            milestone_loop(app.bot, settings, app.bot_data),
            name=f"milestone-{instance_id}",
        )
        from handlers.credo import credo_reminder_loop
        from handlers.nemesis import nemesis_loop

        asyncio.create_task(
            credo_reminder_loop(app.bot, settings, app.bot_data),
            name=f"credo-reminder-{instance_id}",
        )
        asyncio.create_task(
            nemesis_loop(app.bot, settings, app.bot_data),
            name=f"nemesis-{instance_id}",
        )
        asyncio.create_task(
            ready_check_shift_loop(app.bot, settings, app.bot_data),
            name=f"ready-check-{instance_id}",
        )
        from onedrive_cloud_sync import remember_excel_web_url

        remember_excel_web_url(settings)
        schedule_payments_excel_sync(settings)
        if settings.mailer_bridge_enabled:
            await init_mailer_bridge(settings, app.bot_data, app.bot)

    async def on_shutdown(app: Application) -> None:
        from mailer_bridge import get_mailer_bridge

        bridge = get_mailer_bridge(app.bot_data)
        if bridge is not None:
            await bridge.disconnect()

    async def on_error(update: object, context) -> None:
        err = context.error
        if isinstance(err, Conflict):
            logger.critical(
                "[%s] Telegram Conflict: another process is polling this bot token.",
                instance_id,
            )
            raise SystemExit(1) from err
        logger.exception("[%s] Telegram handler error", instance_id, exc_info=err)

    tg_app = (
        Application.builder()
        .token(settings.bot_token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .concurrent_updates(32)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    tg_app.add_error_handler(on_error)
    tg_app.bot_data["settings"] = settings
    if runtime_notify_chat_id is not None:
        tg_app.bot_data["notify_chat_id"] = runtime_notify_chat_id

    for handler in build_add_card_handlers():
        tg_app.add_handler(handler, group=-1)
    for handler in build_payment_message_handlers():
        tg_app.add_handler(handler, group=-1)
    for handler in build_mailer_handlers():
        tg_app.add_handler(handler, group=-1)
    for handler in build_admin_handlers():
        tg_app.add_handler(handler)
    for handler in build_ready_check_handlers():
        tg_app.add_handler(handler)
    for handler in build_bot_handlers():
        tg_app.add_handler(handler)
    from telegram.ext import MessageHandler, filters as tg_filters
    tg_app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, blast_content_trigger), group=-3)

    return BotRuntime(
        instance_id=instance_id,
        settings=settings,
        application=tg_app,
        notify_chat_id=runtime_notify_chat_id,
    )


def prepare_credo_runtime(settings: Settings, *, instance_id: str) -> BotRuntime:
    """Minimal runtime for the credo-only bot (cc commands, no 3CX/payments/mailer)."""
    apply_bot_bold_patch()
    if settings.cloud_deployed and not settings.persistent_data:
        logger.warning(
            "[%s] DATA NOT PERSISTENT — database is at %s.",
            instance_id,
            settings.database_path,
        )
    elif settings.persistent_data:
        logger.info("[%s] Using persistent database at %s", instance_id, settings.database_path)

    init_db(settings.database_path)

    if not settings.skip_instance_lock:
        acquire_single_instance_lock(settings.database_path)

    register_instance(instance_id, settings)

    async def on_startup(app: Application) -> None:
        app.bot_data["instance_id"] = instance_id
        register_bot(instance_id, app.bot)
        ensure_telegram_send_worker(app.bot_data)
        asyncio.create_task(
            sync_bot_command_menu(app.bot, settings),
            name=f"sync-bot-command-menu-{instance_id}",
        )
        from handlers.credo import credo_reminder_loop

        asyncio.create_task(
            credo_reminder_loop(app.bot, settings, app.bot_data),
            name=f"credo-reminder-{instance_id}",
        )

    async def on_error(update: object, context) -> None:
        err = context.error
        if isinstance(err, Conflict):
            logger.critical(
                "[%s] Telegram Conflict: another process is polling this bot token.",
                instance_id,
            )
            raise SystemExit(1) from err
        logger.exception("[%s] Telegram handler error", instance_id, exc_info=err)

    tg_app = (
        Application.builder()
        .token(settings.bot_token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .concurrent_updates(32)
        .post_init(on_startup)
        .build()
    )
    tg_app.add_error_handler(on_error)
    tg_app.bot_data["settings"] = settings

    for handler in build_add_card_handlers():
        tg_app.add_handler(handler, group=-1)
    for handler in build_credo_subscription_handlers():
        tg_app.add_handler(handler, group=-2)
    for handler in build_credo_active_guard_handlers():
        tg_app.add_handler(handler, group=-1)
    for handler in build_credo_bot_handlers():
        tg_app.add_handler(handler)

    return BotRuntime(
        instance_id=instance_id,
        settings=settings,
        application=tg_app,
        notify_chat_id=None,
    )


def run_bot_polling(runtime: BotRuntime) -> None:
    settings = runtime.settings
    label = settings.bot_display_name
    if settings.threex_enabled:
        logger.info("%s: 3CX Call Control enabled for %s", label, settings.threex_fqdn)
    logger.info("%s: polling started", label)
    runtime.application.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
        poll_interval=1.0,
        timeout=30,
        bootstrap_retries=5,
        close_loop=False,
    )
