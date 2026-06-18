"""Shared period parsing for /outstats and payment week boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def stats_timezone() -> ZoneInfo:
    import os

    name = os.getenv("STATS_TIMEZONE", "Europe/London").strip() or "Europe/London"
    for candidate in (name, "Europe/London", "UTC"):
        try:
            return ZoneInfo(candidate)
        except Exception:
            continue
    return timezone.utc  # type: ignore[return-value]


def _parse_stats_period(args: list[str]) -> tuple[datetime | None, str]:
    tz = stats_timezone()
    if not args:
        start_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return start_local.astimezone(timezone.utc), "today"

    token = args[0].strip().lower()
    if token in {"all", "ever", "total"}:
        return None, "all time"
    if token == "today":
        start_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return start_local.astimezone(timezone.utc), "today"
    if token.isdigit():
        days = int(token)
        start = datetime.now(timezone.utc) - timedelta(days=days)
        label = "today" if days <= 1 else f"last {days} days"
        return start, label

    start_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc), "today"


def stats_period_footnote() -> str:
    tz = stats_timezone()
    label = getattr(tz, "key", "UTC")
    return f"<i>“Today” uses {label} time.</i>"


def current_payment_week_start() -> tuple[datetime, str]:
    """Week starts Sunday 00:00 in STATS_TIMEZONE; /payments resets each Sunday."""
    tz = stats_timezone()
    now = datetime.now(tz)
    days_since_sunday = (now.weekday() + 1) % 7
    start_local = (now - timedelta(days=days_since_sunday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    label = f"this week (since Sun {start_local.strftime('%d %b')})"
    return start_local.astimezone(timezone.utc), label
