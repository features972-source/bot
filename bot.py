import asyncio
import logging
import os
import sys

from bot_core import prepare_bot_runtime, run_bot_polling
from config import load_settings
from money_format import init_currency
from webhook_server import start_webhook_server

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def main() -> None:
    if sys.version_info >= (3, 10):
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    from local_run import assert_cloud_run_or_exit

    assert_cloud_run_or_exit()
    settings = load_settings()
    init_currency(settings.currency_symbol)
    runtime = prepare_bot_runtime(settings, instance_id="q1")

    loop = asyncio.get_event_loop()
    start_webhook_server(
        settings,
        runtime.application.bot,
        runtime.application.bot_data,
        loop,
    )

    if settings.threex_enabled:
        print(f"3CX AI Call Control enabled for {settings.threex_fqdn}")
    if settings.cloud_deployed:
        base = settings.public_base_url or settings.listen_public_url
        print(f"Cloud deploy active. Public URL: {base}")
        print(f"Health check: {base}/health")
    print("Press Ctrl+C to stop.")
    run_bot_polling(runtime)


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
