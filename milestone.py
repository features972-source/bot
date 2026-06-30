"""Call milestone announcements — every 200 calls, post in group. Persisted to DB."""
from __future__ import annotations

import asyncio
import logging

from database import get_call_stats_totals, _get_bot_setting, _set_bot_setting
from notify import send_to_notify_chats

logger = logging.getLogger(__name__)

DB_MILESTONE_KEY = "last_announced_milestone"
CHECK_INTERVAL_SECONDS = 60
MILESTONE_INTERVAL = 200  # announce every 200 calls


def _milestone_message(count: int) -> str:
    if count >= 10000:
        return f"🏆🔥 <b>LEGENDARY! {count:,} CALLS!</b> 🔥🏆\n\nThe team is absolutely on fire. Unbelievable work 🐐"
    if count >= 5000:
        return f"👑 <b>{count:,} CALLS!</b> 👑\n\nThis team is elite 💪"
    if count >= 1000:
        return f"🎉🎊 <b>{count:,} CALLS!</b> 🎊🎉\n\nMassive milestone — the team is built different 💪🔥"
    return f"🎉 <b>Team just hit {count:,} calls!</b>\n\nGreat work everyone — keep it up! 📞🔥"


async def milestone_loop(bot, settings, bot_data: dict) -> None:
    """Check every minute if the team has hit a new 200-call milestone."""
    try:
        while True:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            try:
                total, _ = get_call_stats_totals(settings.database_path)

                # Load last announced from DB (survives redeploys)
                raw = _get_bot_setting(settings.database_path, DB_MILESTONE_KEY)
                last = int(raw) if raw and raw.isdigit() else 0

                # Next milestone to hit
                next_milestone = (last // MILESTONE_INTERVAL + 1) * MILESTONE_INTERVAL

                if total < next_milestone:
                    continue

                # Save to DB before posting to prevent double-post on redeploy
                _set_bot_setting(settings.database_path, DB_MILESTONE_KEY, str(next_milestone))

                await send_to_notify_chats(
                    bot,
                    settings,
                    bot_data,
                    text=_milestone_message(next_milestone),
                )
                logger.info("Milestone announced: %d calls", next_milestone)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Milestone check failed")

    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Milestone loop stopped")
