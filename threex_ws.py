"""Shared helpers for the 3CX Call Control WebSocket connection."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

SUBSCRIBE_QUEUE_KEY = "threex_subscribe_queue"
ASYNCIO_LOOP_KEY = "asyncio_loop"


def request_extension_subscribe_sync(bot_data: dict, extension: str, *, wait_seconds: float = 1.0) -> None:
    """Ask the Call Control WebSocket to subscribe to an extension before streaming."""
    loop = bot_data.get(ASYNCIO_LOOP_KEY)
    queue = bot_data.get(SUBSCRIBE_QUEUE_KEY)
    if loop is None or queue is None:
        return
    try:
        future = asyncio.run_coroutine_threadsafe(queue.put(str(extension)), loop)
        future.result(timeout=5.0)
    except Exception as exc:
        logger.warning("Could not queue 3CX subscribe for ext %s: %s", extension, exc)
        return
    if wait_seconds > 0:
        import time

        time.sleep(wait_seconds)
