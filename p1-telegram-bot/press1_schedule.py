"""Scheduled campaign storage and time parsing."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import vicidial_client as vd

TZ = ZoneInfo(os.getenv("PRESS1_SCHEDULE_TZ", "Europe/London"))
_TIME_RE = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$",
    re.IGNORECASE,
)


def _empty() -> dict:
    return {"schedules": []}


def load_schedules() -> dict:
    try:
        raw = vd.run_remote(f"cat {vd.SCHEDULES_PATH} 2>/dev/null", timeout=15).strip()
        if not raw:
            return _empty()
        data = json.loads(raw)
        data.setdefault("schedules", [])
        return data
    except Exception:
        return _empty()


def save_schedules(data: dict) -> None:
    payload = json.dumps(data, indent=2)
    vd.run_remote(
        f"mkdir -p $(dirname {vd.SCHEDULES_PATH}); "
        f"cat > {vd.SCHEDULES_PATH} <<'EOF'\n{payload}\nEOF\n"
        f"chmod 644 {vd.SCHEDULES_PATH}",
        timeout=20,
    )


def _parse_clock(text: str) -> tuple[int, int]:
    text = text.strip().lower()
    if not text:
        raise ValueError("Time required, e.g. 9am or 10:30")
    m = _TIME_RE.match(text.replace(" ", ""))
    if not m:
        raise ValueError(f"Could not parse time: {text!r}")
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = (m.group(3) or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid time: {text!r}")
    return hour, minute


def parse_schedule_args(args: list[str]) -> datetime:
    if not args:
        raise ValueError(
            "Usage: /schedule 9am\n"
            "/schedule tomorrow 10:30\n"
            "/schedule today 3pm"
        )
    parts = [p for p in args if p]
    if parts and parts[-1].lower().endswith((".csv", ".txt")):
        parts = parts[:-1]
    if not parts:
        raise ValueError("Time required after filename hint")
    text = " ".join(parts).strip().lower()
    now = datetime.now(TZ)
    day = now.date()
    time_text = text
    if text.startswith("tomorrow"):
        day = (now + timedelta(days=1)).date()
        time_text = text[len("tomorrow") :].strip()
    elif text.startswith("today"):
        day = now.date()
        time_text = text[len("today") :].strip()
    hour, minute = _parse_clock(time_text)
    run_at = datetime(
        day.year,
        day.month,
        day.day,
        hour,
        minute,
        tzinfo=TZ,
    )
    if run_at <= now and not text.startswith("tomorrow"):
        run_at += timedelta(days=1)
    return run_at


def add_schedule(
    *,
    user_id: int,
    chat_id: int,
    numbers: list[str],
    run_at: datetime,
) -> dict:
    if not numbers:
        raise ValueError("No numbers loaded — paste a list or send a .csv first")
    data = load_schedules()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "user_id": user_id,
        "chat_id": chat_id,
        "numbers": numbers,
        "lead_count": len(numbers),
        "run_at": int(run_at.timestamp()),
        "created_at": int(time.time()),
        "timezone": str(TZ),
    }
    data["schedules"].append(entry)
    save_schedules(data)
    return entry


def list_schedules(user_id: int | None = None) -> list[dict]:
    data = load_schedules()
    now = time.time()
    items = [s for s in data.get("schedules", []) if float(s.get("run_at", 0)) > now - 60]
    if user_id is not None:
        items = [s for s in items if int(s.get("user_id", 0)) == user_id]
    items.sort(key=lambda s: float(s.get("run_at", 0)))
    if len(items) != len(data.get("schedules", [])):
        data["schedules"] = items
        save_schedules(data)
    return items


def remove_schedule(schedule_id: str, user_id: int) -> str:
    data = load_schedules()
    before = len(data.get("schedules", []))
    kept = []
    removed = None
    for s in data.get("schedules", []):
        if s.get("id") == schedule_id and int(s.get("user_id", 0)) == user_id:
            removed = s
            continue
        kept.append(s)
    if not removed:
        raise ValueError(f"No schedule {schedule_id!r} found")
    data["schedules"] = kept
    save_schedules(data)
    return removed.get("id", schedule_id)


def pop_due_schedules() -> list[dict]:
    data = load_schedules()
    now = time.time()
    due: list[dict] = []
    kept: list[dict] = []
    for s in data.get("schedules", []):
        if float(s.get("run_at", 0)) <= now:
            due.append(s)
        else:
            kept.append(s)
    if due:
        data["schedules"] = kept
        save_schedules(data)
    return due


def format_schedule_line(s: dict) -> str:
    run_at = datetime.fromtimestamp(float(s.get("run_at", 0)), tz=TZ)
    return (
        f"• `{s.get('id', '?')}` — {s.get('lead_count', 0)} leads at "
        f"{run_at.strftime('%a %d %b %H:%M %Z')}"
    )


def format_schedule_list(user_id: int | None = None) -> str:
    items = list_schedules(user_id)
    if not items:
        return "No scheduled campaigns."
    lines = ["⏰ Scheduled campaigns:\n"]
    lines.extend(format_schedule_line(s) for s in items)
    lines.append("\nCancel with /unschedule <id>")
    return "\n".join(lines)
