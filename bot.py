import asyncio
import atexit
import logging
import os
import sys

from telegram.error import Conflict
from telegram.ext import Application

from config import load_settings
from database import init_db
from call_control_listener import start_call_control_listener
from notify import (
    active_calls_digest_loop,
    ensure_telegram_send_worker,
    live_call_timers_loop,
)
from handlers.admin_access import sync_bot_command_menu
from handlers.bot_commands import build_bot_handlers
from handlers.mailer import build_mailer_handlers
from mailer_bridge import init_mailer_bridge
from threex_token import get_token_holder
from threex_ws import ASYNCIO_LOOP_KEY
from payments_excel_export import schedule_payments_excel_sync
from webhook_server import start_webhook_server

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
_instance_lock = None  # keeps single-instance file lock alive for process lifetime


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok and code.value == STILL_ACTIVE)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_single_instance_lock(database_path: str):
    """Hold an exclusive lock for the lifetime of this process."""
    lock_path = f"{database_path}.bot.lock"
    if os.path.exists(lock_path):
        try:
            with open(lock_path, encoding="utf-8") as existing:
                old_pid = int(existing.read().strip() or "0")
            if _pid_running(old_pid):
                print(
                    "Another 3cx-telegram-bot instance is already running "
                    f"(pid {old_pid}). Stop it first."
                )
                sys.exit(1)
        except (OSError, ValueError):
            pass
        try:
            os.remove(lock_path)
        except OSError:
            pass

    lock_file = open(lock_path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        print(
            "Another 3cx-telegram-bot instance is already running.\n"
            "Stop the other process first (only one instance should run)."
        )
        sys.exit(1)

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()

    def _release() -> None:
        try:
            lock_file.close()
        except OSError:
            pass
        try:
            os.remove(lock_path)
        except OSError:
            pass

    atexit.register(_release)
    return lock_file


def main() -> None:
    if sys.version_info >= (3, 10):
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    global _instance_lock
    settings = load_settings()
    from money_format import init_currency

    init_currency(settings.currency_symbol)
    init_db(settings.database_path)
    if not settings.skip_instance_lock:
        _instance_lock = _acquire_single_instance_lock(settings.database_path)

    async def on_startup(app: Application) -> None:
        app.bot_data[ASYNCIO_LOOP_KEY] = asyncio.get_running_loop()
        ensure_telegram_send_worker(app.bot_data)
        get_token_holder(app.bot_data, settings)
        asyncio.create_task(
            sync_bot_command_menu(app.bot, settings),
            name="sync-bot-command-menu",
        )
        asyncio.create_task(start_call_control_listener(settings, app.bot, app.bot_data))
        asyncio.create_task(live_call_timers_loop(app.bot, app.bot_data))
        asyncio.create_task(active_calls_digest_loop(app.bot, settings, app.bot_data))
        from handlers.ready_check import ready_check_shift_loop

        asyncio.create_task(
            ready_check_shift_loop(app.bot, settings, app.bot_data),
            name="ready-check-shift",
        )
        from handlers.credo import credo_reminder_loop

        asyncio.create_task(
            credo_reminder_loop(app.bot, settings, app.bot_data),
            name="credo-reminder-loop",
        )
        from onedrive_cloud_sync import remember_excel_web_url

        remember_excel_web_url(settings)
        schedule_payments_excel_sync(settings)
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
                "Telegram Conflict: another process is polling this bot token. Exiting."
            )
            raise SystemExit(1) from err
        logger.exception("Telegram handler error", exc_info=err)

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
    if settings.notify_chat_id is not None:
        tg_app.bot_data["notify_chat_id"] = settings.notify_chat_id

    for handler in build_mailer_handlers():
        tg_app.add_handler(handler, group=-1)

    for handler in build_bot_handlers():
        tg_app.add_handler(handler)

    loop = asyncio.get_event_loop()
    start_webhook_server(settings, tg_app.bot, tg_app.bot_data, loop)

    if settings.threex_enabled:
        print(f"3CX AI Call Control enabled for {settings.threex_fqdn}")
    if settings.cloud_deployed:
        base = settings.public_base_url or settings.listen_public_url
        print(f"Cloud deploy active. Public URL: {base}")
        print(f"Health check: {base}/health")
        print(f"MS Graph OAuth callback: {settings.ms_graph_redirect_uri}")
    print(
        f"Bot is running. 3CX webhook URL (optional):\n"
        f"http://YOUR_SERVER:{settings.webhook_port}/webhook/3cx/{settings.webhook_secret}"
    )
    print("Press Ctrl+C to stop.")
    tg_app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
        poll_interval=1.0,
        timeout=30,
        bootstrap_retries=5,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="3CX Telegram bot")
    parser.add_argument(
        "--env-file",
        default=None,
        help="Env file for a second instance (e.g. .env.bot2)",
    )
    cli_args = parser.parse_args()
    if cli_args.env_file:
        os.environ["BOT_ENV_FILE"] = cli_args.env_file
    main()
