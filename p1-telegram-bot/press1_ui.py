"""Shared Telegram HTML UI — clean operator cards.

HTML parse mode only. Cards use Telegram <blockquote> (accent bar).
Always esc() dynamic / user content.
"""

from __future__ import annotations

import html

SEP = "·"


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def b(text: object) -> str:
    return f"<b>{esc(text)}</b>"


def i(text: object) -> str:
    return f"<i>{esc(text)}</i>"


def code(text: object) -> str:
    return f"<code>{esc(text)}</code>"


def strike(text: object) -> str:
    return f"<s>{esc(text)}</s>"


def rule() -> str:
    return "<i>┄┄┄┄┄┄┄┄┄┄┄┄┄┄</i>"


def muted(text: object) -> str:
    return f"<i>{esc(text)}</i>"


def card(title: str, body_lines: list[str], *, expandable: bool = False) -> str:
    """Blockquote card. Title escaped; body_lines may contain trusted HTML."""
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    inner = "\n".join([esc(title), *body_lines])
    return f"{tag}{inner}</blockquote>"


def kv(label: str, value: object = "", *, icon: str = "") -> str:
    """Key · value row. Prefer over emoji-stuffed labels."""
    head = f"{icon} " if icon else ""
    if value == "":
        return f"{head}{esc(label)}"
    return f"{head}{esc(label)}  <b>{esc(value)}</b>"


def stat(label: str, value: object = "", *, icon: str, suffix: str = "") -> str:
    """Legacy-compatible row used across campaign cards."""
    line = f"{icon} {esc(label)}"
    if value != "" or suffix:
        line += f"  <b>{esc(value)}</b>{esc(suffix)}"
    return line


def bullet(label: str, value: object = "", *, icon: str = "·", suffix: str = "") -> str:
    line = f"{icon} <b>{esc(label)}</b>"
    if value != "" or suffix:
        line += f"  {esc(value)}{esc(suffix)}"
    return line


def note(icon: str, text: str) -> str:
    """Free line — text is escaped (no raw HTML in text)."""
    return f"{icon} {esc(text)}"


def note_html(icon: str, html_body: str) -> str:
    """Free line with trusted HTML fragments already escaped by caller."""
    return f"{icon} {html_body}"


def error(message: object) -> str:
    return f"⚠ {esc(message)}"


def deny() -> str:
    return card(
        "THE FLOOR",
        [
            muted("No seat on this floor."),
            "",
            "Ask an owner for access, then try /start again.",
        ],
    )
