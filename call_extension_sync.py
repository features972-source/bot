"""Extension-level call state sync with 3CX (stable + fast)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from config import Settings
from database import get_link_by_extension, list_links
from call_display import caller_from_participant
from missed_call_tracker import update_missed_call_tracking
from notify import (
    _live_calls,
    announce_call_ended,
    announce_call_started,
    announce_transfer_received,
    detect_transfer_from,
    resume_call_after_restart,
)
from threex_api import (
    filter_real_connected_participants,
    is_real_connected_participant,
    participant_callid,
)
from threex_token import TokenHolder

logger = logging.getLogger(__name__)

SYNC_TASKS_KEY = "extension_sync_tasks"
SYNC_WANT_KEY = "extension_sync_want"
EXT_LOCKS_KEY = "call_extension_locks"
EMPTY_STREAK_KEY = "empty_participant_streak"
LIST_PARTICIPANTS_SEM = asyncio.Semaphore(12)
SYNC_DEBOUNCE_SECONDS = 0.08
RECONCILE_INTERVAL_SECONDS = 12
RECONCILE_CONCURRENCY = 6
EMPTY_CONFIRM_COUNT = 2
QUIET_SYNC_KEY = "call_sync_quiet"
STARTUP_CONNECTED_KEY = "startup_connected_callids"


async def fetch_callcontrol_participants_map(
    fqdn: str,
    tokens: TokenHolder,
    *,
    http_client: httpx.AsyncClient,
) -> dict[str, list[dict[str, Any]]]:
    url = f"https://{fqdn}/callcontrol"
    token = await tokens.get()
    if not token:
        return {}
    try:
        response = await http_client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            await tokens.refresh()
            token = await tokens.get()
            if not token:
                return {}
            response = await http_client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            logger.warning("Callcontrol snapshot failed: HTTP %s", response.status_code)
            return {}
        data = response.json()
        if not isinstance(data, list):
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            extension = str(item.get("dn") or "").strip()
            if not extension:
                continue
            participants = item.get("participants") or []
            if isinstance(participants, list):
                result[extension] = [
                    participant
                    for participant in participants
                    if isinstance(participant, dict)
                ]
            else:
                result[extension] = []
        return result
    except Exception:
        logger.exception("Failed to fetch /callcontrol snapshot")
        return {}


def _extension_lock(bot_data: dict, extension: str) -> asyncio.Lock:
    locks = bot_data.setdefault(EXT_LOCKS_KEY, {})
    if extension not in locks:
        locks[extension] = asyncio.Lock()
    return locks[extension]


def _connected_participants(
    participants: list[dict[str, Any]], *, extension: str
) -> list[dict[str, Any]]:
    return filter_real_connected_participants(participants, extension=extension)


def _empty_streak(bot_data: dict) -> dict[str, int]:
    return bot_data.setdefault(EMPTY_STREAK_KEY, {})


def _clear_empty_streak(bot_data: dict, extension: str) -> None:
    _empty_streak(bot_data).pop(extension, None)


def _bump_empty_streak(bot_data: dict, extension: str) -> int:
    streaks = _empty_streak(bot_data)
    streaks[extension] = streaks.get(extension, 0) + 1
    return streaks[extension]


async def get_extension_participant(
    fqdn: str,
    tokens: TokenHolder,
    extension: str,
    participant_id: int,
    *,
    http_client: httpx.AsyncClient,
) -> dict[str, Any] | None:
    """Fetch one participant by id (used to enrich bare WS delete events)."""
    url = f"https://{fqdn}/callcontrol/{extension}/participants/{participant_id}"
    for attempt in range(2):
        token = await tokens.get()
        if not token:
            return None
        try:
            response = await http_client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == 401:
                await tokens.refresh()
                continue
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                logger.debug(
                    "Participant lookup failed for ext %s pid %s: HTTP %s",
                    extension,
                    participant_id,
                    response.status_code,
                )
                return None
            data = response.json()
            return data if isinstance(data, dict) else None
        except Exception:
            if attempt < 1:
                await asyncio.sleep(0.05)
                continue
            logger.debug(
                "Failed to fetch participant ext %s pid %s",
                extension,
                participant_id,
            )
            return None
    return None


async def list_extension_participants(
    fqdn: str,
    tokens: TokenHolder,
    extension: str,
    *,
    http_client: httpx.AsyncClient,
    urgent: bool = False,
) -> list[dict[str, Any]] | None:
    """Return participant list, or None if the API request failed."""
    url = f"https://{fqdn}/callcontrol/{extension}/participants"

    async def _fetch() -> list[dict[str, Any]] | None:
        for attempt in range(2):
            token = await tokens.get()
            if not token:
                return None
            try:
                response = await http_client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code == 401:
                    await tokens.refresh()
                    continue
                if response.status_code >= 400:
                    logger.warning(
                        "Participants lookup failed for ext %s: HTTP %s",
                        extension,
                        response.status_code,
                    )
                    return None
                data = response.json()
                if isinstance(data, list):
                    return [item for item in data if isinstance(item, dict)]
                return []
            except Exception:
                if attempt < 1:
                    await asyncio.sleep(0.05)
                    continue
                logger.exception("Failed to list participants for ext %s", extension)
                return None
        return None

    if urgent:
        return await _fetch()
    async with LIST_PARTICIPANTS_SEM:
        return await _fetch()


async def sync_extension_state(
    *,
    settings: Settings,
    bot,
    bot_data: dict,
    tokens: TokenHolder,
    fqdn: str,
    http_client: httpx.AsyncClient,
    extension: str,
    active: dict[str, set[int]] | None = None,
    participants: list[dict[str, Any]] | None = None,
    participants_known: bool = False,
    announce: bool | None = None,
    removed_participant_id: int | None = None,
    force_end: bool = False,
    ws_participant: dict[str, Any] | None = None,
    urgent: bool = False,
) -> None:
    link = get_link_by_extension(settings.database_path, extension)
    if link is None:
        return

    if announce is None:
        announce = not bot_data.get(QUIET_SYNC_KEY, False)

    sync_started = time.monotonic()
    pending: list[dict[str, Any]] = []
    participants_resolved = participants
    empty_refetch = False

    quick_end = (
        force_end
        and removed_participant_id is not None
        and _live_calls(bot_data).get(extension) is not None
        and _live_calls(bot_data)[extension].participant_id == removed_participant_id
    )

    if not quick_end and not participants_known:
        if ws_participant and is_real_connected_participant(
            ws_participant, extension=extension
        ):
            participants_resolved = [ws_participant]
        else:
            participants_resolved = await list_extension_participants(
                fqdn,
                tokens,
                extension,
                http_client=http_client,
                urgent=urgent,
            )
            if participants_resolved is None:
                return

    async with _extension_lock(bot_data, extension):
        live_calls = _live_calls(bot_data)
        live_call = live_calls.get(extension)
        live = live_call is not None

        if quick_end:
            if active is not None:
                active.pop(extension, None)
            if live_call is not None and live_call.callid is not None:
                bot_data.get(STARTUP_CONNECTED_KEY, set()).discard(
                    f"{extension}:{live_call.callid}"
                )
            _clear_empty_streak(bot_data, extension)
            pending.append({"kind": "end", "participant": None, "log": "WS remove"})

        else:
            participants = participants_resolved
            connected = _connected_participants(participants or [], extension=extension)
            live_call = live_calls.get(extension)
            live = live_call is not None

            if not connected:
                if not live:
                    _clear_empty_streak(bot_data, extension)
                    return
                streak = _bump_empty_streak(bot_data, extension)
                empty_confirm = 1 if urgent else EMPTY_CONFIRM_COUNT
                if urgent or streak >= empty_confirm:
                    _clear_empty_streak(bot_data, extension)
                    if active is not None:
                        active.pop(extension, None)
                    if live_call is not None and live_call.callid is not None:
                        bot_data.get(STARTUP_CONNECTED_KEY, set()).discard(
                            f"{extension}:{live_call.callid}"
                        )
                    pending.append({"kind": "end", "participant": None, "log": "hangup"})
                else:
                    empty_refetch = True
            else:
                _clear_empty_streak(bot_data, extension)
                participant = connected[0]
                callid = participant_callid(participant)
                try:
                    participant_id = int(participant.get("id"))
                except (TypeError, ValueError):
                    participant_id = None

                if live:
                    if (
                        live_call is not None
                        and callid is not None
                        and live_call.callid == callid
                    ):
                        from call_end import record_call_leg_visibility

                        record_call_leg_visibility(
                            bot_data,
                            extension,
                            participants or [],
                            callid,
                        )
                        return
                    if live_call is not None:
                        if active is not None:
                            active.pop(extension, None)
                        pending.append(
                            {
                                "kind": "end",
                                "participant": participant,
                                "log": "call replaced",
                            }
                        )

                if not announce:
                    if callid is not None:
                        bot_data.setdefault(STARTUP_CONNECTED_KEY, set()).add(
                            f"{extension}:{callid}"
                        )
                elif snap_key := (
                    f"{extension}:{callid}" if callid is not None else ""
                ):
                    startup_connected = bot_data.get(STARTUP_CONNECTED_KEY, set())
                    if snap_key in startup_connected:
                        live_call = live_calls.get(extension)
                        if live_call is None or live_call.silent or not live_call.message_ids:
                            pending.append(
                                {
                                    "kind": "resume",
                                    "participant": participant,
                                    "callid": callid,
                                    "participant_id": participant_id,
                                }
                            )
                            logger.info(
                                "Resuming pre-restart call ext %s callid %s (message + timer)",
                                extension,
                                callid,
                            )
                    else:
                        from_extension = detect_transfer_from(
                            participant, extension, bot_data
                        )
                        if from_extension is not None:
                            logger.info(
                                "Transfer detected: ext %s → ext %s (callid=%s)",
                                from_extension,
                                extension,
                                participant.get("callid"),
                            )
                            pending.append(
                                {
                                    "kind": "transfer",
                                    "from_extension": from_extension,
                                    "participant": participant,
                                }
                            )
                        else:
                            pending.append(
                                {
                                    "kind": "start",
                                    "participant": participant,
                                    "callid": callid,
                                    "participant_id": participant_id,
                                    "active_participant_id": (
                                        participant_id
                                        if participant_id and participant_id > 0
                                        else None
                                    ),
                                }
                            )
                else:
                    from_extension = detect_transfer_from(
                        participant, extension, bot_data
                    )
                    if from_extension is not None:
                        logger.info(
                            "Transfer detected: ext %s → ext %s (callid=%s)",
                            from_extension,
                            extension,
                            participant.get("callid"),
                        )
                        pending.append(
                            {
                                "kind": "transfer",
                                "from_extension": from_extension,
                                "participant": participant,
                            }
                        )
                    else:
                        pending.append(
                            {
                                "kind": "start",
                                "participant": participant,
                                "callid": callid,
                                "participant_id": participant_id,
                                "active_participant_id": (
                                    participant_id
                                    if participant_id and participant_id > 0
                                    else None
                                ),
                            }
                        )

    if empty_refetch:
        streak = _empty_streak(bot_data).get(extension, 0)
        logger.debug(
            "Ext %s empty once (%s/%s) — confirming hangup",
            extension,
            streak,
            EMPTY_CONFIRM_COUNT,
        )
        refetch = await list_extension_participants(
            fqdn,
            tokens,
            extension,
            http_client=http_client,
            urgent=urgent,
        )
        async with _extension_lock(bot_data, extension):
            live_calls = _live_calls(bot_data)
            live_call = live_calls.get(extension)
            if live_call is None:
                _clear_empty_streak(bot_data, extension)
            else:
                if refetch is not None:
                    connected = _connected_participants(refetch, extension=extension)
                    if connected:
                        _clear_empty_streak(bot_data, extension)
                        participant = connected[0]
                        callid = participant_callid(participant)
                        if (
                            callid is not None
                            and live_call.callid == callid
                        ):
                            from call_end import record_call_leg_visibility

                            record_call_leg_visibility(
                                bot_data, extension, refetch, callid
                            )
                            streak = 0
                        else:
                            streak = _empty_streak(bot_data).get(extension, streak)
                    else:
                        streak = _bump_empty_streak(bot_data, extension)
                if streak >= EMPTY_CONFIRM_COUNT:
                    _clear_empty_streak(bot_data, extension)
                    if active is not None:
                        active.pop(extension, None)
                    if live_call is not None and live_call.callid is not None:
                        bot_data.get(STARTUP_CONNECTED_KEY, set()).discard(
                            f"{extension}:{live_call.callid}"
                        )
                    pending.append(
                        {"kind": "end", "participant": None, "log": "hangup"}
                    )

    async with _extension_lock(bot_data, extension):
        live_call = _live_calls(bot_data).get(extension)
        update_missed_call_tracking(
            bot_data,
            settings.database_path,
            link,
            extension,
            participants_resolved,
            live_callid=live_call.callid if live_call else None,
        )

    if not pending:
        return

    elapsed_ms = (time.monotonic() - sync_started) * 1000
    bot_data[f"sync_pending_ms:{extension}"] = elapsed_ms

    for action in pending:
        kind = action["kind"]
        if kind == "end":
            await announce_call_ended(
                bot, settings, bot_data, link, participant=action.get("participant")
            )
            logger.info(
                "Announced off phone (%s): ext %s", action.get("log"), extension
            )
            continue

        if kind == "resume":
            await resume_call_after_restart(
                bot,
                settings,
                bot_data,
                link,
                participant=action.get("participant"),
                callid=action.get("callid"),
                participant_id=action.get("participant_id"),
            )
            pid = action.get("participant_id")
            if pid and active is not None and pid > 0:
                active.setdefault(extension, set()).add(pid)
            continue

        if kind == "transfer":
            await announce_transfer_received(
                bot,
                settings,
                bot_data,
                from_extension=action["from_extension"],
                to_link=link,
                active=active if active is not None else {},
                participant=action["participant"],
            )
            return

        if kind == "start":
            announced = await announce_call_started(
                bot,
                settings,
                bot_data,
                link,
                participant=action["participant"],
                callid=action["callid"],
                participant_id=action["participant_id"],
            )
            pid = action.get("active_participant_id")
            if announced and active is not None and pid:
                active.setdefault(extension, set()).add(pid)
            if announced:
                from call_end import record_call_leg_visibility

                callid = action.get("callid")
                parts = await list_extension_participants(
                    fqdn,
                    tokens,
                    extension,
                    http_client=http_client,
                    urgent=True,
                )
                if parts is not None:
                    record_call_leg_visibility(
                        bot_data, extension, parts, callid
                    )


async def flush_startup_connected_announces(
    *,
    settings: Settings,
    bot,
    bot_data: dict,
    tokens: TokenHolder,
    fqdn: str,
    http_client: httpx.AsyncClient,
    active: dict[str, set[int]],
) -> None:
    """Announce calls discovered during quiet startup reconcile."""
    startup = bot_data.get(STARTUP_CONNECTED_KEY, set())
    if not startup:
        return
    extensions = sorted({key.split(":", 1)[0] for key in startup})
    logger.info(
        "Flushing %d startup-connected extension(s): %s",
        len(extensions),
        ", ".join(extensions),
    )
    for extension in extensions:
        try:
            await sync_extension_state(
                settings=settings,
                bot=bot,
                bot_data=bot_data,
                tokens=tokens,
                fqdn=fqdn,
                http_client=http_client,
                extension=extension,
                active=active,
                announce=True,
            )
        except Exception:
            logger.exception(
                "Startup resume failed for ext %s", extension
            )


def schedule_extension_sync(
    *,
    settings: Settings,
    bot,
    bot_data: dict,
    tokens: TokenHolder,
    fqdn: str,
    http_client: httpx.AsyncClient,
    extension: str,
    active: dict[str, set[int]] | None,
    urgent: bool = False,
    removed_participant_id: int | None = None,
    ws_participant: dict[str, Any] | None = None,
) -> None:
    want = bot_data.setdefault(SYNC_WANT_KEY, {})
    state = want.setdefault(
        extension,
        {"pending": False, "urgent": False, "removed": None, "force_end": False},
    )
    tasks: dict[str, asyncio.Task] = bot_data.setdefault(SYNC_TASKS_KEY, {})
    existing = tasks.get(extension)
    sync_running = existing is not None and not existing.done()
    state["pending"] = True
    if urgent:
        state["urgent"] = True
        ts_key = f"ws_event_ts:{extension}"
        if not sync_running or ts_key not in bot_data:
            bot_data[ts_key] = time.monotonic()
    if ws_participant is not None:
        state["ws_participant"] = ws_participant
    if removed_participant_id is not None:
        state["removed"] = removed_participant_id
        state["force_end"] = True

    if sync_running:
        return

    tasks[extension] = asyncio.create_task(
        _run_extension_sync_loop(
            settings=settings,
            bot=bot,
            bot_data=bot_data,
            tokens=tokens,
            fqdn=fqdn,
            http_client=http_client,
            extension=extension,
            active=active,
        ),
        name=f"sync-ext-{extension}",
    )


async def _run_extension_sync_loop(
    *,
    settings: Settings,
    bot,
    bot_data: dict,
    tokens: TokenHolder,
    fqdn: str,
    http_client: httpx.AsyncClient,
    extension: str,
    active: dict[str, set[int]] | None,
) -> None:
    try:
        while True:
            state = bot_data.get(SYNC_WANT_KEY, {}).get(extension)
            if state is None or not state.get("pending"):
                return

            delay = 0.0 if state.get("urgent") else SYNC_DEBOUNCE_SECONDS
            if delay > 0:
                await asyncio.sleep(delay)

            state = bot_data.get(SYNC_WANT_KEY, {}).get(extension)
            if state is None or not state.get("pending"):
                return

            removed_participant_id = state.pop("removed", None)
            force_end = state.pop("force_end", False)
            ws_participant = state.pop("ws_participant", None)
            was_urgent = bool(state.get("urgent"))
            state["urgent"] = False
            state["pending"] = False

            await sync_extension_state(
                settings=settings,
                bot=bot,
                bot_data=bot_data,
                tokens=tokens,
                fqdn=fqdn,
                http_client=http_client,
                extension=extension,
                active=active,
                removed_participant_id=removed_participant_id,
                force_end=force_end,
                ws_participant=ws_participant,
                urgent=was_urgent,
            )

            state = bot_data.get(SYNC_WANT_KEY, {}).get(extension)
            if state is None or not state.get("pending"):
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Extension sync failed for ext %s", extension)


async def reconcile_all_linked_extensions(
    *,
    settings: Settings,
    bot,
    bot_data: dict,
    tokens: TokenHolder,
    fqdn: str,
    http_client: httpx.AsyncClient,
    active: dict[str, set[int]],
    announce: bool | None = None,
) -> None:
    want = bot_data.get(SYNC_WANT_KEY, {})
    if any(
        state.get("urgent") or state.get("pending")
        for state in want.values()
    ):
        return
    snapshot = await fetch_callcontrol_participants_map(
        fqdn, tokens, http_client=http_client
    )
    extensions = [link.extension for link in list_links(settings.database_path)]
    live_extensions = set(_live_calls(bot_data).keys())
    sem = asyncio.Semaphore(RECONCILE_CONCURRENCY)

    async def _sync_one(extension: str) -> None:
        want = bot_data.get(SYNC_WANT_KEY, {}).get(extension)
        if want and (want.get("urgent") or want.get("pending")):
            return
        sync_task = bot_data.get(SYNC_TASKS_KEY, {}).get(extension)
        if sync_task is not None and not sync_task.done():
            return
        snap_parts = snapshot.get(extension, [])
        snap_connected = _connected_participants(snap_parts, extension=extension)
        is_live = extension in live_extensions
        if not is_live and not snap_connected:
            return

        async with sem:
            await sync_extension_state(
                settings=settings,
                bot=bot,
                bot_data=bot_data,
                tokens=tokens,
                fqdn=fqdn,
                http_client=http_client,
                extension=extension,
                active=active,
                participants=snap_parts,
                participants_known=True,
                announce=announce,
            )

    await asyncio.gather(*(_sync_one(ext) for ext in extensions), return_exceptions=True)


async def reconcile_live_calls_loop(
    *,
    settings: Settings,
    bot,
    bot_data: dict,
    tokens: TokenHolder,
    fqdn: str,
    http_client: httpx.AsyncClient,
    active: dict[str, set[int]],
) -> None:
    try:
        while True:
            await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
            await reconcile_all_linked_extensions(
                settings=settings,
                bot=bot,
                bot_data=bot_data,
                tokens=tokens,
                fqdn=fqdn,
                http_client=http_client,
                active=active,
            )
    except asyncio.CancelledError:
        raise
