"""Run Q1 + Q2 on one Render service — separate data, same code deploy."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading

from bot_core import prepare_bot_runtime, run_bot_polling
from config import load_settings
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

    q1_settings = load_settings()
    init_currency(q1_settings.currency_symbol)

    runtimes = [prepare_bot_runtime(q1_settings, instance_id="q1")]
    q2_settings = load_settings("BOT2", optional=True)
    if q2_settings:
        runtimes.append(prepare_bot_runtime(q2_settings, instance_id="q2"))
        logger.info("Dual-bot mode: Q1 + Q2 on this service")
    else:
        logger.info("Single-bot mode: Q1 only (set BOT2_BOT_TOKEN to enable Q2)")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    start_multi_webhook_server(runtimes, loop)

    if q1_settings.cloud_deployed:
        base = q1_settings.public_base_url or q1_settings.listen_public_url
        logger.info("Cloud deploy active. Public URL: %s", base)
        logger.info("Health check: %s/health", base)

    if len(runtimes) == 1:
        run_bot_polling(runtimes[0])
        return

    for runtime in runtimes[1:]:
        thread = threading.Thread(
            target=run_bot_polling,
            args=(runtime,),
            name=f"bot-{runtime.instance_id}",
            daemon=True,
        )
        thread.start()
        logger.info("Started %s in background thread", runtime.settings.bot_display_name)

    run_bot_polling(runtimes[0])


if __name__ == "__main__":
    main()
