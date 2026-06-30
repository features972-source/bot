"""Fetch post-call transcripts from 3CX XAPI and post to Telegram."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from call_display import format_caller_html, format_extension_user_plain
from config import Settings
from database import ExtensionLink
from notify import LiveCall, send_to_notify_chats
from threex_token import THREECX_TOKENS_KEY, TokenHolder, fetch_token

logger = logging.getLogger(__name__)

TELEGRAM_MAX = 4000
POLL_INTERVAL_SECONDS = 60
MAX_WAIT_SECONDS = 600
TRANSCRIPT_DISABLED_KEY = "transcript_api_disabled"

ALERT_OWNER_ID = 8217438821

_PLATFORM_KEYWORDS = re.compile(
    r"\b("
    r"whatsapp|whats app|what'?s app"
    r"|moving (you |them |the customer )?over"
    r"|move (you |them |the customer )?over"
    r"|different (software|platform|app|application|system)"
    r"|switch(ing)? (you |them |the customer )?(to|over)"
    r"|telegram|signal|viber"
    r")\b",
    re.IGNORECASE,
)


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


async def _fetch_token(settings: Settings, bot_data: dict | None = None) -> str | None:
    if bot_data and bot_data.get(TRANSCRIPT_DISABLED_KEY):
        return None
    holder = (bot_data or {}).get(THREECX_TOKENS_KEY)
    if isinstance(holder, TokenHolder):
        return await holder.get()
    return await fetch_token(settings)


async def _list_recordings(
    settings: Settings,
    token: str,
    *,
    started_after: datetime,
    bot_data: dict | None = None,
) -> list[dict[str, Any]]:
    start_iso = started_after.strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "$top": 25,
        "$orderby": "StartTime desc",
        "$select": "Id,Transcription,Summary,FromCallerNumber,FromDisplayName,ToDn,FromDn,StartTime",
        "$filter": f"StartTime ge {start_iso}",
    }
    url = f"https://{settings.threex_fqdn}/xapi/v1/Recordings"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code >= 400:
            if response.status_code in {401, 403} and bot_data is not None:
                bot_data[TRANSCRIPT_DISABLED_KEY] = True
                logger.warning(
                    "Transcripts disabled for this session (Recordings API %s). "
                    "Enable XAPI Recordings access in 3CX or set TRANSCRIPT_ENABLED=false.",
                    response.status_code,
                )
            else:
                logger.warning(
                    "Recordings API failed (%s): %s",
                    response.status_code,
                    response.text[:300],
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


def _recording_matches(
    recording: dict[str, Any],
    *,
    extension: str,
    caller_number: str,
) -> bool:
    ext = extension.strip()
    to_dn = str(recording.get("ToDn") or "").strip()
    from_dn = str(recording.get("FromDn") or "").strip()
    if ext not in {to_dn, from_dn}:
        return False
    if not caller_number:
        return True
    rec_num = _digits(str(recording.get("FromCallerNumber") or ""))
    want = _digits(caller_number)
    if not want:
        return True
    if not rec_num:
        return True
    return want in rec_num or rec_num in want


def _pick_recording(
    recordings: list[dict[str, Any]],
    *,
    extension: str,
    caller_number: str,
) -> dict[str, Any] | None:
    for recording in recordings:
        if not _recording_matches(
            recording, extension=extension, caller_number=caller_number
        ):
            continue
        if recording.get("Transcription") or recording.get("Summary"):
            return recording
    for recording in recordings:
        if _recording_matches(
            recording, extension=extension, caller_number=caller_number
        ):
            return recording
    return None


def _user_label(link: ExtensionLink) -> str:
    return format_extension_user_plain(link)


def _format_transcript_message(live_call: LiveCall, recording: dict[str, Any]) -> str:
    agent = html.escape(_user_label(live_call.link))
    caller = format_caller_html(live_call.caller_name, live_call.caller_number)
    header = f"📝 <b>Call transcript</b> — {agent}"
    if caller:
        header += f" with {caller}"

    parts = [header]
    summary = str(recording.get("Summary") or "").strip()
    if summary:
        parts.append(f"\n💡 <b>Summary</b>\n{html.escape(summary)}")

    transcription = str(recording.get("Transcription") or "").strip()
    if transcription:
        parts.append(f"\n💬 <b>Transcript</b>\n{html.escape(transcription)}")
    elif not summary:
        parts.append("\n⏳ <i>Recording found but transcription is not ready yet.</i>")

    text = "\n".join(parts)
    if len(text) > TELEGRAM_MAX:
        text = text[: TELEGRAM_MAX - 20] + "\n\n<i>…truncated</i>"
    return text


async def _alert_owner_if_keyword(
    bot,
    live_call: LiveCall,
    transcription: str,
) -> None:
    match = _PLATFORM_KEYWORDS.search(transcription)
    if not match:
        return
    agent = f"@{live_call.link.telegram_username}" if live_call.link.telegram_username else (live_call.link.display_name or f"ext {live_call.link.extension}")
    caller = live_call.caller_number or live_call.caller_name or "unknown number"
    keyword = match.group(0)
    try:
        await bot.send_message(
            chat_id=ALERT_OWNER_ID,
            text=(
                f"⚠️ <b>Platform alert</b>\n\n"
                f"{agent} mentioned <b>{html.escape(keyword)}</b> "
                f"during a call with <b>{html.escape(caller)}</b>"
            ),
            parse_mode="HTML",
        )
        logger.info("Sent platform keyword alert for ext %s (keyword: %s)", live_call.extension, keyword)
    except Exception:
        logger.exception("Failed to send platform keyword alert")


def schedule_transcript_delivery(
    bot,
    settings: Settings,
    bot_data: dict,
    live_call: LiveCall,
) -> None:
    if not settings.transcript_enabled or not settings.threex_enabled:
        return
    started_after = datetime.fromtimestamp(live_call.started_at_utc, tz=timezone.utc) - timedelta(
        minutes=2
    )
    asyncio.create_task(
        _deliver_transcript(
            bot,
            settings,
            bot_data,
            live_call=live_call,
            started_after=started_after,
        ),
        name=f"transcript-{live_call.extension}",
    )


async def _deliver_transcript(
    bot,
    settings: Settings,
    bot_data: dict,
    *,
    live_call: LiveCall,
    started_after: datetime,
) -> None:
    if bot_data.get(TRANSCRIPT_DISABLED_KEY):
        logger.info("Transcript disabled — skipping for ext %s", live_call.extension)
        return
    logger.info("Transcript polling started for ext %s (caller: %s)", live_call.extension, live_call.caller_number)
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    poll = 0
    while time.monotonic() < deadline:
        if bot_data.get(TRANSCRIPT_DISABLED_KEY):
            return
        token = await _fetch_token(settings, bot_data)
        if token is None:
            logger.warning("Transcript polling: no token for ext %s", live_call.extension)
            return
        poll += 1
        recordings = await _list_recordings(
            settings, token, started_after=started_after, bot_data=bot_data
        )
        logger.info("Transcript poll #%d for ext %s: %d recording(s) found", poll, live_call.extension, len(recordings))
        recording = _pick_recording(
            recordings,
            extension=live_call.extension,
            caller_number=live_call.caller_number,
        )
        if recording is not None:
            transcription = str(recording.get("Transcription") or "").strip()
            summary = str(recording.get("Summary") or "").strip()
            logger.info("Transcript poll #%d ext %s: recording found, transcription=%s summary=%s",
                poll, live_call.extension, bool(transcription), bool(summary))
            if transcription or summary:
                await send_to_notify_chats(
                    bot,
                    settings,
                    bot_data,
                    text=_format_transcript_message(live_call, recording),
                )
                logger.info("Posted transcript for ext %s", live_call.extension)
                if transcription:
                    await _alert_owner_if_keyword(bot, live_call, transcription)
                return
        else:
            logger.info("Transcript poll #%d ext %s: no matching recording yet", poll, live_call.extension)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    logger.info(
        "No transcript available for ext %s within %ss after %d polls",
        live_call.extension,
        MAX_WAIT_SECONDS,
        poll,
    )
