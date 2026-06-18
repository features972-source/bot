"""Track inbound rings that end without an answer."""

from __future__ import annotations

import logging
import time
from typing import Any

from call_display import caller_from_participant
from database import ExtensionLink, record_missed_call
from threex_api import is_inbound_ringing_participant, participant_callid

logger = logging.getLogger(__name__)

RINGING_TRACKER_KEY = "inbound_ringing_tracker"
ANSWERED_CALLIDS_KEY = "answered_callids"
ANSWERED_TTL_SECONDS = 3600


def _tracker_key(extension: str, callid: int) -> str:
    return f"{extension}:{callid}"


def _prune_answered(bot_data: dict) -> None:
    answered = bot_data.get(ANSWERED_CALLIDS_KEY)
    if not isinstance(answered, dict):
        return
    now = time.monotonic()
    for key in list(answered.keys()):
        if now - float(answered[key]) > ANSWERED_TTL_SECONDS:
            answered.pop(key, None)


def mark_call_answered(bot_data: dict, extension: str, callid: int | None) -> None:
    if callid is None:
        return
    key = _tracker_key(extension, callid)
    tracker = bot_data.get(RINGING_TRACKER_KEY)
    if isinstance(tracker, dict):
        tracker.pop(key, None)
    answered = bot_data.setdefault(ANSWERED_CALLIDS_KEY, {})
    if isinstance(answered, set):
        bot_data[ANSWERED_CALLIDS_KEY] = {item: time.monotonic() for item in answered}
        answered = bot_data[ANSWERED_CALLIDS_KEY]
    answered[key] = time.monotonic()
    _prune_answered(bot_data)


def update_missed_call_tracking(
    bot_data: dict,
    database_path: str,
    link: ExtensionLink,
    extension: str,
    participants: list[dict[str, Any]] | None,
    *,
    live_callid: int | None = None,
) -> None:
    tracker = bot_data.setdefault(RINGING_TRACKER_KEY, {})
    now = time.monotonic()
    current_ring_keys: set[str] = set()

    if live_callid is not None:
        mark_call_answered(bot_data, extension, live_callid)

    for participant in participants or []:
        if not is_inbound_ringing_participant(participant, extension=extension):
            continue
        callid = participant_callid(participant)
        if callid is None:
            continue
        key = _tracker_key(extension, callid)
        current_ring_keys.add(key)
        if key not in tracker:
            caller_name, caller_number = caller_from_participant(participant)
            tracker[key] = {
                "first_seen": now,
                "caller_name": caller_name,
                "caller_number": caller_number,
            }

    prefix = f"{extension}:"
    for key in list(tracker.keys()):
        if not key.startswith(prefix):
            continue
        if key in current_ring_keys:
            continue
        try:
            callid = int(key.split(":", 1)[1])
        except (ValueError, IndexError):
            tracker.pop(key, None)
            continue
        if live_callid is not None and live_callid == callid:
            mark_call_answered(bot_data, extension, callid)
            continue
        answered = bot_data.get(ANSWERED_CALLIDS_KEY, {})
        if isinstance(answered, dict) and key in answered:
            tracker.pop(key, None)
            continue
        info = tracker.pop(key)
        ring_seconds = max(1, int(now - float(info.get("first_seen", now))))
        inserted = record_missed_call(
            database_path,
            extension=extension,
            telegram_user_id=link.telegram_user_id,
            telegram_username=link.telegram_username,
            display_name=link.display_name,
            caller_name=str(info.get("caller_name") or ""),
            caller_number=str(info.get("caller_number") or ""),
            callid=callid,
            ring_seconds=ring_seconds,
            source="3cx",
        )
        if inserted:
            logger.info(
                "Missed call ext %s callid %s from %s",
                extension,
                callid,
                info.get("caller_number") or info.get("caller_name") or "unknown",
            )


def record_missed_from_webhook(
    database_path: str,
    *,
    extension: str,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str | None,
    caller_name: str = "",
    caller_number: str = "",
    callid: int | None = None,
) -> bool:
    return record_missed_call(
        database_path,
        extension=extension,
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        display_name=display_name,
        caller_name=caller_name,
        caller_number=caller_number,
        callid=callid,
        ring_seconds=0,
        source="webhook",
    )
