#!/usr/bin/env python3
"""Floor dynamics + Telegram sound/visual effects.

Telegram can't play custom audio on message *edits*, but hit alerts can:
  • phone notification sound (disable_notification=False)
  • free message effects (🎉 / 🔥) in private chats
"""

from __future__ import annotations

# Free Bot API message effects (private chats). Visual + soft client chime.
EFFECT_PARTY = "5046509860389126442"  # 🎉
EFFECT_FIRE = "5104841245755180586"  # 🔥
EFFECT_HEART = "5159385139981059251"  # ❤️
EFFECT_THUMBS = "5107584321108051014"  # 👍

_PULSE = ("●", "◉", "◎", "○", "◎", "◉")
_SPARK = "▁▂▃▄▅▆▇█"


def live_pulse(frame: int) -> str:
    return _PULSE[frame % len(_PULSE)]


def progress_shimmer(pct: int, frame: int, width: int = 10) -> str:
    """Solid bar with a soft leading tick that walks while dialing."""
    pct = max(0, min(100, pct))
    filled = int(round(width * pct / 100))
    if pct > 0 and filled == 0:
        filled = 1
    cells = ["█"] * filled + ["░"] * (width - filled)
    if 0 < pct < 100 and filled < width:
        cells[filled] = "▒" if frame % 2 == 0 else "░"
    elif pct >= 100:
        cells[-1] = "▓" if frame % 2 == 0 else "█"
    return "".join(cells)


def incline_spark(values: list[float], width: int = 10) -> str:
    """Rising conversion incline — empty until we have a few samples."""
    if len(values) < 2:
        return ""
    recent = values[-width:]
    lo = min(recent)
    hi = max(recent)
    span = hi - lo
    if span <= 0:
        return ("▄" * len(recent)).ljust(width, "·")[:width]
    out: list[str] = []
    for v in recent:
        idx = int(round((v - lo) / span * (len(_SPARK) - 1)))
        out.append(_SPARK[max(0, min(len(_SPARK) - 1, idx))])
    return "".join(out).ljust(width, "·")[:width]


def record_incline(progress: dict, *, answered: int, press1: int) -> list[float]:
    """Append current press-1-of-answer rate for the live incline spark."""
    samples: list[float] = progress.setdefault("p1_incline", [])
    rate = (press1 * 100.0 / answered) if answered > 0 else 0.0
    samples.append(rate)
    samples[:] = samples[-14:]
    return samples


def effect_for_hit(*, press1: int, answered: int) -> str:
    """Pick a louder celebration as conversion climbs."""
    rate = (press1 / answered) if answered else 0.0
    if rate >= 0.15 or press1 >= 10:
        return EFFECT_FIRE
    if press1 >= 3:
        return EFFECT_PARTY
    return EFFECT_THUMBS
