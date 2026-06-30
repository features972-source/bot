"""Queue pile-up alert — post in group when too many callers are ringing at once."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from notify import send_to_notify_chats
from threex_api import ThreeCXClient, is_inbound_ringing_participant
from database import list_links

logger = logging.getLogger(__name__)

QUEUE_ALERT_KEY = "queue_alert_last_posted"
CHECK_INTERVAL_SECONDS = 15   # check every 15 seconds
QUEUE_THRESHOLD = 3           # alert when this many callers are ringing simultaneously
COOLDOWN_SECONDS = 120        # don't re-alert for 2 minutes


async def _count_ringing(settings, bot_data: dict) -> int:
    """Count how many inbound callers are currently ringing across all linked extensions."""
    from threex_token import get_token_holder
    holder = get_token_holder(bot_data, settings)
    token = await holder.get()
    if not token:
        return 0

    links = list_links(settings.database_path)
    ringing_callids: set[int] = set()

    async with ThreeCXClient(settings.threex_fqdn, token) as client:
        for link in links:
            try:
                participants = await client.list_participants(link.extension)
                for p in participants:
                    if is_inbound_ringing_participant(p, extension=link.extension):
                        callid = p.get("callid") or p.get("CallId") or p.get("call_id")
                        if callid:
                            ringing_callids.add(int(callid))
            except Exception:
                pass

    return len(ringing_callids)


async def queue_alert_loop(bot, settings, bot_data: dict) -> None:
    """Alert the group when queue depth exceeds threshold."""
    if not getattr(settings, "threex_enabled", False):
        return
    try:
        while True:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            try:
                ringing = await _count_ringing(settings, bot_data)
                if ringing < QUEUE_THRESHOLD:
                    continue

                last_posted = bot_data.get(QUEUE_ALERT_KEY)
                if last_posted is not None:
                    age = (datetime.now(timezone.utc) - last_posted).total_seconds()
                    if age < COOLDOWN_SECONDS:
                        continue

                bot_data[QUEUE_ALERT_KEY] = datetime.now(timezone.utc)
                await send_to_notify_chats(
                    bot,
                    settings,
                    bot_data,
                    text=(
                        f"� <b>{ringing} people in the queue!</b>\n"
                        f"Answer the phones now �"
                    ),
                )
                logger.info("Posted queue alert: %d callers ringing", ringing)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Queue alert check failed")

    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Queue alert loop stopped")
