"""Start a live listen session and invite the listener via Telegram."""

from __future__ import annotations

import html
import os

from telegram import Bot, InlineKeyboardMarkup

from database import get_link_by_extension
from handlers.telegram_listen_invite import build_listen_call_invite, format_call_title
from listen_stream import listen_public_base, start_listen_session, telegram_listen_ready
from threex_api import ThreeCXApiError, admin_extension, get_client


def _agent_label(settings, extension: str) -> str:
    link = get_link_by_extension(settings.database_path, extension)
    if link is None:
        return f"ext {extension}"
    if link.telegram_username:
        return f"@{link.telegram_username} (ext {extension})"
    if link.display_name:
        return f"{link.display_name} (ext {extension})"
    return f"ext {extension}"


def _phone_fallback_enabled() -> bool:
    return os.getenv("LISTEN_USE_PHONE_FALLBACK", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _named_invite_enabled() -> bool:
    return os.getenv("LISTEN_NAMED_INVITE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


async def _resolve_caller_info(
    bot_data: dict,
    settings,
    *,
    extension: str,
    participant_id: int,
    caller_name: str = "",
    caller_number: str = "",
    agent_label: str = "",
) -> tuple[str, str, str]:
    name = caller_name.strip()
    number = caller_number.strip()
    agent = agent_label.strip() or _agent_label(settings, extension)

    if name or number:
        return name, number, agent

    client = get_client(bot_data, settings)
    try:
        participant = await client.get_participant(extension, participant_id)
        if participant:
            raw_name = str(participant.get("party_caller_name") or "").strip()
            raw_number = str(participant.get("party_caller_id") or "").strip()
            if raw_name and raw_name != raw_number:
                name = raw_name
            number = raw_number or name
    except ThreeCXApiError:
        pass

    return name, number, agent


def create_listen_url(
    bot_data: dict,
    settings,
    *,
    extension: str,
    participant_id: int,
    caller_name: str = "",
    caller_number: str = "",
    agent_label: str = "",
) -> str:
    """Start stream in background and return the public listen URL immediately."""
    session = start_listen_session(
        bot_data,
        settings,
        extension=extension,
        participant_id=participant_id,
        caller_name=caller_name.strip(),
        caller_number=caller_number.strip(),
        agent_label=agent_label.strip() or f"ext {extension}",
    )
    return f"{listen_public_base(settings)}/listen/{session.public_id}"


async def start_live_listen_with_fallback(
    bot_data: dict,
    settings,
    bot: Bot,
    *,
    extension: str,
    participant_id: int,
    caller_name: str = "",
    caller_number: str = "",
    agent_label: str = "",
) -> tuple[str, InlineKeyboardMarkup | None]:
    name, number, agent = await _resolve_caller_info(
        bot_data,
        settings,
        extension=extension,
        participant_id=participant_id,
        caller_name=caller_name,
        caller_number=caller_number,
        agent_label=agent_label,
    )
    title = format_call_title(name, number)

    listen_url: str | None = None
    if telegram_listen_ready(settings):
        listen_url = create_listen_url(
            bot_data,
            settings,
            extension=extension,
            participant_id=participant_id,
            caller_name=name,
            caller_number=number,
            agent_label=agent,
        )

    phone_listen_ext: str | None = None
    phone_fallback = _phone_fallback_enabled()
    if phone_fallback:
        phone_listen_ext = admin_extension(settings)

    message, markup, _ = await build_listen_call_invite(
        bot,
        settings,
        caller_name=name,
        caller_number=number,
        agent_label=agent,
        listen_url=listen_url,
        phone_listen_ext=phone_listen_ext,
        create_room_invite=_named_invite_enabled(),
        listen_extension=extension,
        listen_participant_id=participant_id,
        show_phone_button=phone_fallback,
    )

    if phone_fallback and phone_listen_ext:
        message += (
            f"\n\n<i>Tap <b>📞 Listen on phone — ext {html.escape(phone_listen_ext)}</b> "
            "then answer that extension on your 3CX app.</i>"
        )
    elif listen_url:
        message += (
            "\n\n<i>Mini App audio often fails on agent extensions — use "
            "<b>Listen on phone</b> if enabled.</i>"
        )

    if not telegram_listen_ready(settings):
        message += (
            "\n\n<i>Add LISTEN_PUBLIC_URL=https://your-tunnel-url to .env "
            "then restart the bot to listen inside Telegram.</i>"
        )
    return message, markup
