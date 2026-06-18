"""Listen to 3CX AI Call Control API and post Telegram phone status."""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx
import websockets

from call_end import note_ws_participant_removed, probe_call_end_after_tracked_remove
from call_extension_sync import (
    QUIET_SYNC_KEY,
    flush_startup_connected_announces,
    get_extension_participant,
    reconcile_all_linked_extensions,
    reconcile_live_calls_loop,
    schedule_extension_sync,
)
from config import Settings
from database import list_links
from notify import _live_calls
from threex_api import extract_participant_bodies_from_ws, participant_from_ws_event
from threex_token import get_token_holder
from threex_ws import SUBSCRIBE_QUEUE_KEY

logger = logging.getLogger(__name__)

PARTICIPANT_PATH = re.compile(r"^/callcontrol/(?P<dn>\d+)/participants/(?P<pid>\d+)$")
PARTICIPANTS_PATH = re.compile(r"^/callcontrol/(?P<dn>\d+)/participants$")


async def start_call_control_listener(
    settings: Settings,
    bot,
    bot_data: dict,
) -> None:
    if not settings.threex_enabled:
        logger.info("3CX Call Control disabled (set THREECX_FQDN + credentials in .env)")
        return
    await _run_listener(settings, bot, bot_data)


async def _run_listener(settings: Settings, bot, bot_data: dict) -> None:
    fqdn = settings.threex_fqdn
    ws_url = f"wss://{fqdn}/callcontrol/ws"
    tokens = get_token_holder(bot_data, settings)
    active: dict[str, set[int]] = {}
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)

    while True:
        token = await tokens.refresh()
        if not token:
            await asyncio.sleep(30)
            continue

        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=10, limits=limits) as http_client:
                reconcile_task = asyncio.create_task(
                    reconcile_live_calls_loop(
                        settings=settings,
                        bot=bot,
                        bot_data=bot_data,
                        tokens=tokens,
                        fqdn=fqdn,
                        http_client=http_client,
                        active=active,
                    ),
                    name="3cx-reconcile",
                )
                try:
                    async with websockets.connect(
                        ws_url,
                        additional_headers=headers,
                        ping_interval=20,
                        ping_timeout=20,
                    ) as ws:
                        await ws.send(
                            json.dumps({"RequestID": "subscribe", "Path": "/callcontrol"})
                        )
                        logger.info("3CX Call Control WebSocket connected (%s)", fqdn)

                        bot_data[QUIET_SYNC_KEY] = True
                        try:
                            await reconcile_all_linked_extensions(
                                settings=settings,
                                bot=bot,
                                bot_data=bot_data,
                                tokens=tokens,
                                fqdn=fqdn,
                                http_client=http_client,
                                active=active,
                                announce=False,
                            )
                        finally:
                            bot_data.pop(QUIET_SYNC_KEY, None)
                        logger.info("Startup reconcile finished for linked extensions")
                        await flush_startup_connected_announces(
                            settings=settings,
                            bot=bot,
                            bot_data=bot_data,
                            tokens=tokens,
                            fqdn=fqdn,
                            http_client=http_client,
                            active=active,
                        )

                        subscribe_task = asyncio.create_task(
                            _subscription_sender(ws, bot_data, settings),
                            name="3cx-ws-subscribe",
                        )
                        try:
                            async for raw in ws:
                                if isinstance(raw, bytes):
                                    raw = raw.decode("utf-8", errors="replace")
                                _dispatch_message(
                                    _handle_message(
                                        raw,
                                        settings=settings,
                                        bot=bot,
                                        bot_data=bot_data,
                                        tokens=tokens,
                                        fqdn=fqdn,
                                        active=active,
                                        http_client=http_client,
                                    )
                                )
                        finally:
                            subscribe_task.cancel()
                            bot_data.pop(SUBSCRIBE_QUEUE_KEY, None)
                finally:
                    reconcile_task.cancel()
        except Exception as exc:
            logger.exception("3CX Call Control disconnected (%s); retrying in 15s", exc)
            await asyncio.sleep(15)


def _dispatch_message(coro) -> None:
    task = asyncio.create_task(coro)

    def _log_task_error(done: asyncio.Task) -> None:
        if done.cancelled():
            return
        exc = done.exception()
        if exc:
            logger.exception("3CX message handler failed", exc_info=exc)

    task.add_done_callback(_log_task_error)


async def _subscription_sender(ws, bot_data: dict, settings: Settings) -> None:
    queue: asyncio.Queue[str] = asyncio.Queue()
    bot_data[SUBSCRIBE_QUEUE_KEY] = queue
    for link in list_links(settings.database_path):
        await queue.put(link.extension)
    subscribed: set[str] = {"/callcontrol"}
    try:
        while True:
            extension = await queue.get()
            path = f"/callcontrol/{extension}"
            if path in subscribed:
                continue
            await ws.send(json.dumps({"RequestID": f"sub-{extension}", "Path": path}))
            subscribed.add(path)
            logger.info("3CX WS subscribed to %s", path)
    except asyncio.CancelledError:
        raise


async def _handle_message(
    raw: str,
    *,
    settings: Settings,
    bot,
    bot_data: dict,
    tokens,
    fqdn: str,
    active: dict[str, set[int]],
    http_client: httpx.AsyncClient,
) -> None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return

    event = payload.get("event") or {}
    entity = str(event.get("entity") or "")

    participant_match = PARTICIPANT_PATH.match(entity)
    participants_match = PARTICIPANTS_PATH.match(entity)
    if not participant_match and not participants_match:
        return

    extension = (
        participant_match.group("dn")
        if participant_match
        else participants_match.group("dn")
    )
    event_type = event.get("event_type")
    removed_participant_id: int | None = None
    ws_participant_id: int | None = None
    if participant_match:
        try:
            ws_participant_id = int(participant_match.group("pid"))
        except (TypeError, ValueError):
            ws_participant_id = None
        if event_type == 1:
            removed_participant_id = ws_participant_id

    ws_participant = None
    if removed_participant_id is None:
        ws_participant = participant_from_ws_event(payload, extension=extension)
    elif ws_participant_id is not None:
        ws_participant = await get_extension_participant(
            fqdn,
            tokens,
            extension,
            ws_participant_id,
            http_client=http_client,
        )

    live_call = _live_calls(bot_data).get(extension)
    if live_call is not None:
        tracked_pid = live_call.participant_id
        if (
            removed_participant_id is not None
            and tracked_pid is not None
            and removed_participant_id == tracked_pid
        ):
            await probe_call_end_after_tracked_remove(
                bot_data,
                extension=extension,
                tokens=tokens,
                fqdn=fqdn,
                http_client=http_client,
                callid=live_call.callid,
            )

        bodies = extract_participant_bodies_from_ws(payload)
        if bodies:
            for participant in bodies:
                note_ws_participant_removed(
                    bot_data,
                    extension,
                    participant,
                    tracked_participant_id=tracked_pid,
                )
        elif removed_participant_id is not None:
            participant = ws_participant or {"id": removed_participant_id}
            note_ws_participant_removed(
                bot_data,
                extension,
                participant,
                tracked_participant_id=tracked_pid,
            )

    # Sync immediately on every WS event (coalesced per extension).
    schedule_extension_sync(
        settings=settings,
        bot=bot,
        bot_data=bot_data,
        tokens=tokens,
        fqdn=fqdn,
        http_client=http_client,
        extension=extension,
        active=active,
        urgent=True,
        removed_participant_id=removed_participant_id,
        ws_participant=ws_participant,
    )
