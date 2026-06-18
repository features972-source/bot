"""Create a named Telegram invite link for live listen sessions."""

from __future__ import annotations

import html
import logging
import time

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config import Settings
from listen_stream import telegram_listen_ready

logger = logging.getLogger(__name__)

INVITE_TTL_SECONDS = 3600


def format_call_title(caller_name: str, caller_number: str) -> str:
    name = caller_name.strip()
    number = caller_number.strip()
    if name and number and name == number:
        return number
    if name and number:
        return f"{name} · {number}"
    if number:
        return number
    return name or "Unknown caller"


def invite_link_name(caller_name: str, caller_number: str) -> str:
    """Telegram invite link names are limited to 32 characters."""
    title = format_call_title(caller_name, caller_number)
    if len(title) <= 32:
        return title
    number = caller_number.strip()
    if number:
        return number[-32:]
    return title[:32]


def _listen_group_id(settings: Settings) -> int | None:
    return settings.listen_chat_id or settings.notify_chat_id


def _listen_button(text: str, listen_url: str | None) -> InlineKeyboardButton | None:
    if not listen_url:
        return None
    if listen_url.startswith("https://"):
        return InlineKeyboardButton(text, web_app=WebAppInfo(url=listen_url))
    return InlineKeyboardButton(text, url=listen_url)


async def build_listen_call_invite(
    bot: Bot,
    settings: Settings,
    *,
    caller_name: str,
    caller_number: str,
    agent_label: str,
    listen_url: str | None = None,
    phone_listen_ext: str | None = None,
    create_room_invite: bool = False,
    listen_extension: str | None = None,
    listen_participant_id: int | None = None,
    show_phone_button: bool = False,
) -> tuple[str, InlineKeyboardMarkup, str | None]:
    """Build invite message; Mini App button is instant, room invite is optional."""
    title = format_call_title(caller_name, caller_number)
    link_name = invite_link_name(caller_name, caller_number)
    group_id = _listen_group_id(settings)
    in_telegram = bool(listen_url and listen_url.startswith("https://"))

    invite_url: str | None = None
    if create_room_invite and group_id is not None:
        try:
            invite = await bot.create_chat_invite_link(
                chat_id=group_id,
                name=link_name,
                member_limit=1,
                expire_date=int(time.time()) + INVITE_TTL_SECONDS,
            )
            invite_url = invite.invite_link
        except TelegramError as exc:
            logger.warning("Could not create listen invite link: %s", exc)

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    listen_btn = _listen_button(f"🔴 Listen in Telegram — {link_name}", listen_url)
    if listen_btn is not None:
        keyboard_rows.append([listen_btn])
    if listen_url and listen_url.startswith("https://"):
        keyboard_rows.append(
            [InlineKeyboardButton("🌐 Open in browser", url=listen_url)]
        )
    phone_ext = phone_listen_ext
    if show_phone_button and phone_ext and listen_extension and listen_participant_id is not None:
        keyboard_rows.insert(
            0,
            [
                InlineKeyboardButton(
                    f"📞 Listen on phone — ext {phone_ext}",
                    callback_data=f"lp:{listen_extension}:{listen_participant_id}",
                )
            ],
        )
    if invite_url:
        keyboard_rows.append(
            [InlineKeyboardButton(f"📞 Join room — {link_name}", url=invite_url)]
        )
    markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else InlineKeyboardMarkup([])

    if in_telegram:
        listen_line = (
            "1. Tap <b>📞 Listen on phone</b> — answer ext "
            f"<b>{html.escape(phone_listen_ext or '999')}</b> on your 3CX app\n"
            "2. Or tap <b>Listen in Telegram</b> (audio may not work on agent extensions)\n\n"
        ) if show_phone_button and phone_listen_ext else (
            "1. Tap <b>Listen in Telegram</b>\n"
            "2. Tap <b>▶ Tap to listen live</b> inside the app\n\n"
        )
    elif listen_url:
        listen_line = "Tap <b>Listen in Telegram</b> to open the live audio player.\n\n"
    elif telegram_listen_ready(settings):
        listen_line = ""
    else:
        listen_line = (
            "⚠️ Set <b>LISTEN_PUBLIC_URL</b> to an <b>HTTPS</b> URL (e.g. ngrok) "
            "so you can listen inside Telegram.\n\n"
        )

    phone_line = (
        f"📞 Or answer extension <b>{html.escape(phone_listen_ext)}</b> on your phone.\n\n"
        if phone_listen_ext
        else ""
    )
    message = (
        f"🔴 <b>Live listen</b> 🎧\n"
        f"📞 <b>{html.escape(title)}</b>\n"
        f"👤 Agent: {html.escape(agent_label)}\n\n"
        f"{listen_line}"
        f"{phone_line}"
        f"<i>📌 Keep this open while the call is active.</i>"
    )
    return message, markup, invite_url


async def _post_listen_session_to_group(
    bot: Bot,
    group_id: int,
    *,
    title: str,
    agent_label: str,
    listen_url: str | None,
) -> None:
    lines = [
        f"🔴 <b>{html.escape(title)}</b> — live listen session",
        f"Agent: {html.escape(agent_label)}",
    ]
    try:
        rows: list[list[InlineKeyboardButton]] = []
        listen_btn = _listen_button("🔴 Listen in Telegram", listen_url)
        if listen_btn is not None:
            rows.append([listen_btn])
        await bot.send_message(
            group_id,
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        logger.warning("Could not post listen session to group %s: %s", group_id, exc)
