"""Celebrational daily call / finish facts for payment announcements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database import (
    PaymentRecord,
    count_user_calls_since,
    count_user_finishes_since,
    count_user_opens_since,
)
from money_format import format_amount


@dataclass(frozen=True)
class AgentDayStats:
    calls: int
    finishes: int
    finish_amount: float
    opens: int
    open_amount: float


def load_agent_day_stats(
    path: str,
    *,
    telegram_user_id: int,
    since: datetime,
) -> AgentDayStats:
    finishes, finish_amount = count_user_finishes_since(
        path,
        telegram_user_id=telegram_user_id,
        since=since,
    )
    opens, open_amount = count_user_opens_since(
        path,
        telegram_user_id=telegram_user_id,
        since=since,
    )
    return AgentDayStats(
        calls=count_user_calls_since(
            path,
            telegram_user_id=telegram_user_id,
            since=since,
        ),
        finishes=finishes,
        finish_amount=finish_amount,
        opens=opens,
        open_amount=open_amount,
    )


def _ordinal(value: int) -> str:
    if 11 <= value % 100 <= 13:
        return f"{value}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _calls_tagline(calls: int) -> str:
    if calls >= 50:
        return "absolute beast mode"
    if calls >= 25:
        return "machine mode"
    if calls >= 10:
        return "strong volume"
    if calls >= 5:
        return "keep grinding"
    if calls >= 2:
        return "keep dialing"
    return "day started"


def _finishes_tagline(finishes: int) -> str:
    if finishes >= 10:
        return "legendary day"
    if finishes >= 5:
        return "on fire"
    if finishes >= 3:
        return "heating up"
    if finishes >= 2:
        return "momentum building"
    return "let's go"


def _format_calls_line(calls: int) -> str | None:
    if calls <= 0:
        return None
    if calls == 1:
        return "📞 1st call of the day — already closing! 🌟"
    tagline = _calls_tagline(calls)
    suffix = " 🔥" if calls >= 25 else " 💪" if calls >= 10 else ""
    return f"📞 {calls} calls taken today — {tagline}!{suffix}"


def format_calls_celebration_line(calls: int) -> str | None:
    return _format_calls_line(calls)


def _format_finishes_line(finishes: int, finish_amount: float) -> str | None:
    if finishes <= 0:
        return None
    amount = format_amount(finish_amount)
    tagline = _finishes_tagline(finishes)
    if finishes == 1:
        return f"🎯 1st finish today ({amount}) — {tagline}! 🌟"
    ordinal = _ordinal(finishes)
    flames = " 🔥🔥" if finishes >= 5 else " 🔥" if finishes >= 3 else ""
    return f"🎯 {ordinal} finish today ({amount} total closed) — {tagline}!{flames}"


def _format_opens_line(opens: int, open_amount: float) -> str | None:
    if opens <= 0:
        return None
    amount = format_amount(open_amount)
    if opens == 1:
        return f"🚪 1st open logged today ({amount}) — great start!"
    ordinal = _ordinal(opens)
    return f"🚪 {ordinal} open today ({amount} on the board) — opener energy! ✨"


def format_payment_day_celebration(
    record: PaymentRecord,
    *,
    finisher_stats: AgentDayStats,
    starter_stats: AgentDayStats | None = None,
) -> str | None:
    lines: list[str] = []

    finisher_calls = _format_calls_line(finisher_stats.calls)
    finisher_finishes = _format_finishes_line(
        finisher_stats.finishes,
        finisher_stats.finish_amount,
    )
    if finisher_calls:
        lines.append(finisher_calls)
    if finisher_finishes:
        lines.append(finisher_finishes)

    starter_id = record.starter_user_id
    if (
        starter_stats is not None
        and starter_id is not None
        and starter_id != record.finisher_user_id
    ):
        opens_line = _format_opens_line(starter_stats.opens, starter_stats.open_amount)
        if opens_line:
            lines.append(opens_line)

    if not lines:
        return None
    return "\n".join(lines)


def format_shadow_day_facts(stats: AgentDayStats) -> str:
    parts: list[str] = []
    if stats.calls > 0:
        parts.append(f"📞 {stats.calls} call{'s' if stats.calls != 1 else ''} today")
    if stats.finishes > 0:
        parts.append(
            f"🎯 {stats.finishes} finish{'es' if stats.finishes != 1 else ''} "
            f"({format_amount(stats.finish_amount)})"
        )
    if not parts:
        return ""
    return " · ".join(parts)
