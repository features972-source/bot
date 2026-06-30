"""Call milestone announcements — post in group when team hits 100, 250, 500, 1000 calls etc."""
from __future__ import annotations

import asyncio
import logging

from database import get_call_stats_totals
from notify import send_to_notify_chats

logger = logging.getLogger(__name__)

MILESTONE_KEY = "last_announced_milestone"
CHECK_INTERVAL_SECONDS = 30

MILESTONES = [
    50, 100, 150, 200, 250, 300, 400, 500, 750,
    1000, 1500, 2000, 2500, 3000, 4000, 5000,
    7500, 10000,
]


def _milestone_message(count: int) -> str:
    if count >= 10000:
        return f"🏆🔥 <b>LEGENDARY! {count:,} CALLS!</b> 🔥🏆\n\nThe team is absolutely on fire. Unbelievable work 🐐"
    if count >= 5000:
        return f"👑 <b>5,000 CALLS!</b> 👑\n\nHalf way to 10K — this team is elite 💪"
    if count >= 1000:
        return f"🎉🎊 <b>{count:,} CALLS!</b> 🎊🎉\n\nMassive milestone — the team is built different 💪🔥"
    if count >= 500:
        return f"🚀 <b>Team just hit {count:,} calls!</b> 🚀\n\nHalfway to 1,000 — let's keep pushing! 💪"
    if count >= 100:
        return f"🎉 <b>Team just hit {count:,} calls!</b>\n\nGreat work everyone — keep it up! 📞🔥"
    return f"⭐ <b>{count:,} calls done!</b> The team is building momentum 📈"


async def milestone_loop(bot, settings, bot_data: dict) -> None:
    """Check every 30 seconds if the team has hit a new call milestone."""
    try:
        while True:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            try:
                total, _ = get_call_stats_totals(settings.database_path)
                last = bot_data.get(MILESTONE_KEY, 0)

                # Find highest milestone hit that hasn't been announced yet
                hit = None
                for m in MILESTONES:
                    if total >= m > last:
                        hit = m

                if hit is None:
                    continue

                bot_data[MILESTONE_KEY] = hit
                await send_to_notify_chats(
                    bot,
                    settings,
                    bot_data,
                    text=_milestone_message(hit),
                )
                logger.info("Milestone announced: %d calls", hit)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Milestone check failed")

    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Milestone loop stopped")
