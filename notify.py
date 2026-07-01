from __future__ import annotations



import asyncio

import logging

import time

from datetime import datetime, timedelta, timezone

import httpx

from dataclasses import dataclass, field

from typing import Any, Callable



from telegram.constants import ParseMode

from telegram.error import BadRequest, RetryAfter



from database import (
    ExtensionLink,
    get_link_by_extension,
    is_chat_blacklisted,
    record_completed_call,
)

from call_display import (
    caller_from_participant,
    format_bold_agent_label,
    format_extension_user_label,
)



logger = logging.getLogger(__name__)



LIVE_CALLS_KEY = "live_calls"

ACTIVE_CALL_HANDLERS_KEY = "active_call_handlers"

RECENT_CALL_HANDLERS_KEY = "recent_call_handlers"

TIMER_UPDATE_SECONDS = 5
TELEGRAM_EDIT_SPACING_SECONDS = 0.4

RECENT_TRANSFER_WINDOW_SECONDS = 45

TRANSFER_OUT_COOLDOWN_SECONDS = 45

RECENT_TRANSFER_OUT_KEY = "recent_transfer_out_extensions"

RECENT_ANNOUNCE_KEY = "recent_call_announces"

ANNOUNCE_LOCKS_KEY = "announce_locks"

ANNOUNCE_DEDUPE_SECONDS = 120

ACTIVE_CALLS_DIGEST_SECONDS = 300

TELEGRAM_SEND_QUEUE_KEY = "telegram_send_queue"
TELEGRAM_SEND_WORKER_KEY = "telegram_send_worker"
TELEGRAM_SEND_SEQ_KEY = "telegram_send_seq"
TELEGRAM_CHAT_LOCKS_KEY = "telegram_chat_send_locks"
TELEGRAM_WORKER_ACTIVE_KEY = "telegram_send_worker_active"
URGENT_SEND_CONCURRENCY = 6


class TelegramSendPriority:
    URGENT = 0
    LOW = 10


def _telegram_send_seq(bot_data: dict) -> int:
    n = int(bot_data.get(TELEGRAM_SEND_SEQ_KEY, 0)) + 1
    bot_data[TELEGRAM_SEND_SEQ_KEY] = n
    return n


def _chat_send_lock(bot_data: dict, chat_id: int) -> asyncio.Lock:
    locks = bot_data.setdefault(TELEGRAM_CHAT_LOCKS_KEY, {})
    if chat_id not in locks:
        locks[chat_id] = asyncio.Lock()
    return locks[chat_id]


def _complete_send_future(future: asyncio.Future, result: Any = None, exc: BaseException | None = None) -> None:
    if future.done():
        return
    try:
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result)
    except asyncio.InvalidStateError:
        pass


def ensure_telegram_send_worker(bot_data: dict) -> None:
    task = bot_data.get(TELEGRAM_SEND_WORKER_KEY)
    if task is not None and not task.done():
        return
    if task is not None and task.done():
        worker_exc = task.exception()
        if worker_exc is not None:
            logger.error("Telegram send worker died; restarting", exc_info=worker_exc)
        else:
            logger.warning("Telegram send worker stopped; restarting")
        bot_data.pop(TELEGRAM_SEND_WORKER_KEY, None)
    bot_data.setdefault(TELEGRAM_SEND_QUEUE_KEY, asyncio.PriorityQueue())
    bot_data.setdefault(
        "telegram_urgent_sem", asyncio.Semaphore(URGENT_SEND_CONCURRENCY)
    )
    bot_data[TELEGRAM_SEND_WORKER_KEY] = asyncio.create_task(
        _telegram_send_worker(bot_data),
        name="telegram-send-worker",
    )
    logger.info("Telegram send worker started")


async def _telegram_send_worker(bot_data: dict) -> None:
    queue: asyncio.PriorityQueue = bot_data[TELEGRAM_SEND_QUEUE_KEY]
    try:
        while True:
            _priority, _seq, future, runner = await queue.get()
            if future.done():
                continue
            bot_data[TELEGRAM_WORKER_ACTIVE_KEY] = True
            try:
                result = await runner()
            except Exception as exc:
                _complete_send_future(future, exc=exc)
            else:
                _complete_send_future(future, result=result)
            finally:
                bot_data[TELEGRAM_WORKER_ACTIVE_KEY] = False
    except asyncio.CancelledError:
        raise


async def _enqueue_telegram(
    bot_data: dict,
    priority: int,
    runner: Callable[[], Any],
) -> Any:
    if bot_data.get(TELEGRAM_WORKER_ACTIVE_KEY):
        return await runner()
    ensure_telegram_send_worker(bot_data)
    queue: asyncio.PriorityQueue = bot_data[TELEGRAM_SEND_QUEUE_KEY]
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    await queue.put((priority, _telegram_send_seq(bot_data), future, runner))
    return await future


def _seconds_until_next_digest_boundary(
    interval_seconds: int = ACTIVE_CALLS_DIGEST_SECONDS,
) -> tuple[float, datetime]:
    """Wait until the next clock-aligned boundary (e.g. :00, :05, :10)."""
    now = datetime.now().astimezone()
    interval_minutes = max(1, interval_seconds // 60)
    next_minute = ((now.minute // interval_minutes) + 1) * interval_minutes
    if next_minute >= 60:
        next_tick = (
            now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        )
    else:
        next_tick = now.replace(minute=next_minute, second=0, microsecond=0)
    delay = max(0.0, (next_tick - now).total_seconds())
    return delay, next_tick





@dataclass

class LiveCall:

    extension: str

    link: ExtensionLink

    started_at: float

    message_ids: dict[int, int] = field(default_factory=dict)

    call_kind: str = "normal"

    transfer_from_link: ExtensionLink | None = None

    transfer_from_extension: str | None = None

    last_displayed_elapsed: int = -1

    caller_name: str = ""

    caller_number: str = ""

    started_at_utc: float = 0.0

    callid: int | None = None

    participant_id: int | None = None

    silent: bool = False





def format_duration(seconds: int) -> str:

    seconds = max(0, seconds)

    minutes, secs = divmod(seconds, 60)

    hours, minutes = divmod(minutes, 60)

    if hours:

        return f"{hours}:{minutes:02d}:{secs:02d}"

    return f"{minutes}:{secs:02d}"





def _user_label(link: ExtensionLink) -> str:
    return format_extension_user_label(link)





def _extension_label(link: ExtensionLink | None, extension: str) -> str:

    if link is not None:

        return _user_label(link)

    return f"ext {extension}"





def _call_message_lines(
    title: str,
    link: ExtensionLink,
    *,
    title_emoji: str = "",
    duration_seconds: int | None = None,
) -> list[str]:
    heading = f"{title_emoji} <b>{title}</b>" if title_emoji else f"<b>{title}</b>"
    lines = [heading, f"👤 {format_bold_agent_label(link)}"]
    if duration_seconds is not None:
        lines.append(
            f"⏱️ <b>Duration</b> · <b>{format_duration(duration_seconds)}</b>"
        )
    return lines


def format_on_phone_message(
    link: ExtensionLink,
    *,
    elapsed_seconds: int | None = None,
) -> str:
    if link.telegram_username:
        agent = f"@{link.telegram_username}"
    elif link.display_name:
        agent = link.display_name
    else:
        agent = f"ext {link.extension}"
    return f"📞🟢 {agent} is on a call"





def format_off_phone_message(
    link: ExtensionLink,
    *,
    duration_seconds: int | None = None,
) -> str:
    if link.telegram_username:
        agent = f"@{link.telegram_username}"
    elif link.display_name:
        agent = link.display_name
    else:
        agent = f"ext {link.extension}"
    dur = f" · {format_duration(duration_seconds)}" if duration_seconds is not None else ""
    return f"📞❌ {agent} call ended{dur}"





def format_transfer_live_message(
    *,
    from_link: ExtensionLink | None,
    from_extension: str,
    to_link: ExtensionLink,
    elapsed_seconds: int | None = None,
) -> str:
    to_label = f"@{to_link.telegram_username}" if to_link.telegram_username else (to_link.display_name or f"ext {to_link.extension}")
    from_label = f"@{from_link.telegram_username}" if from_link and from_link.telegram_username else (getattr(from_link, 'display_name', None) or f"ext {from_extension}")
    return f"🔀 {from_label} → {to_label} is on a call"





def format_transfer_final_message(
    *,
    from_link: ExtensionLink | None,
    from_extension: str,
    to_link: ExtensionLink,
    duration_seconds: int,
) -> str:
    to_label = f"@{to_link.telegram_username}" if to_link.telegram_username else (to_link.display_name or f"ext {to_link.extension}")
    from_label = f"@{from_link.telegram_username}" if from_link and from_link.telegram_username else (getattr(from_link, 'display_name', None) or f"ext {from_extension}")
    dur = format_duration(duration_seconds)
    return f"📞❌ {from_label} → {to_label} call ended · {dur}"





def format_transfer_sent_message(*, from_link: ExtensionLink, to_link: ExtensionLink) -> str:
    from_label = f"@{from_link.telegram_username}" if from_link.telegram_username else (from_link.display_name or f"ext {from_link.extension}")
    to_label = f"@{to_link.telegram_username}" if to_link.telegram_username else (to_link.display_name or f"ext {to_link.extension}")
    return f"🔀 {from_label} transferred to {to_label}"





def format_transfer_sender_ended_message(
    *,
    from_link: ExtensionLink,
    to_link: ExtensionLink,
    duration_seconds: int,
) -> str:
    from_label = f"@{from_link.telegram_username}" if from_link.telegram_username else (from_link.display_name or f"ext {from_link.extension}")
    to_label = f"@{to_link.telegram_username}" if to_link.telegram_username else (to_link.display_name or f"ext {to_link.extension}")
    dur = format_duration(duration_seconds)
    return f"📞❌ {from_label} → {to_label} call ended · {dur}"





def _notify_chat_ids(settings, bot_data: dict) -> list[int]:

    primary = bot_data.get("notify_chat_id") or settings.notify_chat_id

    chat_ids: list[int] = []

    if primary is not None:

        chat_ids.append(primary)

    if settings.copy_to_chat_id is not None and settings.copy_to_chat_id not in chat_ids:

        chat_ids.append(settings.copy_to_chat_id)

    return chat_ids





def _live_calls(bot_data: dict) -> dict[str, LiveCall]:

    return bot_data.setdefault(LIVE_CALLS_KEY, {})





def _active_call_handlers(bot_data: dict) -> dict[int, str]:

    return bot_data.setdefault(ACTIVE_CALL_HANDLERS_KEY, {})





def _recent_call_handlers(bot_data: dict) -> dict[int, dict[str, Any]]:

    return bot_data.setdefault(RECENT_CALL_HANDLERS_KEY, {})





def _announce_locks(bot_data: dict) -> dict[str, asyncio.Lock]:

    return bot_data.setdefault(ANNOUNCE_LOCKS_KEY, {})





def _recent_announces(bot_data: dict) -> dict[str, float]:

    return bot_data.setdefault(RECENT_ANNOUNCE_KEY, {})





def _announce_dedupe_key(extension: str, participant: dict[str, Any] | None) -> str:

    callid = _participant_callid(participant) if participant else None

    if callid is not None:

        return f"{extension}:{callid}"

    return extension





def _mark_recent_announce(bot_data: dict, key: str) -> None:

    now = time.monotonic()

    recent = _recent_announces(bot_data)

    recent[key] = now

    stale_before = now - ANNOUNCE_DEDUPE_SECONDS

    for stale_key, at in list(recent.items()):

        if at < stale_before:

            recent.pop(stale_key, None)





def _is_duplicate_announce(bot_data: dict, key: str) -> bool:

    last = _recent_announces(bot_data).get(key)

    if last is None:

        return False

    return time.monotonic() - last < ANNOUNCE_DEDUPE_SECONDS





def _end_dedupe_key(extension: str) -> str:

    return f"end:{extension}"





def _normalize_dn(value: Any) -> str | None:

    text = str(value or "").strip()

    if not text or text.lower() == "none":

        return None

    return text





def _participant_callid(participant: dict[str, Any]) -> int | None:

    raw = participant.get("callid")

    if raw in (None, ""):

        return None

    try:

        return int(raw)

    except (TypeError, ValueError):

        return None





def detect_transfer_from(

    participant: dict[str, Any],

    extension: str,

    bot_data: dict,

) -> str | None:

    settings = bot_data.get("settings")
    db_path = getattr(settings, "database_path", None) if settings else None

    for field, type_field in (

        ("referred_by_dn", "referred_by_type"),

        ("originated_by_dn", "originated_by_type"),

        ("on_behalf_of_dn", "on_behalf_of_type"),

    ):

        dn = _normalize_dn(participant.get(field))

        if not dn or dn == extension:

            continue

        dn_type = str(participant.get(type_field) or "").strip().lower()

        if dn_type and dn_type not in {"none", "extension", "0"}:

            continue

        # Only treat as a transfer if the DN is a known linked agent extension.
        # Queue/IVR extensions are not in the database and must not trigger transfer UI.
        if db_path:
            link = get_link_by_extension(db_path, dn)
            if link is None:
                continue

        return dn



    callid = _participant_callid(participant)

    if callid is None:

        return None



    active_handler = _active_call_handlers(bot_data).get(callid)

    if active_handler and active_handler != extension:

        return active_handler



    recent = _recent_call_handlers(bot_data).get(callid)

    if recent and recent.get("extension") != extension:

        age = time.monotonic() - float(recent.get("at", 0))

        if age <= RECENT_TRANSFER_WINDOW_SECONDS:

            return str(recent["extension"])



    return None





def register_call_handler(

    participant: dict[str, Any] | None,

    extension: str,

    bot_data: dict,

) -> None:

    if participant is None:

        return

    callid = _participant_callid(participant)

    if callid is not None:

        _active_call_handlers(bot_data)[callid] = extension





def register_recent_call_handler(

    participant: dict[str, Any] | None,

    extension: str,

    bot_data: dict,

) -> None:

    if participant is None:

        return

    callid = _participant_callid(participant)

    if callid is None:

        return



    now = time.monotonic()

    _recent_call_handlers(bot_data)[callid] = {

        "extension": extension,

        "at": now,

    }

    _active_call_handlers(bot_data).pop(callid, None)



    stale_before = now - RECENT_TRANSFER_WINDOW_SECONDS

    recent = _recent_call_handlers(bot_data)

    for key, info in list(recent.items()):

        if float(info.get("at", 0)) < stale_before:

            recent.pop(key, None)





def _recent_transfer_out(bot_data: dict) -> dict[str, float]:

    return bot_data.setdefault(RECENT_TRANSFER_OUT_KEY, {})





def register_transfer_out(bot_data: dict, extension: str) -> None:

    now = time.monotonic()

    recent = _recent_transfer_out(bot_data)

    recent[extension] = now

    stale_before = now - TRANSFER_OUT_COOLDOWN_SECONDS

    for ext, at in list(recent.items()):

        if at < stale_before:

            recent.pop(ext, None)





def was_recent_transfer_out(bot_data: dict, extension: str) -> bool:

    at = _recent_transfer_out(bot_data).get(extension)

    if at is None:

        return False

    return time.monotonic() - at <= TRANSFER_OUT_COOLDOWN_SECONDS





def _live_call_text(live_call: LiveCall, elapsed: int) -> str:

    if live_call.call_kind == "transfer":

        return format_transfer_live_message(

            from_link=live_call.transfer_from_link,

            from_extension=live_call.transfer_from_extension or "?",

            to_link=live_call.link,

            elapsed_seconds=elapsed,

        )

    return format_on_phone_message(
        live_call.link,
        elapsed_seconds=elapsed,
    )





def _live_call_final_text(live_call: LiveCall, duration: int) -> str:

    if live_call.call_kind == "transfer":

        return format_transfer_final_message(

            from_link=live_call.transfer_from_link,

            from_extension=live_call.transfer_from_extension or "?",

            to_link=live_call.link,

            duration_seconds=duration,

        )

    return format_off_phone_message(
        live_call.link,
        duration_seconds=duration,
    )





async def send_to_notify_chats(
    bot,
    settings,
    bot_data: dict,
    *,
    text: str,
    priority: int = TelegramSendPriority.URGENT,
) -> None:
    chat_ids = _notify_chat_ids(settings, bot_data)
    if not chat_ids:
        return

    async def _send_all() -> None:
        await asyncio.gather(
            *(
                _telegram_send_message(
                    bot,
                    bot_data,
                    chat_id=chat_id,
                    text=text,
                    priority=priority,
                    inline=True,
                )
                for chat_id in chat_ids
            )
        )

    if priority == TelegramSendPriority.URGENT:
        await _send_all()
    else:
        await _enqueue_telegram(bot_data, priority, _send_all)





async def _telegram_send_message(
    bot,
    bot_data: dict,
    *,
    chat_id: int,
    text: str,
    priority: int = TelegramSendPriority.URGENT,
    reply_markup=None,
    inline: bool = False,
):
    async def _send() -> Any:
        while True:
            try:
                return await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 0.5)

    if inline or priority == TelegramSendPriority.URGENT:
        ensure_telegram_send_worker(bot_data)
        urgent_sem: asyncio.Semaphore = bot_data["telegram_urgent_sem"]
        async with urgent_sem:
            async with _chat_send_lock(bot_data, chat_id):
                return await _send()
    if bot_data.get(TELEGRAM_WORKER_ACTIVE_KEY):
        async with _chat_send_lock(bot_data, chat_id):
            return await _send()
    return await _enqueue_telegram(bot_data, priority, _send)





async def _start_live_call(

    bot,

    settings,

    bot_data: dict,

    *,

    extension: str,

    link: ExtensionLink,

    initial_text: str,

    call_kind: str = "normal",

    transfer_from_link: ExtensionLink | None = None,

    transfer_from_extension: str | None = None,

    caller_name: str = "",

    caller_number: str = "",

    reply_markup=None,

    callid: int | None = None,

    participant_id: int | None = None,

) -> None:

    live_calls = _live_calls(bot_data)

    if extension in live_calls:

        return

    live_call = LiveCall(

        extension=extension,

        link=link,

        started_at=time.monotonic(),

        call_kind=call_kind,

        transfer_from_link=transfer_from_link,

        transfer_from_extension=transfer_from_extension,

        last_displayed_elapsed=0,

        caller_name=caller_name,

        caller_number=caller_number,

        started_at_utc=time.time(),

        callid=callid,

        participant_id=participant_id,

    )

    live_calls[extension] = live_call
    bot_data[f"call_start_utc:{extension}"] = live_call.started_at_utc

    try:
        chat_ids = _notify_chat_ids(settings, bot_data)

        async def _send_initial() -> None:
            async def _send_one(chat_id: int) -> None:
                message = await _telegram_send_message(
                    bot,
                    bot_data,
                    chat_id=chat_id,
                    text=initial_text,
                    reply_markup=reply_markup,
                    inline=True,
                )
                if message is None:
                    return
                live_call.message_ids[chat_id] = message.message_id

            await asyncio.gather(*(_send_one(cid) for cid in chat_ids))

        await _send_initial()
    except Exception:
        live_calls.pop(extension, None)
        raise


async def resume_call_after_restart(
    bot,
    settings,
    bot_data: dict,
    link: ExtensionLink,
    *,
    participant: dict[str, Any] | None = None,
    callid: int | None = None,
    participant_id: int | None = None,
) -> bool:
    """Post a fresh on-phone message and timer for a call active before bot restart."""
    extension = link.extension
    live_calls = _live_calls(bot_data)
    existing = live_calls.get(extension)
    if existing is not None and not existing.silent and existing.message_ids:
        return False

    if existing is not None:
        live_calls.pop(extension, None)

    if participant is not None:
        if callid is None:
            callid = _participant_callid(participant)
        if participant_id is None:
            try:
                participant_id = int(participant.get("id"))
            except (TypeError, ValueError):
                participant_id = None

    dedupe_key = _announce_dedupe_key(extension, participant)
    _recent_announces(bot_data).pop(dedupe_key, None)

    return await announce_call_started(
        bot,
        settings,
        bot_data,
        link,
        participant=participant,
        callid=callid,
        participant_id=participant_id,
    )


async def _stop_live_call(

    bot,

    bot_data: dict,

    extension: str,

    *,

    final_text: str | None = None,

    final_text_factory: Callable[[LiveCall, int], str] | None = None,

) -> LiveCall | None:

    from call_end import (
        consume_telegram_hangup_label,
        consume_ws_end_reason,
    )

    live_calls = _live_calls(bot_data)

    live_call = live_calls.pop(extension, None)

    if live_call is None:

        return None

    duration = int(time.monotonic() - live_call.started_at)
    settings = bot_data.get("settings")
    if settings is not None:
        record_completed_call(
            settings.database_path,
            extension=live_call.extension,
            telegram_user_id=live_call.link.telegram_user_id,
            telegram_username=live_call.link.telegram_username,
            display_name=live_call.link.display_name,
            duration_seconds=duration,
            caller_name=live_call.caller_name,
            caller_number=live_call.caller_number,
            call_kind=live_call.call_kind,
            started_at_utc=live_call.started_at_utc,
        )
        bot_data.pop(f"call_start_utc:{extension}", None)
        try:
            from quiet_wins import maybe_quiet_win_handle_time

            await maybe_quiet_win_handle_time(
                bot,
                settings,
                live_call.link,
                duration,
            )
        except Exception:
            logger.exception(
                "Quiet win handle-time check failed for ext %s",
                extension,
            )

    if live_call.silent:
        return live_call

    ended_by_html = consume_telegram_hangup_label(bot_data, extension)
    if ended_by_html is None:
        ended_by_html = consume_ws_end_reason(bot_data, extension)
    if ended_by_html:
        logger.info(
            "Call ended ext %s: ended_by=%s",
            extension,
            ended_by_html,
        )

    if final_text is None:

        if final_text_factory is not None:

            final_text = final_text_factory(live_call, duration)

        else:

            final_text = _live_call_final_text(live_call, duration)



    settings = bot_data.get("settings")
    if settings is not None:
        await send_to_notify_chats(
            bot,
            settings,
            bot_data,
            text=final_text,
        )

    return live_call





async def announce_call_started(

    bot,

    settings,

    bot_data: dict,

    link: ExtensionLink,

    *,

    participant: dict[str, Any] | None = None,

    callid: int | None = None,

    participant_id: int | None = None,

) -> bool:

    extension = link.extension

    locks = _announce_locks(bot_data)

    if extension not in locks:

        locks[extension] = asyncio.Lock()



    async with locks[extension]:

        if extension in _live_calls(bot_data):

            return False

        if was_recent_transfer_out(bot_data, extension):

            logger.info(

                "Skipping on-phone after transfer out ext %s",

                extension,

            )

            return False

        notify_chat_id = bot_data.get("notify_chat_id") or settings.notify_chat_id
        if notify_chat_id is not None and is_chat_blacklisted(
            settings.database_path,
            notify_chat_id,
            telegram_user_id=link.telegram_user_id,
            telegram_username=link.telegram_username,
        ):
            logger.info(
                "Skipping on-phone for blocked user ext %s (@%s)",
                extension,
                link.telegram_username,
            )
            return False

        dedupe_key = _announce_dedupe_key(extension, participant)

        if _is_duplicate_announce(bot_data, dedupe_key):

            logger.info("Skipping duplicate on-phone announce for %s", dedupe_key)

            return False



        register_call_handler(participant, extension, bot_data)

        caller_name, caller_number = caller_from_participant(participant)

        if callid is None and participant is not None:

            callid = _participant_callid(participant)

        if participant_id is None and participant is not None:

            try:

                participant_id = int(participant.get("id"))

            except (TypeError, ValueError):

                participant_id = None

        await _start_live_call(

            bot,

            settings,

            bot_data,

            extension=extension,

            link=link,

            initial_text=format_on_phone_message(link),

            caller_name=caller_name,

            caller_number=caller_number,

            callid=callid,

            participant_id=participant_id,

        )

        _mark_recent_announce(bot_data, dedupe_key)

        pending_ms = bot_data.pop(f"sync_pending_ms:{extension}", None)
        ws_ms = None
        ws_ts = bot_data.pop(f"ws_event_ts:{extension}", None)
        if ws_ts is not None:
            ws_ms = (time.monotonic() - ws_ts) * 1000
        if ws_ms is not None and pending_ms is not None:
            logger.info(
                "Announced on phone: ext %s (%.0fms after WS, %.0fms sync+send)",
                extension,
                ws_ms,
                pending_ms,
            )
        elif pending_ms is not None:
            logger.info(
                "Announced on phone: ext %s (%.0fms after sync)",
                extension,
                pending_ms,
            )
        else:
            logger.info("Announced on phone: ext %s", extension)

        return True





def _record_orphan_call_end(
    settings,
    bot_data: dict,
    link: ExtensionLink,
    participant: dict[str, Any] | None,
) -> bool:
    """Count a completed call when off-phone fired without a tracked live call."""
    started_at_utc = bot_data.pop(f"call_start_utc:{link.extension}", None)
    if started_at_utc is None:
        return False

    caller_name, caller_number = (
        caller_from_participant(participant) if participant else ("", "")
    )
    duration = max(1, int(time.time() - started_at_utc))
    record_completed_call(
        settings.database_path,
        extension=link.extension,
        telegram_user_id=link.telegram_user_id,
        telegram_username=link.telegram_username,
        display_name=link.display_name,
        duration_seconds=duration,
        caller_name=caller_name,
        caller_number=caller_number,
        call_kind="normal",
        started_at_utc=started_at_utc,
    )
    return True


async def announce_call_ended(

    bot,

    settings,

    bot_data: dict,

    link: ExtensionLink,

    *,

    participant: dict[str, Any] | None = None,

) -> None:

    extension = link.extension
    locks = _announce_locks(bot_data)
    if extension not in locks:
        locks[extension] = asyncio.Lock()

    async with locks[extension]:
        dedupe_key = _end_dedupe_key(extension)
        if _is_duplicate_announce(bot_data, dedupe_key):
            logger.info(
                "Skipping duplicate off-phone announce for ext %s", extension
            )
            return

        register_recent_call_handler(participant, extension, bot_data)

        live_call = await _stop_live_call(bot, bot_data, extension)

        if live_call is not None:
            _mark_recent_announce(bot_data, dedupe_key)

            if live_call.silent:
                logger.info(
                    "Ended pre-restart call (no Telegram): ext %s", extension
                )
            else:
                from transcript import schedule_transcript_delivery

                schedule_transcript_delivery(bot, settings, bot_data, live_call)

                logger.info("Announced off phone: ext %s", extension)

            return

        if not _record_orphan_call_end(settings, bot_data, link, participant):
            logger.debug(
                "Skipping orphan off-phone (no tracked call) ext %s", extension
            )
            return

        _mark_recent_announce(bot_data, dedupe_key)
        await send_to_notify_chats(
            bot,
            settings,
            bot_data,
            text=format_off_phone_message(link),
        )





async def announce_transfer_received(

    bot,

    settings,

    bot_data: dict,

    *,

    from_extension: str,

    to_link: ExtensionLink,

    active: dict[str, set[int]],

    participant: dict[str, Any] | None = None,

) -> None:

    from_link = get_link_by_extension(settings.database_path, from_extension)



    register_transfer_out(bot_data, from_extension)



    if from_extension in active:

        active[from_extension].clear()



    sender_live = _live_calls(bot_data).get(from_extension)

    if sender_live is not None:

        sender_link = from_link or sender_live.link

        duration = max(1, int(time.monotonic() - sender_live.started_at))

        await _stop_live_call(

            bot,

            bot_data,

            from_extension,

            final_text=format_transfer_sender_ended_message(

                from_link=sender_link,

                to_link=to_link,

                duration_seconds=duration,

            ),

        )



    register_call_handler(participant, to_link.extension, bot_data)

    caller_name, caller_number = caller_from_participant(participant)

    await _start_live_call(

        bot,

        settings,

        bot_data,

        extension=to_link.extension,

        link=to_link,

        initial_text=format_transfer_live_message(

            from_link=from_link,

            from_extension=from_extension,

            to_link=to_link,
        ),

        call_kind="transfer",

        transfer_from_link=from_link,

        transfer_from_extension=from_extension,

        caller_name=caller_name,

        caller_number=caller_number,

    )





async def live_call_timers_loop(bot, bot_data: dict) -> None:
    """No-op: ON CALL and CALL ENDED are separate new messages (no timer edits)."""
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        raise





async def _delete_one_live_message(
    bot,
    *,
    chat_id: int,
    message_id: int,
) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest as exc:
        logger.debug(
            "Could not delete call message in chat %s: %s", chat_id, exc
        )
    except Exception:
        logger.exception("Failed to delete call message in chat %s", chat_id)


async def _delete_live_messages(
    bot,
    message_ids: dict[int, int],
) -> None:
    if not message_ids:
        return
    await asyncio.gather(
        *(
            _delete_one_live_message(
                bot,
                chat_id=chat_id,
                message_id=message_id,
            )
            for chat_id, message_id in message_ids.items()
        )
    )


async def _edit_one_live_message(
    bot,
    bot_data: dict,
    *,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup=None,
) -> bool:
    while True:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            return True
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return True
            logger.warning(
                "Could not edit call timer in chat %s: %s", chat_id, exc
            )
            return False
        except Exception:
            logger.exception("Failed to update call timer in chat %s", chat_id)
            return False


async def _edit_live_messages(
    bot,
    bot_data: dict,
    message_ids: dict[int, int],
    text: str,
    reply_markup=None,
) -> None:
    if not message_ids:
        return
    await asyncio.gather(
        *(
            _edit_one_live_message(
                bot,
                bot_data,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
            for chat_id, message_id in message_ids.items()
        )
    )


def format_active_calls_digest(
    bot_data: dict,
    *,
    verified: list[tuple[ExtensionLink, dict[str, Any]]] | None = None,
) -> str | None:
    """Build digest from 3CX-verified calls (not stale in-memory state alone)."""
    if verified is None:
        live_calls = _live_calls(bot_data)
        if not live_calls:
            return None
        verified = [(live_calls[ext].link, {}) for ext in live_calls]

    if not verified:
        return None

    now = time.monotonic()
    live_calls = _live_calls(bot_data)
    lines = [f"📋 <b>ACTIVE CALLS</b> · {len(verified)}"]
    entries: list[str] = []
    for link, participant in sorted(
        verified,
        key=lambda item: int(item[0].extension) if item[0].extension.isdigit() else item[0].extension,
    ):
        extension = link.extension
        live_call = live_calls.get(extension)
        if live_call is not None:
            elapsed = int(now - live_call.started_at)
            call_kind = live_call.call_kind
        else:
            elapsed = 0
            call_kind = "normal"

        if call_kind == "transfer":
            entry = (
                f"🔀 {format_bold_agent_label(link)} · "
                f"⏱️ <b>{format_duration(elapsed)}</b>"
            )
        else:
            entry = (
                f"📞 {format_bold_agent_label(link)} · "
                f"⏱️ <b>{format_duration(elapsed)}</b>"
            )
        entries.append(entry)

    return "\n\n".join([lines[0], *entries])


async def active_calls_digest_loop(bot, settings, bot_data: dict) -> None:
    """Post a summary of on-phone agents every 5 minutes on the clock."""
    from call_extension_sync import (
        fetch_callcontrol_participants_map,
        list_extension_participants,
    )
    from database import list_links
    from threex_api import filter_real_connected_participants
    from threex_token import get_token_holder

    last_digest_hash: str | None = None

    try:
        while True:
            delay, next_tick = _seconds_until_next_digest_boundary()
            # Always wait at least 60s after startup before first digest
            delay = max(delay, 60)
            logger.info(
                "Next active calls digest at %s (in %.0fs)",
                next_tick.strftime("%H:%M"),
                delay,
            )
            await asyncio.sleep(delay)

            if not _notify_chat_ids(settings, bot_data):
                logger.warning("Digest skipped: no notify chat configured")
                continue
            if not settings.threex_enabled:
                logger.warning("Digest skipped: 3CX disabled")
                continue

            tokens = get_token_holder(bot_data, settings)
            fqdn = settings.threex_fqdn
            limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
            verified: list[tuple[ExtensionLink, dict[str, Any]]] = []

            try:
                async with httpx.AsyncClient(timeout=15, limits=limits) as http_client:
                    snapshot = await fetch_callcontrol_participants_map(
                        fqdn, tokens, http_client=http_client
                    )
                    live_extensions = set(_live_calls(bot_data).keys())
                    for link in list_links(settings.database_path):
                        extension = link.extension
                        snap_participants = filter_real_connected_participants(
                            snapshot.get(extension, []),
                            extension=extension,
                        )
                        participants: list[dict[str, Any]] = []
                        if snap_participants or extension in live_extensions:
                            fetched = await list_extension_participants(
                                fqdn,
                                tokens,
                                extension,
                                http_client=http_client,
                            )
                            if fetched is not None:
                                participants = filter_real_connected_participants(
                                    fetched,
                                    extension=extension,
                                )
                            else:
                                participants = snap_participants
                        if participants:
                            verified.append((link, participants[0]))
            except Exception:
                logger.exception("Digest snapshot failed")
                continue

            if not verified:
                logger.info("Digest skipped: no active calls")
                continue

            text = format_active_calls_digest(bot_data, verified=verified)
            digest_hash = str(hash(text))
            if digest_hash == last_digest_hash:
                logger.info("Digest skipped: same as last post")
                continue
            last_digest_hash = digest_hash
            await send_to_notify_chats(
                bot,
                settings,
                bot_data,
                text=text,
                priority=TelegramSendPriority.LOW,
            )
            logger.info("Posted active calls digest (%d on phone)", len(verified))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Active calls digest loop stopped")


async def clear_live_calls_state(bot_data: dict) -> int:
    """Drop in-memory on-phone state without posting off-phone messages."""
    live_calls = _live_calls(bot_data)
    extensions = list(live_calls.keys())
    for extension in extensions:
        live_call = live_calls.pop(extension, None)
        if live_call is None:
            continue
    _active_call_handlers(bot_data).clear()
    _recent_call_handlers(bot_data).clear()
    _recent_announces(bot_data).clear()
    return len(extensions)


async def daily_summary_loop(bot, settings, bot_data: dict) -> None:
    """Post a daily call summary at midnight (STATS_TIMEZONE)."""
    from handlers.stats_period import stats_timezone
    from database import list_recent_completed_calls

    try:
        while True:
            tz = stats_timezone()
            now = datetime.now(tz)
            # Sleep until next midnight
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=5, microsecond=0
            )
            wait = (tomorrow - now).total_seconds()
            await asyncio.sleep(wait)

            # Build summary for the day that just ended
            since = datetime.now(tz).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).astimezone(timezone.utc) - timedelta(days=1)
            until = since + timedelta(days=1)

            try:
                calls = list_recent_completed_calls(
                    settings.database_path, limit=10000, since=since
                )
                calls = [c for c in calls if c.ended_at and c.ended_at < until.isoformat()]
            except Exception:
                calls = []

            if not calls:
                continue

            total = len(calls)
            avg_dur = int(sum(c.duration_seconds for c in calls) / total) if total else 0

            # Per-agent counts
            agent_counts: dict[str, int] = {}
            for c in calls:
                label = f"@{c.telegram_username}" if c.telegram_username else (c.display_name or f"ext {c.extension}")
                agent_counts[label] = agent_counts.get(label, 0) + 1

            top = sorted(agent_counts.items(), key=lambda x: x[1], reverse=True)

            date_label = since.astimezone(tz).strftime("%a %d %b")
            lines = [f"📊 <b>Daily Summary · {date_label}</b>"]
            lines.append(f"📞 Total calls: <b>{total}</b>")
            lines.append(f"⏱️ Avg duration: <b>{format_duration(avg_dur)}</b>")
            if top:
                lines.append("")
                lines.append("🏆 <b>Top agents:</b>")
                for i, (agent, count) in enumerate(top[:5], 1):
                    lines.append(f"  {i}. {agent} · {count} call{'s' if count != 1 else ''}")

            text = "\n".join(lines)
            try:
                await send_to_notify_chats(bot, settings, bot_data, text=text)
                logger.info("Posted daily summary: %d calls on %s", total, date_label)
            except Exception:
                logger.exception("Failed to post daily summary")

    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Daily summary loop stopped")
