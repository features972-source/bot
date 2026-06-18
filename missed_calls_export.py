"""Export missed calls to CSV for Telegram download."""

from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from io import StringIO

from database import MissedCall
from handlers.stats_period import stats_timezone

HEADERS = (
    "ID",
    "Missed at",
    "Extension",
    "Agent",
    "Caller name",
    "Caller number",
    "Ring (sec)",
    "Call ID",
    "Source",
)


def _parse_missed_at(missed_at: str) -> datetime:
    text = missed_at.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_missed_at(missed_at: str) -> str:
    local = _parse_missed_at(missed_at).astimezone(stats_timezone())
    return local.strftime("%d/%m/%Y %H:%M:%S")


def _agent_label(record: MissedCall) -> str:
    display = (record.display_name or "").strip()
    if display:
        return display
    username = (record.telegram_username or "").strip().lstrip("@")
    if username:
        return f"@{username}"
    return str(record.telegram_user_id)


def missed_calls_filename(period_label: str) -> str:
    slug = period_label.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    slug = slug or "export"
    return f"missed-calls-{slug}.csv"


def build_missed_calls_csv(records: list[MissedCall]) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADERS)
    for record in records:
        writer.writerow(
            [
                record.id,
                _format_missed_at(record.missed_at),
                record.extension,
                _agent_label(record),
                record.caller_name,
                record.caller_number,
                record.ring_seconds,
                record.callid if record.callid is not None else "",
                record.source,
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")
