"""Private closer-rank DMs after payments."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from telegram import Bot

from config import Settings
from database import (
    PaymentLeaderboardEntry,
    get_latest_quiet_win_detail,
    get_payment_leaderboard,
    log_quiet_win,
    recent_quiet_win,
)
from handlers.stats_period import _parse_stats_period
from money_format import format_amount
from payment_day_celebration import format_shadow_day_facts, load_agent_day_stats

logger = logging.getLogger(__name__)

SHADOW_WIN_TYPE = "shadow_closer"
COOLDOWN_MINUTES = 15


@dataclass(frozen=True)
class ShadowRank:
    rank: int
    total_closers: int
    entry: PaymentLeaderboardEntry
    above: PaymentLeaderboardEntry | None
    below: PaymentLeaderboardEntry | None
    gap_to_above: float
    lead_over_below: float


def _entry_label(entry: PaymentLeaderboardEntry) -> str:
    username = (entry.telegram_username or "").strip().lstrip("@")
    if username:
        return f"@{html.escape(username)}"
    display = (entry.display_name or "").strip()
    if display:
        return html.escape(display)
    return html.escape(str(entry.user_id))


def compute_shadow_rank(
    entries: list[PaymentLeaderboardEntry],
    user_id: int,
) -> ShadowRank | None:
    index = next((i for i, entry in enumerate(entries) if entry.user_id == user_id), None)
    if index is None:
        return None
    entry = entries[index]
    above = entries[index - 1] if index > 0 else None
    below = entries[index + 1] if index + 1 < len(entries) else None
    gap_to_above = above.total_amount - entry.total_amount if above is not None else 0.0
    lead_over_below = entry.total_amount - below.total_amount if below is not None else 0.0
    return ShadowRank(
        rank=index + 1,
        total_closers=len(entries),
        entry=entry,
        above=above,
        below=below,
        gap_to_above=max(0.0, gap_to_above),
        lead_over_below=max(0.0, lead_over_below),
    )


def format_shadow_closer_message(rank: ShadowRank, *, day_facts: str = "") -> str:
    you = _entry_label(rank.entry)
    total = html.escape(format_amount(rank.entry.total_amount))
    facts_block = f"\n\n{day_facts}" if day_facts else ""

    if rank.total_closers == 1:
        return (
            "👤 <b>Shadow board</b> — you're the only closer logged today.\n\n"
            f"You: <b>{total}</b> · {you}{facts_block}"
        )

    if rank.rank == 1:
        below = rank.below
        assert below is not None
        ahead = html.escape(format_amount(rank.lead_over_below))
        return (
            f"👤 <b>Shadow board</b> — you're <b>#1</b> among closers today.\n\n"
            f"You: <b>{total}</b> · {you}\n"
            f"<b>{ahead}</b> ahead of {_entry_label(below)}{facts_block}"
        )

    assert rank.above is not None
    gap = html.escape(format_amount(rank.gap_to_above))
    return (
        f"👤 <b>Shadow board</b> — you're <b>#{rank.rank}</b> among closers today.\n\n"
        f"<b>{gap}</b> behind {_entry_label(rank.above)}\n"
        f"You: <b>{total}</b> · {you}{facts_block}"
    )


def _should_send_shadow_dm(settings: Settings, *, telegram_user_id: int, rank: int) -> bool:
    detail = f"rank={rank}"
    last_detail = get_latest_quiet_win_detail(
        settings.database_path,
        telegram_user_id,
        SHADOW_WIN_TYPE,
    )
    if last_detail != detail:
        return True
    return not recent_quiet_win(
        settings.database_path,
        telegram_user_id,
        SHADOW_WIN_TYPE,
        within_minutes=COOLDOWN_MINUTES,
    )


async def maybe_shadow_closer_rank(
    bot: Bot,
    settings: Settings,
    *,
    telegram_user_id: int,
) -> None:
    since, _ = _parse_stats_period([])
    entries = get_payment_leaderboard(settings.database_path, since=since)
    rank = compute_shadow_rank(entries, telegram_user_id)
    if rank is None:
        return
    if not _should_send_shadow_dm(settings, telegram_user_id=telegram_user_id, rank=rank.rank):
        return

    day_stats = load_agent_day_stats(
        settings.database_path,
        telegram_user_id=telegram_user_id,
        since=since,
    )
    day_facts = format_shadow_day_facts(day_stats)
    text = format_shadow_closer_message(rank, day_facts=day_facts)
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
            win_type=SHADOW_WIN_TYPE,
            detail=f"rank={rank.rank}",
        )
    except Exception:
        logger.exception(
            "Failed to send shadow closer rank to user %s",
            telegram_user_id,
        )
