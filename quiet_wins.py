"""Private quiet-win DMs for linked agents."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telegram import Bot

from config import Settings
from database import (
    ExtensionLink,
    get_user_close_rates,
    get_user_handle_baseline,
    log_quiet_win,
    recent_quiet_win,
)
from notify import format_duration

logger = logging.getLogger(__name__)

MIN_PRIOR_CALLS = 5
MIN_CALL_SECONDS = 45
HANDLE_BEAT_RATIO = 0.92
RECENT_CLOSE_DAYS = 7
MIN_RECENT_CALLS = 3
MIN_PRIOR_CALLS_CLOSE = 5
CLOSE_RATE_MARGIN = 0.03
COOLDOWN_HOURS = 24


def _premium_bot(settings: Settings) -> bool:
    return "q1" in settings.bot_display_name.lower()


async def maybe_quiet_win_handle_time(
    bot: Bot,
    settings: Settings,
    link: ExtensionLink,
    duration_seconds: int,
) -> None:
    if not _premium_bot(settings):
        return
    if duration_seconds < MIN_CALL_SECONDS:
        return
    if recent_quiet_win(
        settings.database_path,
        link.telegram_user_id,
        "handle_time",
        within_hours=COOLDOWN_HOURS,
    ):
        return

    baseline = get_user_handle_baseline(
        settings.database_path,
        link.telegram_user_id,
        min_calls=MIN_PRIOR_CALLS,
        exclude_latest=True,
    )
    if baseline is None:
        return
    avg_seconds, sample_calls = baseline
    if duration_seconds >= int(avg_seconds * HANDLE_BEAT_RATIO):
        return

    saved = max(0, int(avg_seconds - duration_seconds))
    text = (
        "✨ <b>Quiet win</b> — you beat your average handle time.\n\n"
        f"This call: <b>{format_duration(duration_seconds)}</b>\n"
        f"Your avg ({sample_calls} calls): <b>{format_duration(int(avg_seconds))}</b>\n"
        f"Saved about <b>{format_duration(saved)}</b>."
    )
    try:
        await bot.send_message(
            chat_id=link.telegram_user_id,
            text=text,
            parse_mode="HTML",
            disable_notification=True,
        )
        log_quiet_win(
            settings.database_path,
            telegram_user_id=link.telegram_user_id,
            win_type="handle_time",
            detail=f"{duration_seconds}s vs avg {int(avg_seconds)}s",
        )
    except Exception:
        logger.exception(
            "Failed to send handle-time quiet win to user %s",
            link.telegram_user_id,
        )


async def maybe_quiet_win_close_rate(
    bot: Bot,
    settings: Settings,
    *,
    telegram_user_id: int,
    telegram_username: str | None = None,
    display_name: str | None = None,
) -> None:
    if not _premium_bot(settings):
        return
    if recent_quiet_win(
        settings.database_path,
        telegram_user_id,
        "close_rate",
        within_hours=COOLDOWN_HOURS,
    ):
        return

    now = datetime.now(timezone.utc)
    recent_since = now - timedelta(days=RECENT_CLOSE_DAYS)
    prior_calls, prior_payments, prior_rate = get_user_close_rates(
        settings.database_path,
        telegram_user_id,
        since=None,
        until=recent_since,
    )
    recent_calls, recent_payments, recent_rate = get_user_close_rates(
        settings.database_path,
        telegram_user_id,
        since=recent_since,
        until=None,
    )

    if prior_calls < MIN_PRIOR_CALLS_CLOSE or recent_calls < MIN_RECENT_CALLS:
        return
    if recent_rate <= prior_rate + CLOSE_RATE_MARGIN:
        return

    prior_pct = int(round(prior_rate * 100))
    recent_pct = int(round(recent_rate * 100))
    text = (
        "✨ <b>Quiet win</b> — your close rate is up.\n\n"
        f"Last {RECENT_CLOSE_DAYS} days: <b>{recent_pct}%</b> "
        f"({recent_payments}/{recent_calls} calls)\n"
        f"Your usual: <b>{prior_pct}%</b> "
        f"({prior_payments}/{prior_calls} calls)."
    )
    try:
        await bot.send_message(
            chat_id=telegram_user_id,
            text=text,
            parse_mode="HTML",
            disable_notification=True,
        )
        log_quiet_win(
            settings.database_path,
            telegram_user_id=telegram_user_id,
            win_type="close_rate",
            detail=f"{recent_pct}% vs usual {prior_pct}%",
        )
    except Exception:
        logger.exception(
            "Failed to send close-rate quiet win to user %s",
            telegram_user_id,
        )
