"""Telethon userbot bridge: forward the mailer bot into Q1 Call Manager DMs."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from telethon import TelegramClient, events
from telethon.tl.types import (
    KeyboardButtonCallback,
    KeyboardButtonUrl,
    ReplyInlineMarkup,
)

from config import Settings
from mailer_audit import (
    extract_emails,
    extract_recipient_hint,
    extract_subject_hint,
    log_mailer_event,
    new_session_id,
)

logger = logging.getLogger(__name__)

MAILER_BRIDGE_KEY = "mailer_bridge"
CALLBACK_PREFIX = "mailer:"
IDLE_TIMEOUT_SECONDS = 300
_EMAIL_SENT_RE = re.compile(
    r"(?i)(?:email\s+sent\s+successfully|email\s+has\s+been\s+sent|successfully\s+sent)"
)
_HIDDEN_MAILER_BUTTONS = frozenset(
    {"deposit", "check balance", "view transactions", "view transaction"}
)


@dataclass
class MailerSession:
    user_id: int
    chat_id: int
    user_display: str = ""
    telegram_username: str | None = None
    session_id: str = ""
    started_at: float = field(default_factory=time.monotonic)
    last_activity_at: float = field(default_factory=time.monotonic)
    last_mailer_message_id: int | None = None
    button_refs: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    button_labels: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    button_order: list[str] = field(default_factory=list)
    next_button_ref: int = 0
    known_recipients: list[str] = field(default_factory=list)
    last_subject: str | None = None


@dataclass
class MailerQueueEntry:
    user_id: int
    chat_id: int
    user_display: str = ""
    start_args: str = ""
    joined_at: float = field(default_factory=time.monotonic)


class MailerBridge:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: TelegramClient | None = None
        self._bot = None
        self._session: MailerSession | None = None
        self._queue: list[MailerQueueEntry] = []
        self._mailer_entity = None
        self._handler_registered = False
        self._idle_task: asyncio.Task | None = None

    @property
    def configured(self) -> bool:
        return self._settings.mailer_bridge_enabled

    @property
    def active_user_id(self) -> int | None:
        return self._session.user_id if self._session else None

    @property
    def last_mailer_message_id(self) -> int | None:
        return self._session.last_mailer_message_id if self._session else None

    def set_bot(self, bot) -> None:
        self._bot = bot

    async def connect(self) -> None:
        if not self.configured:
            return
        session_path = self._settings.telethon_session_path
        self._client = TelegramClient(
            session_path,
            self._settings.telethon_api_id,
            self._settings.telethon_api_hash,
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            logger.warning(
                "Telethon userbot not logged in. Run: "
                "python scripts/telethon_login.py"
            )
            await self._client.disconnect()
            self._client = None
            return

        username = self._settings.mailer_bot_username.lstrip("@")
        self._mailer_entity = await self._client.get_entity(username)
        if not self._handler_registered:
            self._client.add_event_handler(
                lambda e: self._on_mailer_message(e, edited=False),
                events.NewMessage(from_users=self._mailer_entity),
            )
            self._client.add_event_handler(
                lambda e: self._on_mailer_message(e, edited=True),
                events.MessageEdited(from_users=self._mailer_entity),
            )
            self._handler_registered = True
        me = await self._client.get_me()
        logger.info("Mailer bridge connected as %s", getattr(me, "username", me.id))
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(
                self._idle_watch_loop(),
                name="mailer-idle-watch",
            )

    async def _ensure_connected(self) -> bool:
        """Reconnect Telethon if Telegram dropped while the bot kept running."""
        if not self.configured:
            return False

        if self._client is None:
            try:
                await self.connect()
            except Exception:
                logger.exception("Failed to connect mailer bridge")
                return False
            return self._client is not None

        if self._client.is_connected():
            return True

        try:
            await self._client.connect()
            if not await self._client.is_user_authorized():
                logger.warning(
                    "Telethon userbot not logged in. Run: "
                    "python scripts/telethon_login.py"
                )
                await self._client.disconnect()
                self._client = None
                return False
            if self._mailer_entity is None:
                username = self._settings.mailer_bot_username.lstrip("@")
                self._mailer_entity = await self._client.get_entity(username)
            logger.info("Mailer bridge reconnected")
            return True
        except Exception:
            logger.exception("Failed to reconnect mailer bridge")
            return False

    async def disconnect(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            self._idle_task = None
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    def _touch_session(self) -> None:
        if self._session is not None:
            self._session.last_activity_at = time.monotonic()

    def _remember_recipients(self, text: str) -> list[str]:
        if self._session is None:
            return []
        found: list[str] = []
        for email in extract_emails(text):
            if email not in self._session.known_recipients:
                self._session.known_recipients.append(email)
                found.append(email)
        hint = extract_recipient_hint(text)
        if hint and hint not in self._session.known_recipients:
            self._session.known_recipients.append(hint)
            found.append(hint)
        subject = extract_subject_hint(text)
        if subject:
            self._session.last_subject = subject
        return found

    def _audit(
        self,
        event_type: str,
        *,
        detail: str = "",
        recipient: str | None = None,
        destination: str | None = None,
        content: str | None = None,
        user_id: int | None = None,
        telegram_username: str | None = None,
        display_name: str | None = None,
        session_id: str | None = None,
    ) -> None:
        session = self._session
        resolved_user_id = user_id
        resolved_session_id = session_id
        resolved_username = telegram_username
        resolved_display = display_name
        if session is not None:
            if resolved_user_id is None:
                resolved_user_id = session.user_id
            if not resolved_session_id:
                resolved_session_id = session.session_id
            if resolved_username is None:
                resolved_username = session.telegram_username
            if resolved_display is None:
                resolved_display = session.user_display or None
        if resolved_user_id is None or not resolved_session_id:
            return
        log_mailer_event(
            self._settings.database_path,
            session_id=resolved_session_id,
            event_type=event_type,
            telegram_user_id=resolved_user_id,
            telegram_username=resolved_username,
            display_name=resolved_display,
            detail=detail,
            recipient=recipient,
            destination=destination or self._settings.mailer_display_name,
            content=content,
        )

    def _session_idle_expired(self) -> bool:
        if self._session is None:
            return False
        return (
            time.monotonic() - self._session.last_activity_at
            > IDLE_TIMEOUT_SECONDS
        )

    async def _expire_session_if_idle(self) -> None:
        if self._session_idle_expired():
            await self._release_session(
                reason="idle_timeout",
                notify_message=(
                    "Mailer session ended after 5 minutes of inactivity."
                ),
            )

    def _queue_position(self, user_id: int) -> int | None:
        for index, entry in enumerate(self._queue):
            if entry.user_id == user_id:
                return index + 1
        return None

    def _remove_from_queue(self, user_id: int) -> bool:
        before = len(self._queue)
        self._queue = [entry for entry in self._queue if entry.user_id != user_id]
        return len(self._queue) < before

    def _enqueue(
        self,
        *,
        user_id: int,
        chat_id: int,
        user_display: str,
        start_args: str,
    ) -> int:
        existing = self._queue_position(user_id)
        if existing is not None:
            return existing
        self._queue.append(
            MailerQueueEntry(
                user_id=user_id,
                chat_id=chat_id,
                user_display=user_display,
                start_args=start_args,
            )
        )
        return len(self._queue)

    def _format_queue_status(self, position: int, active_display: str) -> str:
        ahead = position - 1
        if ahead == 0:
            wait_line = "You're next — you'll be notified when the mailer is free."
        elif ahead == 1:
            wait_line = "1 person ahead of you."
        else:
            wait_line = f"{ahead} people ahead of you."
        return (
            f"Mailer is in use by {active_display}.\n\n"
            f"You're <b>#{position}</b> in the queue. {wait_line}\n"
            "Send /maildone to leave the queue."
        )

    async def _idle_watch_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                if self._session_idle_expired():
                    await self._release_session(
                        reason="idle_timeout",
                        notify_message=(
                            "Mailer session ended after 5 minutes of inactivity."
                        ),
                    )
        except asyncio.CancelledError:
            raise

    async def _reset_mailer_bot(self) -> None:
        if self._client is None or self._mailer_entity is None:
            return
        try:
            await self._client.send_message(self._mailer_entity, "/start")
            logger.info("Reset upstream mailer bot for next user")
        except Exception:
            logger.exception("Failed to reset upstream mailer bot")

    async def _release_session(
        self,
        *,
        reason: str,
        notify_message: str | None = None,
    ) -> None:
        session = self._session
        if session is None:
            return
        self._session = None
        await self._reset_mailer_bot()
        if self._bot is not None and notify_message:
            try:
                from telegram import ReplyKeyboardRemove

                await self._bot.send_message(
                    chat_id=session.chat_id,
                    text=notify_message,
                    reply_markup=ReplyKeyboardRemove(),
                )
            except Exception:
                logger.exception(
                    "Failed to notify user %s that mailer session ended",
                    session.user_id,
                )
        recipient_summary = ", ".join(session.known_recipients) or None
        detail_parts = [f"reason={reason}"]
        if session.last_subject:
            detail_parts.append(f"subject={session.last_subject}")
        self._audit(
            "session_end",
            detail="; ".join(detail_parts),
            recipient=recipient_summary,
            user_id=session.user_id,
            telegram_username=session.telegram_username,
            display_name=session.user_display or None,
            session_id=session.session_id,
        )
        logger.info(
            "Mailer session released (%s) for user %s",
            reason,
            session.user_id,
        )
        await self._activate_next_in_queue()

    async def _begin_session(
        self,
        *,
        user_id: int,
        chat_id: int,
        user_display: str,
        telegram_username: str | None = None,
        start_args: str = "",
    ) -> tuple[bool, str]:
        if not await self._ensure_connected():
            return False, "Mailer bridge is offline."

        command = "/start"
        if start_args.strip():
            command = f"/start {start_args.strip()}"

        try:
            await self._client.send_message(self._mailer_entity, command)
        except Exception as exc:
            logger.exception("Failed to send /start to mailer bot")
            return (
                False,
                f"Could not reach {self._settings.mailer_display_name}: {exc}",
            )

        self._session = MailerSession(
            user_id=user_id,
            chat_id=chat_id,
            user_display=user_display,
            telegram_username=telegram_username,
            session_id=new_session_id(),
        )
        self._touch_session()
        start_detail = "start_args=" + start_args.strip() if start_args.strip() else "immediate"
        self._audit(
            "mail_start",
            detail=start_detail,
            user_id=user_id,
            telegram_username=telegram_username,
            display_name=user_display or None,
            session_id=self._session.session_id,
        )
        return (
            True,
            f"Connected to <b>{self._settings.mailer_display_name}</b>.\n\n"
            "Replies will appear here. The session ends automatically after "
            "the email is sent or 5 minutes of inactivity.",
        )

    async def _activate_next_in_queue(self) -> None:
        if self._session is not None or not self._queue or self._bot is None:
            return

        while self._queue:
            entry = self._queue.pop(0)
            try:
                ok, detail = await self._begin_session(
                    user_id=entry.user_id,
                    chat_id=entry.chat_id,
                    user_display=entry.user_display,
                    start_args=entry.start_args,
                )
                if not ok:
                    logger.warning(
                        "Skipped queued user %s: %s",
                        entry.user_id,
                        detail,
                    )
                    continue
                await self._bot.send_message(
                    chat_id=entry.chat_id,
                    text=f"🟢 <b>It's your turn!</b>\n\n{detail}",
                    parse_mode="HTML",
                )
                logger.info("Activated queued mailer session for user %s", entry.user_id)
                return
            except Exception:
                logger.exception(
                    "Failed to activate queued mailer session for user %s",
                    entry.user_id,
                )

    async def start_for_user(
        self,
        *,
        user_id: int,
        chat_id: int,
        user_display: str = "",
        telegram_username: str | None = None,
        start_args: str = "",
    ) -> tuple[bool, str]:
        await self._expire_session_if_idle()
        if not await self._ensure_connected():
            return (
                False,
                "Mailer bridge is offline.\n\n"
                "The Telegram link to Q1 Mailer dropped. Try /mail again in "
                "a few seconds, or ask an admin to restart the bot.",
            )

        if self._session is not None and self._session.user_id != user_id:
            who = self._session.user_display or f"user {self._session.user_id}"
            position = self._enqueue(
                user_id=user_id,
                chat_id=chat_id,
                user_display=user_display,
                start_args=start_args,
            )
            queue_session_id = new_session_id()
            log_mailer_event(
                self._settings.database_path,
                session_id=queue_session_id,
                event_type="mail_queued",
                telegram_user_id=user_id,
                telegram_username=telegram_username,
                display_name=user_display or None,
                detail=f"position={position}; active_user={who}",
                destination=self._settings.mailer_display_name,
            )
            return True, self._format_queue_status(position, who)

        self._remove_from_queue(user_id)
        return await self._begin_session(
            user_id=user_id,
            chat_id=chat_id,
            user_display=user_display,
            telegram_username=telegram_username,
            start_args=start_args,
        )

    async def end_for_user(self, user_id: int) -> tuple[bool, str]:
        if self._session is not None and self._session.user_id == user_id:
            self._audit("mail_done", detail="user ended session")
            await self._release_session(
                reason="maildone",
                notify_message=None,
            )
            return True, "Mailer session ended."

        if self._remove_from_queue(user_id):
            log_mailer_event(
                self._settings.database_path,
                session_id=new_session_id(),
                event_type="mail_queue_left",
                telegram_user_id=user_id,
                detail="removed from queue via /maildone",
                destination=self._settings.mailer_display_name,
            )
            return True, "Removed from the mailer queue."

        return False, "You do not have an active mailer session or queue spot."

    def match_button_label(self, text: str) -> tuple[int, int, int] | None:
        if self._session is None:
            return None
        key = text.strip()
        if not key:
            return None
        if key in self._session.button_labels:
            return self._session.button_labels[key]
        lowered = key.lower()
        for label, pos in self._session.button_labels.items():
            if label.lower() == lowered:
                return pos
        return None

    async def forward_user_text(self, user_id: int, text: str) -> tuple[bool, str]:
        await self._expire_session_if_idle()
        if self._session is None or self._session.user_id != user_id:
            return False, "No active mailer session. Send /mail to start."
        if not await self._ensure_connected():
            return False, "Mailer bridge is offline."
        try:
            await self._client.send_message(self._mailer_entity, text)
            self._touch_session()
            recipients = self._remember_recipients(text)
            self._audit(
                "text_sent",
                recipient=", ".join(recipients) if recipients else None,
                content=text,
            )
            logger.info("Forwarded text to mailer for user %s", user_id)
            return True, ""
        except Exception as exc:
            logger.exception("Failed to forward text to mailer bot")
            return False, f"Could not send to mailer: {exc}"

    def _reset_button_map(self) -> None:
        if self._session is None:
            return
        self._session.button_labels.clear()
        self._session.button_order.clear()

    def _register_button_ref(
        self, message_id: int, row: int, col: int, label: str
    ) -> str:
        if self._session is None:
            return f"{CALLBACK_PREFIX}{message_id}:{row}:{col}"
        ref = str(self._session.next_button_ref)
        self._session.next_button_ref += 1
        self._session.button_refs[ref] = (message_id, row, col)
        clean = label.strip()
        if clean:
            self._session.button_labels[clean] = (message_id, row, col)
            if clean not in self._session.button_order:
                self._session.button_order.append(clean)
        return f"{CALLBACK_PREFIX}{ref}"

    def resolve_button_ref(self, ref: str) -> tuple[int, int, int] | None:
        if self._session is None:
            return None
        return self._session.button_refs.get(ref)

    async def click_button(
        self,
        user_id: int,
        message_id: int,
        row: int,
        col: int,
    ) -> tuple[bool, str]:
        await self._expire_session_if_idle()
        if self._session is None or self._session.user_id != user_id:
            return False, "No active mailer session. Send /mail first."
        if not await self._ensure_connected():
            return False, "Mailer bridge is offline."
        try:
            message = await self._client.get_messages(
                self._mailer_entity,
                ids=message_id,
            )
            if message is None:
                return False, "That mailer message is no longer available."
            if not message.buttons or row >= len(message.buttons):
                return False, "Button layout changed — send /mail again."
            row_buttons = message.buttons[row]
            if col >= len(row_buttons):
                return False, "Button layout changed — send /mail again."
            button_label = row_buttons[col].text
            await row_buttons[col].click()
            self._session.last_mailer_message_id = message_id
            self._touch_session()
            self._audit(
                "button_click",
                detail=f"label={button_label}",
                content=button_label,
            )
            logger.info(
                "Clicked mailer button msg=%s row=%s col=%s label=%r",
                message_id,
                row,
                col,
                button_label,
            )
            return True, ""
        except Exception as exc:
            logger.exception(
                "Failed to click mailer button msg=%s row=%s col=%s",
                message_id,
                row,
                col,
            )
            return False, f"Button click failed: {exc}"

    async def _on_mailer_message(self, event, *, edited: bool = False) -> None:
        if self._bot is None or self._session is None:
            return
        if self._session_idle_expired():
            await self._release_session(
                reason="idle_timeout",
                notify_message=(
                    "Mailer session ended after 5 minutes of inactivity."
                ),
            )
            return

        message = event.message
        self._session.last_mailer_message_id = message.id
        self._touch_session()
        chat_id = self._session.chat_id
        user_id = self._session.user_id
        try:
            text = message.message or ""
            if text:
                text = _sanitize_mailer_text(
                    text,
                    self._settings.mailer_display_name,
                    self._settings.mailer_bot_username,
                )
            if not text and message.media:
                text = f"(media from {self._settings.mailer_display_name})"
            email_sent = _is_email_sent_message(text)
            display_text = f"✏️ {text}" if edited and text else text

            if message.reply_markup and not email_sent:
                self._reset_button_map()
            keyboard = None
            if not email_sent:
                keyboard = _build_reply_keyboard(
                    message.reply_markup,
                    register_button=self._register_button_ref,
                    mailer_message_id=message.id,
                    button_order=self._session.button_order if self._session else [],
                )
            body = display_text or "(empty reply from mailer)"
            if keyboard is not None:
                body = f"{body}\n\n👇 Tap a button below"
            await self._bot.send_message(
                chat_id=chat_id,
                text=body,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            recipients = self._remember_recipients(text)
            self._audit(
                "mailer_reply",
                detail="edited" if edited else "message",
                recipient=", ".join(recipients) if recipients else None,
                content=display_text or text,
            )
            logger.info(
                "Forwarded mailer %s to user %s: %r",
                "edit" if edited else "message",
                user_id,
                (display_text or "")[:80],
            )
            if email_sent:
                self._audit(
                    "email_sent",
                    recipient=", ".join(self._session.known_recipients) or None,
                    detail=self._session.last_subject or "confirmed by mailer",
                    content=display_text or text,
                )
                await self._release_session(
                    reason="email_sent",
                    notify_message="✅ Email sent — your mailer session has ended.",
                )
        except Exception:
            logger.exception(
                "Failed to forward mailer message to user %s",
                user_id,
            )


def _is_email_sent_message(text: str) -> bool:
    return bool(_EMAIL_SENT_RE.search(text))


def _normalize_mailer_button_label(label: str) -> str:
    return re.sub(r"[^\w\s]", "", label.lower()).strip()


def _mailer_button_visible(label: str) -> bool:
    return _normalize_mailer_button_label(label) not in _HIDDEN_MAILER_BUTTONS


def _build_reply_keyboard(
    reply_markup,
    *,
    register_button,
    mailer_message_id: int,
    button_order: list[str],
) -> ReplyKeyboardMarkup | None:
    from telegram import KeyboardButton, ReplyKeyboardMarkup

    if not isinstance(reply_markup, ReplyInlineMarkup):
        return None

    for row_idx, row in enumerate(reply_markup.rows):
        for col_idx, button in enumerate(row.buttons):
            if isinstance(button, KeyboardButtonCallback):
                if not _mailer_button_visible(button.text):
                    continue
                register_button(
                    mailer_message_id,
                    row_idx,
                    col_idx,
                    button.text,
                )

    visible_labels = [label for label in button_order if _mailer_button_visible(label)]
    if not visible_labels:
        return None

    return ReplyKeyboardMarkup(
        [[KeyboardButton(label)] for label in visible_labels],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _sanitize_mailer_text(text: str, display_name: str, bot_username: str) -> str:
    """Replace upstream mailer bot branding with Q1 display name."""
    username = bot_username.lstrip("@")
    for old in (
        f"@{username}",
        username,
        "RvssianMail Bot",
        "RvssianMailBot",
        "Rvssian Mail Bot",
        "Russian MailBot",
        "Russian Mail Bot",
        "🤖 RvssianMail Bot",
        "🤖 Russian MailBot",
    ):
        text = text.replace(old, display_name)

    text = re.sub(
        r"(?i)mercedes\s+mailer\s+system",
        "Q1 Mailer System",
        text,
    )

    text = re.sub(r"(?i)(👤\s*)?User:\s*.+", "👤 User: admin", text)
    text = re.sub(r"(?i)(🎫\s*)?Package:\s*.+", "🎫 Package: Q1 Unlimited", text)
    text = re.sub(r"(?im)^[^\n]*\b(?:Expires?|Expiry):\s*[^\n]*\n?", "", text)
    text = re.sub(r"(?im)^📅[^\n]*\n?", "", text)
    text = re.sub(
        r"(?im)^[^\n]*\bFamily\s*Share:\s*[^\n]*\n?",
        "",
        text,
    )
    text = re.sub(r"(?im)^👥[^\n]*\n?", "", text)
    text = re.sub(r"(?i)(💰\s*)?Balance:\s*.+", "", text)
    text = re.sub(r"(?im)^💰[^\n]*\n?", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def get_mailer_bridge(bot_data: dict) -> MailerBridge | None:
    return bot_data.get(MAILER_BRIDGE_KEY)


async def init_mailer_bridge(settings: Settings, bot_data: dict, bot) -> MailerBridge | None:
    bridge = MailerBridge(settings)
    bot_data[MAILER_BRIDGE_KEY] = bridge
    if not bridge.configured:
        return None
    bridge.set_bot(bot)
    await bridge.connect()
    return bridge
