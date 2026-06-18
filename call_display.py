"""Format caller and extension user details for Telegram messages."""

from __future__ import annotations

import html
from typing import Any

from database import ExtensionLink


def caller_from_participant(participant: dict[str, Any] | None) -> tuple[str, str]:
    if not participant:
        return "", ""
    name = str(participant.get("party_caller_name") or "").strip()
    number = str(participant.get("party_caller_id") or "").strip()
    if name and number and name == number:
        name = ""
    return name, number


def format_caller_html(caller_name: str, caller_number: str) -> str:
    name = caller_name.strip()
    number = caller_number.strip()
    if name and number:
        return f"{html.escape(name)} ({html.escape(number)})"
    if number:
        return html.escape(number)
    if name:
        return html.escape(name)
    return ""


def format_with_caller(
    prefix: str,
    *,
    caller_name: str = "",
    caller_number: str = "",
    suffix: str = "",
) -> str:
    caller = format_caller_html(caller_name, caller_number)
    if caller:
        return f"{prefix} with {caller}{suffix}"
    return f"{prefix}{suffix}"


def format_extension_user_label(link: ExtensionLink, *, html_mode: bool = True) -> str:
    """@username (Display Name) when possible; HTML tg:// link if only display name."""
    username = (link.telegram_username or "").strip()
    display = (link.display_name or "").strip()
    if username and display:
        name_part = html.escape(display) if html_mode else display
        return f"@{username} ({name_part})"
    if username:
        return f"@{username}"
    if display:
        if html_mode:
            return (
                f'<a href="tg://user?id={link.telegram_user_id}">'
                f"{html.escape(display)}</a>"
            )
        return display
    if html_mode:
        return f'<a href="tg://user?id={link.telegram_user_id}">User</a>'
    return f"User {link.telegram_user_id}"


def format_extension_user_plain(link: ExtensionLink) -> str:
    return format_extension_user_label(link, html_mode=False)


def format_customer_name_only(caller_name: str, caller_number: str) -> str:
    """Customer display name only — never show phone numbers in group chat."""
    name = caller_name.strip()
    number = caller_number.strip()
    if name and number and name == number:
        return ""
    if name:
        return html.escape(name)
    return ""


def format_bold_agent_label(link: ExtensionLink) -> str:
    return f"<b>{format_extension_user_label(link, html_mode=True)}</b>"


def format_customer_line(caller_name: str, caller_number: str) -> str:
    customer = format_customer_name_only(caller_name, caller_number)
    if customer:
        return f"👤 <b>Customer</b> · <b>{customer}</b>"
    return ""


def format_ended_by_line(ended_by: str | None) -> str:
    if not ended_by:
        return ""
    if ended_by == "caller":
        label = "Caller"
    elif ended_by == "user":
        label = "Agent"
    else:
        label = ended_by.strip().title() or ended_by
    return f"👤 <b>Ended by</b> · <b>{html.escape(label)}</b>"
