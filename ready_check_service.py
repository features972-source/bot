"""Shift-start ready check: headset, softphone, credo, mail."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden

from config import Settings
from database import ExtensionLink, mark_ready_check_sent, ready_check_sent_today
from handlers.credo import get_credo_credit_cards, is_credo_allowed
from mailer_bridge import get_mailer_bridge
from threex_token import get_token_holder

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "ready:"


@dataclass
class ReadyStatus:
    extension: str | None
    headset_confirmed: bool
    softphone_ok: bool
    softphone_detail: str
    credo_required: bool
    credo_ok: bool
    credo_detail: str
    mail_required: bool
    mail_ok: bool
    mail_detail: str

    @property
    def all_ready(self) -> bool:
        return (
            self.headset_confirmed
            and self.softphone_ok
            and (not self.credo_required or self.credo_ok)
            and (not self.mail_required or self.mail_ok)
        )


async def extension_softphone_status(
    settings: Settings,
    bot_data: dict,
    extension: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[bool, str]:
    if not settings.threex_enabled:
        return False, "3CX not configured — confirm manually"

    tokens = get_token_holder(bot_data, settings)
    token = await tokens.get()
    if not token:
        return False, "Could not reach 3CX"

    url = f"https://{settings.threex_fqdn}/callcontrol/{extension}/devices"
    close_client = False
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=10)
        close_client = True
    try:
        response = await http_client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            await tokens.refresh()
            token = await tokens.get()
            if not token:
                return False, "3CX auth failed"
            response = await http_client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            return False, f"3CX check failed ({response.status_code})"
        data = response.json()
        if not isinstance(data, list) or not data:
            return False, "No softphone registered"
        agent = str(data[0].get("user_agent") or "").strip()
        if agent:
            return True, f"Registered ({agent[:40]})"
        return True, f"Registered ({len(data)} device(s))"
    except Exception:
        logger.exception("Softphone status check failed for ext %s", extension)
        return False, "Could not verify registration"
    finally:
        if close_client:
            await http_client.aclose()


async def gather_ready_status(
    settings: Settings,
    bot_data: dict,
    link: ExtensionLink,
    *,
    headset_confirmed: bool = False,
    softphone_override: bool = False,
) -> ReadyStatus:
    extension = link.extension
    softphone_ok, softphone_detail = await extension_softphone_status(
        settings, bot_data, extension
    )
    if softphone_override:
        softphone_ok = True
        if "confirm" not in softphone_detail.lower():
            softphone_detail = "Confirmed manually"

    credo_required = is_credo_allowed(
        settings, settings.database_path, link.telegram_user_id
    )
    credo_ok = True
    credo_detail = "Not required"
    if credo_required:
        cards = get_credo_credit_cards(settings)
        credo_ok = bool(cards)
        credo_detail = (
            f"{len(cards)} card(s) ready" if credo_ok else "No cards configured"
        )

    mail_required = settings.mailer_bridge_enabled
    mail_ok = True
    mail_detail = "Not configured"
    if mail_required:
        bridge = get_mailer_bridge(bot_data)
        mail_ok = bool(
            bridge and bridge.configured and bridge._client is not None
        )
        mail_detail = (
            f"{settings.mailer_display_name} online"
            if mail_ok
            else f"{settings.mailer_display_name} offline"
        )

    return ReadyStatus(
        extension=extension,
        headset_confirmed=headset_confirmed,
        softphone_ok=softphone_ok,
        softphone_detail=softphone_detail,
        credo_required=credo_required,
        credo_ok=credo_ok,
        credo_detail=credo_detail,
        mail_required=mail_required,
        mail_ok=mail_ok,
        mail_detail=mail_detail,
    )


def _status_icon(ok: bool, required: bool = True) -> str:
    if not required:
        return "—"
    return "✅" if ok else "❌"


def format_ready_message(status: ReadyStatus, *, intro: str) -> str:
    lines = [
        intro,
        "",
        "Before you take calls:",
        "",
        f"🎧 <b>Headset</b> — {_status_icon(status.headset_confirmed)} "
        + ("On" if status.headset_confirmed else "Tap below to confirm"),
        f"📱 <b>Softphone</b> (ext {status.extension}) — "
        f"{_status_icon(status.softphone_ok)} {status.softphone_detail}",
    ]
    if status.credo_required:
        lines.append(
            f"💳 <b>Credo</b> — {_status_icon(status.credo_ok)} {status.credo_detail}"
        )
    else:
        lines.append(f"💳 <b>Credo</b> — — {status.credo_detail}")
    if status.mail_required:
        lines.append(
            f"📧 <b>Mail</b> — {_status_icon(status.mail_ok)} {status.mail_detail}"
        )
    else:
        lines.append(f"📧 <b>Mail</b> — — {status.mail_detail}")

    if status.all_ready:
        lines.extend(["", "✅ <b>You're ready for your shift.</b> Good luck."])
    else:
        lines.extend(["", "Fix anything marked ❌, then tap <b>Re-check</b>."])
    return "\n".join(lines)


def ready_check_keyboard(status: ReadyStatus) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if not status.headset_confirmed:
        rows.append(
            [InlineKeyboardButton("🎧 Headset on", callback_data=f"{CALLBACK_PREFIX}headset")]
        )
    if not status.softphone_ok:
        rows.append(
            [
                InlineKeyboardButton(
                    "📱 Softphone registered",
                    callback_data=f"{CALLBACK_PREFIX}softphone",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("🔁 Re-check", callback_data=f"{CALLBACK_PREFIX}recheck")]
    )
    return InlineKeyboardMarkup(rows)


def _session_key(user_id: int) -> str:
    return f"ready_check:{user_id}"


def load_session(bot_data: dict, user_id: int) -> dict:
    sessions = bot_data.setdefault("ready_check_sessions", {})
    return sessions.setdefault(
        user_id,
        {"headset": False, "softphone_manual": False},
    )


async def send_ready_check(
    bot: Bot,
    settings: Settings,
    bot_data: dict,
    link: ExtensionLink,
    *,
    intro: str,
    mark_daily: bool = False,
) -> bool:
    session = load_session(bot_data, link.telegram_user_id)
    status = await gather_ready_status(
        settings,
        bot_data,
        link,
        headset_confirmed=session.get("headset", False),
        softphone_override=session.get("softphone_manual", False),
    )
    try:
        message = await bot.send_message(
            chat_id=link.telegram_user_id,
            text=format_ready_message(status, intro=intro),
            parse_mode="HTML",
            reply_markup=None if status.all_ready else ready_check_keyboard(status),
            disable_notification=mark_daily,
        )
        session["chat_id"] = message.chat_id
        session["message_id"] = message.message_id
        if mark_daily:
            mark_ready_check_sent(settings.database_path, link.telegram_user_id)
        return True
    except Forbidden:
        logger.info(
            "Ready check blocked — user %s has not started the bot",
            link.telegram_user_id,
        )
        return False
    except Exception:
        logger.exception(
            "Failed to send ready check to user %s",
            link.telegram_user_id,
        )
        return False


async def refresh_ready_check_message(
    bot: Bot,
    settings: Settings,
    bot_data: dict,
    user_id: int,
    *,
    intro: str,
) -> None:
    from database import get_link_by_telegram_user_id

    link = get_link_by_telegram_user_id(settings.database_path, user_id)
    if link is None:
        return
    session = load_session(bot_data, user_id)
    status = await gather_ready_status(
        settings,
        bot_data,
        link,
        headset_confirmed=session.get("headset", False),
        softphone_override=session.get("softphone_manual", False),
    )
    chat_id = session.get("chat_id")
    message_id = session.get("message_id")
    if not chat_id or not message_id:
        await send_ready_check(
            bot,
            settings,
            bot_data,
            link,
            intro=intro,
        )
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=format_ready_message(status, intro=intro),
            parse_mode="HTML",
            reply_markup=None if status.all_ready else ready_check_keyboard(status),
        )
    except Exception:
        logger.exception("Failed to refresh ready check for user %s", user_id)
