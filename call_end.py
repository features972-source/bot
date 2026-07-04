"""Resolve and enrich who ended a phone call."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from config import Settings
from database import ExtensionLink
from threex_token import THREECX_TOKENS_KEY, TokenHolder, fetch_token

logger = logging.getLogger(__name__)

HANGUP_BY_KEY = "pending_hangup_by"
WS_END_HINT_KEY = "call_end_ws_hints"
CALL_LEGS_VISIBLE_KEY = "call_legs_visible"
HANGUP_TTL_SECONDS = 60
WS_END_TTL_SECONDS = 60
ENRICH_POLL_SECONDS = 8
ENRICH_MAX_WAIT_SECONDS = 90

ENDED_BY_CALLER = "caller"
ENDED_BY_USER = "user"

_call_history_denied_logged = False


def _set_ws_end_hint(
    bot_data: dict,
    extension: str,
    kind: str,
    *,
    reason: str = "",
) -> None:
    ext = extension.strip()
    hints = bot_data.setdefault(WS_END_HINT_KEY, {})
    existing = hints.get(ext)
    if kind == ENDED_BY_CALLER and existing and existing.get("kind") == ENDED_BY_USER:
        return
    hints[ext] = {"kind": kind, "at": time.monotonic()}
    label = "agent" if kind == ENDED_BY_USER else "caller"
    if reason:
        logger.info("Call end hint ext %s: %s (%s)", ext, label, reason)
    else:
        logger.info("Call end hint ext %s: %s", ext, label)


def mark_telegram_hangup(bot_data: dict, extension: str, *, label: str = "") -> None:
    pending = bot_data.setdefault(HANGUP_BY_KEY, {})
    pending[extension] = {
        "kind": ENDED_BY_USER,
        "at": time.monotonic(),
    }


def consume_telegram_hangup_label(bot_data: dict, extension: str) -> str | None:
    pending = bot_data.get(HANGUP_BY_KEY, {})
    info = pending.pop(extension, None)
    if not info:
        return None
    if time.monotonic() - float(info.get("at", 0)) > HANGUP_TTL_SECONDS:
        return None
    return ENDED_BY_USER


def _participant_id(participant: dict[str, Any]) -> int | None:
    raw = participant.get("id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def note_ws_participant_removed(
    bot_data: dict,
    extension: str,
    participant: dict[str, Any],
    *,
    tracked_participant_id: int | None = None,
) -> None:
    from threex_api import is_agent_leg_participant, is_external_caller_participant

    ext = extension.strip()
    hints = bot_data.setdefault(WS_END_HINT_KEY, {})
    existing = hints.get(ext)

    removed_id = _participant_id(participant)
    ended_by: str | None = None

    if tracked_participant_id is not None and removed_id is not None:
        if removed_id != tracked_participant_id:
            ended_by = ENDED_BY_USER
        elif is_agent_leg_participant(participant, extension=ext):
            ended_by = ENDED_BY_USER
        elif is_external_caller_participant(participant, extension=ext):
            ended_by = ENDED_BY_CALLER
        else:
            return
    elif is_agent_leg_participant(participant, extension=ext):
        ended_by = ENDED_BY_USER
    elif is_external_caller_participant(participant, extension=ext):
        ended_by = ENDED_BY_CALLER

    if ended_by is None:
        return

    if ended_by == ENDED_BY_USER:
        _set_ws_end_hint(bot_data, ext, ENDED_BY_USER, reason=f"removed pid {removed_id}")
        return

    if existing and existing.get("kind") == ENDED_BY_USER:
        return

    _set_ws_end_hint(bot_data, ext, ENDED_BY_CALLER, reason=f"removed pid {removed_id}")


def record_call_leg_visibility(
    bot_data: dict,
    extension: str,
    participants: list[dict[str, Any]],
    callid: int | None,
) -> None:
    """Remember whether this call ever had a separate agent leg in Call Control."""
    from threex_api import is_agent_leg_participant, participant_callid

    if callid is None:
        return
    same_call = [
        participant
        for participant in participants
        if participant_callid(participant) == callid
    ]
    if not same_call:
        return
    has_agent_leg = any(
        is_agent_leg_participant(participant, extension=extension.strip())
        for participant in same_call
    )
    legs = bot_data.setdefault(CALL_LEGS_VISIBLE_KEY, {})
    entry = legs.get(extension.strip(), {})
    entry["had_agent_leg"] = entry.get("had_agent_leg", False) or has_agent_leg
    entry["callid"] = callid
    legs[extension.strip()] = entry


def _had_agent_leg_visible(bot_data: dict, extension: str, callid: int | None) -> bool:
    entry = bot_data.get(CALL_LEGS_VISIBLE_KEY, {}).get(extension.strip())
    if not entry or entry.get("callid") != callid:
        return False
    return bool(entry.get("had_agent_leg"))


def clear_call_leg_visibility(bot_data: dict, extension: str) -> None:
    legs = bot_data.get(CALL_LEGS_VISIBLE_KEY)
    if legs is not None:
        legs.pop(extension.strip(), None)


async def probe_call_end_after_tracked_remove(
    bot_data: dict,
    *,
    extension: str,
    tokens: TokenHolder,
    fqdn: str,
    http_client: httpx.AsyncClient,
    callid: int | None,
) -> None:
    """After the tracked caller leg is removed, inspect who is still connected."""
    from call_extension_sync import list_extension_participants
    from threex_api import (
        is_agent_leg_participant,
        is_connected_participant,
        participant_callid,
    )

    if callid is None:
        return

    participants = await list_extension_participants(
        fqdn,
        tokens,
        extension,
        http_client=http_client,
        urgent=True,
    )
    if participants is None:
        logger.info("Call end probe ext %s: participant list unavailable", extension)
        return

    connected = [
        participant
        for participant in participants
        if participant_callid(participant) == callid
        and is_connected_participant(participant)
    ]
    agent_still_on = any(
        is_agent_leg_participant(participant, extension=extension.strip())
        for participant in connected
    )

    had_agent_leg = _had_agent_leg_visible(bot_data, extension, callid)

    if agent_still_on:
        _set_ws_end_hint(
            bot_data,
            extension,
            ENDED_BY_CALLER,
            reason="probe: agent leg still connected",
        )
    elif not connected:
        if had_agent_leg:
            reason = "probe: all legs disconnected"
        else:
            reason = "probe: single-leg teardown (agent ended)"
        _set_ws_end_hint(
            bot_data,
            extension,
            ENDED_BY_USER,
            reason=reason,
        )
    clear_call_leg_visibility(bot_data, extension)


def consume_ws_end_reason(bot_data: dict, extension: str) -> str | None:
    hints = bot_data.get(WS_END_HINT_KEY, {})
    info = hints.pop(extension.strip(), None)
    if not info:
        return None
    if time.monotonic() - float(info.get("at", 0)) > WS_END_TTL_SECONDS:
        return None
    kind = info.get("kind")
    if kind in {ENDED_BY_CALLER, ENDED_BY_USER}:
        return kind
    return None


_BLOCKQUOTE_RE = re.compile(
    r"^(?P<open><blockquote[^>]*>)(?P<inner>.*)(?P<close></blockquote>)\s*$",
    re.DOTALL,
)


def _split_blockquote(text: str) -> tuple[str, str, str]:
    """Split into (open_tag, inner, close_tag); empty tags if not wrapped."""
    match = _BLOCKQUOTE_RE.match(text)
    if match:
        return match.group("open"), match.group("inner"), match.group("close")
    return "", text, ""


def append_ended_by(text: str, ended_by: str | None) -> str:
    from call_display import format_ended_by_line

    if not ended_by or "Ended by" in text:
        return text
    line = format_ended_by_line(ended_by)
    if not line:
        return text
    open_tag, inner, close_tag = _split_blockquote(text)
    return f"{open_tag}{inner}\n{line}{close_tag}"


def set_ended_by(text: str, ended_by: str | None) -> str:
    """Replace or add the ended-by line (used when enriching from call history)."""
    if not ended_by:
        return text
    open_tag, inner, close_tag = _split_blockquote(text)
    inner = "\n".join(
        line for line in inner.splitlines() if "Ended by" not in line
    ).strip()
    rebuilt = f"{open_tag}{inner}{close_tag}" if open_tag else inner
    return append_ended_by(rebuilt, ended_by)


def _field(record: dict[str, Any], *names: str) -> str:
    for name in names:
        for key, value in record.items():
            if key.lower() == name.lower() and value not in (None, ""):
                return str(value).strip()
    return ""


def _termination_reason(record: dict[str, Any]) -> str:
    return _field(
        record,
        "TerminationReason",
        "termination_reason",
        "Reason",
        "reason",
    ).lower()


def label_from_call_history(
    record: dict[str, Any],
    *,
    extension: str,
) -> str | None:
    reason = _termination_reason(record)
    if not reason:
        return None

    ext = extension.strip()
    source_dn = _field(record, "SourceDn", "source_dn_number", "FromDn", "from_dn")
    dest_dn = _field(
        record,
        "DestinationDn",
        "destination_dn_number",
        "ToDn",
        "to_dn",
    )

    agent_is_dest = ext == dest_dn or (dest_dn and ext in dest_dn)
    agent_is_source = ext == source_dn or (source_dn and ext in source_dn)

    if reason == "src_participant_terminated":
        if agent_is_source:
            return ENDED_BY_USER
        return ENDED_BY_CALLER
    if reason == "dst_participant_terminated":
        if agent_is_dest:
            return ENDED_BY_USER
        return ENDED_BY_CALLER

    if reason in {"cancelled", "rejected", "no_answer", "timeout"}:
        return ENDED_BY_CALLER
    return None


async def _fetch_token(settings: Settings, bot_data: dict) -> str | None:
    holder = bot_data.get(THREECX_TOKENS_KEY)
    if isinstance(holder, TokenHolder):
        return await holder.get()
    return await fetch_token(settings)


async def _list_call_history(
    settings: Settings,
    token: str,
    *,
    started_after: datetime,
) -> list[dict[str, Any]]:
    global _call_history_denied_logged

    start_iso = started_after.strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "$top": 25,
        "$orderby": "SegmentStartTime desc",
        "$filter": f"SegmentStartTime ge {start_iso}",
    }
    url = f"https://{settings.threex_fqdn}/xapi/v1/CallHistoryView"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code >= 400:
            if response.status_code == 403 and not _call_history_denied_logged:
                logger.warning(
                    "CallHistoryView denied (403); off-phone end enrichment unavailable "
                    "until 3CX admin grants XAPI scope"
                )
                _call_history_denied_logged = True
            elif response.status_code != 403:
                logger.info(
                    "CallHistoryView failed (%s): %s",
                    response.status_code,
                    response.text[:200],
                )
            return []
        data = response.json()
        if isinstance(data, dict):
            items = data.get("value")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def _history_matches(
    record: dict[str, Any],
    *,
    extension: str,
    caller_number: str,
    started_after: datetime,
) -> bool:
    ext = extension.strip()
    source_dn = _field(record, "SourceDn", "source_dn_number", "FromDn", "from_dn")
    dest_dn = _field(
        record,
        "DestinationDn",
        "destination_dn_number",
        "ToDn",
        "to_dn",
    )
    if ext not in {source_dn, dest_dn} and not any(
        ext and ext in dn for dn in (source_dn, dest_dn) if dn
    ):
        return False

    segment_start = _field(
        record,
        "SegmentStartTime",
        "StartTime",
        "segment_start_time",
        "start_time",
    )
    if segment_start:
        try:
            started = datetime.fromisoformat(segment_start.replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if started < started_after - timedelta(minutes=1):
                return False
        except ValueError:
            pass

    if not caller_number:
        return True
    caller_fields = (
        _field(record, "SourceCallerId", "source_caller_id", "Caller", "caller"),
        _field(record, "FromCallerNumber", "from_caller_number"),
        _field(record, "DestinationCallerId", "destination_caller_id"),
    )
    want = "".join(ch for ch in caller_number if ch.isdigit())
    if not want:
        return True
    for value in caller_fields:
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits and (want in digits or digits in want):
            return True
    return not want


def _pick_call_history(
    records: list[dict[str, Any]],
    *,
    extension: str,
    caller_number: str,
    started_after: datetime,
) -> dict[str, Any] | None:
    for record in records:
        if not _history_matches(
            record,
            extension=extension,
            caller_number=caller_number,
            started_after=started_after,
        ):
            continue
        if _termination_reason(record):
            return record
    for record in records:
        if _history_matches(
            record,
            extension=extension,
            caller_number=caller_number,
            started_after=started_after,
        ):
            return record
    return None


def schedule_call_end_enrichment(
    bot,
    settings: Settings,
    bot_data: dict,
    *,
    extension: str,
    link: ExtensionLink,
    caller_name: str,
    caller_number: str,
    started_at_utc: float,
    base_text: str,
    message_ids: dict[int, int],
) -> None:
    if not settings.threex_enabled or not message_ids:
        return
    started_after = datetime.fromtimestamp(started_at_utc, tz=timezone.utc) - timedelta(
        minutes=2
    )
    asyncio.create_task(
        _enrich_call_end(
            bot,
            settings,
            bot_data,
            extension=extension,
            caller_number=caller_number,
            base_text=base_text,
            message_ids=dict(message_ids),
            started_after=started_after,
        ),
        name=f"call-end-{extension}",
    )


async def _enrich_call_end(
    bot,
    settings: Settings,
    bot_data: dict,
    *,
    extension: str,
    caller_number: str,
    base_text: str,
    message_ids: dict[int, int],
    started_after: datetime,
) -> None:
    deadline = time.monotonic() + ENRICH_MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        token = await _fetch_token(settings, bot_data)
        if token is None:
            return
        records = await _list_call_history(settings, token, started_after=started_after)
        record = _pick_call_history(
            records,
            extension=extension,
            caller_number=caller_number,
            started_after=started_after,
        )
        if record is not None:
            ended_by = label_from_call_history(record, extension=extension)
            if ended_by:
                enriched = set_ended_by(base_text, ended_by)
                if enriched != base_text:
                    await _edit_off_messages(bot, message_ids, enriched)
                    logger.info(
                        "Updated off-phone end reason for ext %s: %s",
                        extension,
                        ended_by,
                    )
                return
        await asyncio.sleep(ENRICH_POLL_SECONDS)

    logger.debug(
        "No call end reason found for ext %s within %ss",
        extension,
        ENRICH_MAX_WAIT_SECONDS,
    )


async def _edit_off_messages(
    bot,
    message_ids: dict[int, int],
    text: str,
) -> None:
    from telegram.constants import ParseMode
    from telegram.error import BadRequest

    for chat_id, message_id in message_ids.items():
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning(
                    "Could not update off-phone message in chat %s: %s",
                    chat_id,
                    exc,
                )
        except Exception:
            logger.exception("Failed to update off-phone message in chat %s", chat_id)
