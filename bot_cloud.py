"""Run one bot instance on Render — deploy Q1, Q2, etc. as separate Web Services."""

from __future__ import annotations

import asyncio
import logging
import sys

from bot_core import prepare_bot_runtime, run_bot_polling
from config import load_settings, resolve_instance_id
from money_format import init_currency
from webhook_server import start_multi_webhook_server

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
    instance_id = resolve_instance_id(
        database_path=settings.database_path,
        bot_display_name=settings.bot_display_name,
    )
    runtime = prepare_bot_runtime(settings, instance_id=instance_id)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    start_multi_webhook_server([runtime], loop)

    if settings.cloud_deployed:
        base = settings.public_base_url or settings.listen_public_url
        logger.info(
            "Cloud deploy active (%s / %s). Public URL: %s",
            settings.bot_display_name,
            instance_id,
            base,
        )
        logger.info("Health check: %s/health", base)

    run_bot_polling(runtime)


if __name__ == "__main__":
    main()
