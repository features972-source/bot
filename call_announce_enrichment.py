"""Extra lines for ON CALL / CALL ENDED group posts."""

from __future__ import annotations

from datetime import datetime

from database import count_user_calls_since
from handlers.stats_period import _parse_stats_period
from payment_day_celebration import format_calls_celebration_line

LONG_CALL_WARNING_SECONDS = 15 * 60


def _today_since() -> datetime:
    since, _ = _parse_stats_period([])
    return since


def compute_call_number_today(path: str, telegram_user_id: int) -> int:
    """Nth call today including the one now starting."""
    since = _today_since()
    return count_user_calls_since(
        path,
        telegram_user_id=telegram_user_id,
        since=since,
    ) + 1


def calls_completed_today(path: str, telegram_user_id: int) -> int:
    since = _today_since()
    return count_user_calls_since(
        path,
        telegram_user_id=telegram_user_id,
        since=since,
    )


def calls_celebration_after_end(path: str, telegram_user_id: int) -> tuple[int, str | None]:
    """Call count for today after hangup, plus optional celebration line."""
    calls = calls_completed_today(path, telegram_user_id)
    return calls, format_calls_celebration_line(calls)


def on_call_title(call_number_today: int | None, *, long_warning: bool = False) -> str:
    if call_number_today and call_number_today > 0:
        title = f"ON CALL · call #{call_number_today} today"
    else:
        title = "ON CALL"
    if long_warning:
        title += " ⚠️"
    return title


def off_call_title(calls_today: int | None) -> str:
    if calls_today and calls_today > 0:
        return f"CALL ENDED · {calls_today} calls today"
    return "CALL ENDED"


def transfer_on_title(call_number_today: int | None, *, long_warning: bool = False) -> str:
    if call_number_today and call_number_today > 0:
        title = f"ON CALL · TRANSFER · call #{call_number_today} today"
    else:
        title = "ON CALL · TRANSFER"
    if long_warning:
        title += " ⚠️"
    return title


def transfer_off_title(calls_today: int | None) -> str:
    if calls_today and calls_today > 0:
        return f"CALL ENDED · TRANSFER · {calls_today} calls today"
    return "CALL ENDED · TRANSFER"
