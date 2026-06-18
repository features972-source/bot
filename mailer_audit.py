"""Audit logging for /mail sessions — DB + logs/mailer-audit.log."""

from __future__ import annotations

import logging
import re
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

from database import MailerLogEntry, insert_mailer_log, list_mailer_logs

_ROOT = Path(__file__).resolve().parent
_LOG_DIR = _ROOT / "logs"
_LOG_FILE = _LOG_DIR / "mailer-audit.log"

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)
_RECIPIENT_LINE_RE = re.compile(
    r"(?im)(?:to|recipient|sent\s+to|email\s+to|delivered\s+to)\s*[:\-]?\s*"
    r"([^\n,;]+)",
)
_SUBJECT_LINE_RE = re.compile(r"(?im)(?:subject)\s*[:\-]?\s*([^\n]+)")

_audit_logger: logging.Logger | None = None


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def extract_emails(text: str) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    found: list[str] = []
    for match in _EMAIL_RE.finditer(text):
        email = match.group(0).lower()
        if email not in seen:
            seen.add(email)
            found.append(email)
    return found


def extract_recipient_hint(text: str) -> str | None:
    if not text:
        return None
    emails = extract_emails(text)
    if emails:
        return ", ".join(emails)
    match = _RECIPIENT_LINE_RE.search(text)
    if match:
        hint = match.group(1).strip()
        if hint:
            return hint[:500]
    return None


def extract_subject_hint(text: str) -> str | None:
    if not text:
        return None
    match = _SUBJECT_LINE_RE.search(text)
    if not match:
        return None
    subject = match.group(1).strip()
    return subject[:500] if subject else None


def _ensure_audit_logger() -> logging.Logger:
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mailer.audit")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
    _audit_logger = logger
    return logger


def log_mailer_event(
    database_path: str,
    *,
    session_id: str,
    event_type: str,
    telegram_user_id: int,
    telegram_username: str | None = None,
    display_name: str | None = None,
    detail: str = "",
    recipient: str | None = None,
    destination: str | None = None,
    content: str | None = None,
) -> None:
    user_label = _format_user_label(
        telegram_user_id, telegram_username, display_name
    )
    recipient_part = f" → {recipient}" if recipient else ""
    destination_part = f" @ {destination}" if destination else ""
    content_preview = ""
    if content:
        preview = content.replace("\n", " ").strip()
        if len(preview) > 200:
            preview = preview[:200] + "…"
        content_preview = f' | "{preview}"'

    line = (
        f"{event_type} | session={session_id} | user={user_label}"
        f"{recipient_part}{destination_part}{content_preview}"
    )
    if detail:
        line = f"{line} | {detail}"

    _ensure_audit_logger().info(line)
    insert_mailer_log(
        database_path,
        session_id=session_id,
        event_type=event_type,
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        display_name=display_name,
        detail=detail,
        recipient=recipient,
        destination=destination,
        content=content,
    )


def _format_user_label(
    user_id: int,
    username: str | None,
    display_name: str | None,
) -> str:
    if username:
        return f"@{username} ({user_id})"
    if display_name:
        return f"{display_name} ({user_id})"
    return str(user_id)


def _escape_md(text: str) -> str:
    for char in ("\\", "_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"):
        text = text.replace(char, f"\\{char}")
    return text


def format_mailer_log_row(entry: MailerLogEntry) -> str:
    user = entry.telegram_username
    if user:
        user = f"@{entry.telegram_username}"
    elif entry.display_name:
        user = _escape_md(entry.display_name)
    else:
        user = str(entry.telegram_user_id)

    parts = [
        f"`{entry.created_at[:19]}`",
        f"**{entry.event_type}**",
        user,
        f"session `{entry.session_id}`",
    ]
    if entry.recipient:
        parts.append(f"→ `{_escape_md(entry.recipient)}`")
    if entry.destination:
        parts.append(f"@ `{_escape_md(entry.destination)}`")
    if entry.detail:
        parts.append(_escape_md(entry.detail))
    if entry.content:
        preview = entry.content.replace("\n", " ").strip()
        if len(preview) > 120:
            preview = preview[:120] + "…"
        parts.append(f'"{_escape_md(preview)}"')
    return " · ".join(parts)


def recent_mailer_log_rows(database_path: str, *, limit: int = 25) -> list[str]:
    entries = list_mailer_logs(database_path, limit=limit)
    return [format_mailer_log_row(entry) for entry in entries]
