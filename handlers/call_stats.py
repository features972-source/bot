"""Admin call statistics commands."""

from __future__ import annotations

import html
from io import BytesIO

from telegram import InputFile, Update
from telegram.ext import CommandHandler, ContextTypes

from config import Settings
from database import (
    AgentCallStats,
    get_agent_call_stats,
    get_call_stats_totals,
    list_missed_calls_since,
)
from handlers.admin_access import require_admin
from handlers.stats_period import _parse_stats_period, stats_period_footnote
from missed_calls_export import build_missed_calls_csv, missed_calls_filename
from notify import format_duration


def build_call_stats_handlers() -> list:
    return [
        CommandHandler("stats", stats_command),
        CommandHandler("missedcalls", missedcalls_command),
    ]


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    since, period_label = _parse_stats_period(context.args)
    agents = get_agent_call_stats(settings.database_path, since=since)
    total_calls, total_seconds = get_call_stats_totals(settings.database_path, since=since)

    if not agents:
        await update.effective_message.reply_text(
            f"📊 No completed calls recorded for {period_label}."
        )
        return

    lines = [
        f"📊 <b>Call stats</b> — {html.escape(period_label)}",
        "",
        f"Total: <b>{total_calls}</b> calls · ⏱️ <b>{format_duration(total_seconds)}</b> talk time",
        "",
        "<b>Leaderboard</b>",
    ]

    medals = ("🥇", "🥈", "🥉")
    for index, agent in enumerate(agents):
        prefix = medals[index] if index < len(medals) else f"{index + 1}."
        lines.append(
            f"{prefix} {_agent_label(agent)} · ext <b>{html.escape(agent.extension)}</b>\n"
            f"   📞 {agent.call_count} calls · "
            f"⏱️ {format_duration(agent.total_seconds)} total · "
            f"avg {format_duration(int(agent.avg_seconds))}"
        )

    lines.extend(
        [
            "",
            stats_period_footnote(),
            "",
            "<i>Usage: /stats · /stats today · /stats 7 · /stats 30 · /stats all</i>",
        ]
    )

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def missedcalls_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    if message is None:
        return

    since, period_label = _parse_stats_period(context.args or [])
    records = list_missed_calls_since(settings.database_path, since=since)
    if not records:
        await message.reply_text(
            f"📵 No missed calls for {period_label}.",
        )
        return

    csv_bytes = build_missed_calls_csv(records)
    filename = missed_calls_filename(period_label)
    document = InputFile(BytesIO(csv_bytes), filename=filename)
    count = len(records)
    await message.reply_document(
        document=document,
        caption=(
            f"📵 <b>{count}</b> missed call{'s' if count != 1 else ''} — "
            f"{html.escape(period_label)}"
        ),
        parse_mode="HTML",
    )


def _agent_label(agent: AgentCallStats) -> str:
    username = (agent.telegram_username or "").strip()
    display = (agent.display_name or "").strip()
    if username and display:
        return f"@{html.escape(username.lstrip('@'))} ({html.escape(display)})"
    if username:
        return f"@{html.escape(username.lstrip('@'))}"
    if display:
        return html.escape(display)
    return f"ext {html.escape(agent.extension)}"

