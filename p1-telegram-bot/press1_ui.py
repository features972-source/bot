"""Shared Telegram HTML UI helpers — professional card styling.

Everything the bot sends uses HTML parse mode. Cards are rendered with
Telegram <blockquote> blocks (the orange side-bar + quote-mark look).
Always wrap dynamic/user content in esc() to keep the HTML valid.
"""

from __future__ import annotations

import html

# Visual language ---------------------------------------------------------
BULLET = "⬢"
FIRE = "🔥"
RULE = "━━━━━━━━━━━━━━━━"


def esc(value: object) -> str:
    """Escape dynamic content so it can't break the HTML markup."""
    return html.escape(str(value), quote=False)


def b(text: object) -> str:
    return f"<b>{esc(text)}</b>"


def code(text: object) -> str:
    return f"<code>{esc(text)}</code>"


def strike(text: object) -> str:
    return f"<s>{esc(text)}</s>"


def card(title: str, body_lines: list[str], *, expandable: bool = False) -> str:
    """Build a blockquote card. `title` is escaped; body_lines are raw HTML."""
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    inner = "\n".join([f"<b>{esc(title)}</b>", *body_lines])
    return f"{tag}{inner}</blockquote>"


def bullet(label: str, value: object = "", *, icon: str = BULLET, suffix: str = "") -> str:
    """A card row: hexagon + bold label + value, e.g. `⬢ Dialed  340`."""
    line = f"{icon} <b>{esc(label)}</b>"
    if value != "" or suffix:
        line += f"  {esc(value)}{suffix}"
    return line


def note(icon: str, text: str) -> str:
    """A free line inside a card (text is escaped)."""
    return f"{icon} {esc(text)}"


def error(message: object) -> str:
    return f"⚠️ {esc(message)}"
