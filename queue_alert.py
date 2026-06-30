"""Queue pile-up alert — post in group if too many calls go unanswered in a short window."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from database import count_missed_calls
from notify import send_to_notify_chats

logger = logging.getLogger(__name__)

QUEUE_ALERT_KEY = "queue_alert_last_posted"
CHECK_INTERVAL_SECONDS = 60
MISSED_THRESHOLD = 3          # how many missed calls triggers the alert
WINDOW_MINUTES = 5            # within how many minutes
COOLDOWN_MINUTES = 10         # don't re-alert more than once per this period


async def queue_alert_loop(bot, settings, bot_data: dict) -> None:
    """Check every minute if calls are piling up unanswered and alert the group."""
    try:
        while True:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            try:
                since = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)
                missed = count_missed_calls(settings.database_path, since=since)

                if missed < MISSED_THRESHOLD:
                    continue

                # Don't spam — enforce cooldown
                last_posted = bot_data.get(QUEUE_ALERT_KEY)
                if last_posted is not None:
                    age = (datetime.now(timezone.utc) - last_posted).total_seconds()
                    if age < COOLDOWN_MINUTES * 60:
                        continue

                bot_data[QUEUE_ALERT_KEY] = datetime.now(timezone.utc)
                await send_to_notify_chats(
                    bot,
                    settings,
                    bot_data,
                    text=(
                        f"📵 <b>Queue alert</b> — {missed} calls unanswered in the last {WINDOW_MINUTES} mins!\n"
                        f"Someone pick up the phones 🔔"
                    ),
                )
                logger.info("Posted queue alert: %d missed calls in %d mins", missed, WINDOW_MINUTES)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Queue alert check failed")

    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Queue alert loop stopped")
