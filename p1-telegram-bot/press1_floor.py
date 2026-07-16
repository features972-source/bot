#!/usr/bin/env python3
"""THE FLOOR — unique P1 operator identity, callsigns, heat, and pulse intel.

This is the visual/ops language that makes the Telegram bot feel like a
live trading floor for press-1 campaigns — not a generic dialer menu.
"""

from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime, timezone

import press1_ui as ui

# Phonetic callsign banks — short, radio-ready, memorable.
_PREFIXES = (
    "TIDE", "NIGHT", "SPARK", "DRIFT", "SIGNAL", "CURRENT", "FLASH",
    "EMBER", "NORTH", "VELOCITY", "ECHO", "MERIDIAN", "HALO", "RIPTIDE",
    "COPPER", "IVORY", "AETHER", "PULSE", "VORTEX", "LANTERN",
)
_SUFFIX_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def callsign_for_run(run_id: str = "", *, chat_id: int = 0) -> str:
    """Stable-ish memorable callsign for a campaign run."""
    seed = f"{run_id}|{chat_id}|{int(time.time()) // 60}"
    digest = hashlib.sha256(seed.encode()).hexdigest()
    pref = _PREFIXES[int(digest[:2], 16) % len(_PREFIXES)]
    tail = "".join(_SUFFIX_CHARS[int(digest[i : i + 2], 16) % len(_SUFFIX_CHARS)] for i in (2, 4, 6))
    return f"{pref}-{tail}"


def fresh_callsign() -> str:
    pref = random.choice(_PREFIXES)
    tail = "".join(random.choice(_SUFFIX_CHARS) for _ in range(3))
    return f"{pref}-{tail}"


def heat_label(*, dialed: int, answered: int, press1: int) -> tuple[str, str]:
    """Return (badge, blurb) for conversion heat."""
    if answered <= 0 and dialed < 5:
        return "⚪ COLD START", "Waiting for first answers"
    if answered <= 0:
        return "🔵 SILENT", "Answers not converting yet"
    rate = press1 / answered
    if rate >= 0.18:
        return "🔥 ON FIRE", f"{rate * 100:.1f}% of answers press 1"
    if rate >= 0.10:
        return "🟠 HOT", f"{rate * 100:.1f}% press-1 rate"
    if rate >= 0.05:
        return "🟡 WARM", f"{rate * 100:.1f}% press-1 rate"
    if press1 > 0:
        return "🔵 COOL", f"{rate * 100:.1f}% — stack is catching some"
    return "⚪ ICE", "Answers landing, no press-1s yet"


def heat_bar(*, dialed: int, answered: int, press1: int, width: int = 10) -> str:
    if answered <= 0:
        return "·" * width
    rate = min(1.0, (press1 / answered) / 0.22)  # 22% = full bar
    filled = int(round(width * rate))
    return "▓" * filled + "░" * (width - filled)


def floor_clock() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def welcome_card(*, transfer: str = "", loaded: int = 0, grant_left: str = "") -> str:
    lines = [
        ui.note("🎙️", "Live press-1 operations floor"),
        "",
        "Paste a list · drop a CSV · tap the pad below.",
        "",
        ui.stat("Transfer", transfer or "not set", icon="🎯"),
        ui.stat("Loaded", f"{loaded} leads", icon="💾"),
    ]
    if grant_left:
        lines.append(ui.stat("Access", grant_left, icon="🔑"))
    lines.extend(
        [
            "",
            "<i>Every digit a caller presses streams here live.</i>",
            f"<i>Floor clock {floor_clock()}</i>",
        ]
    )
    return ui.card("🌊  THE FLOOR", lines)


def help_card() -> str:
    return (
        ui.card(
            "🌊  THE FLOOR",
            [
                ui.note("🎙️", "Operator-built press-1 war room"),
                "",
                "🚀 <b>RUN THE LIST</b>",
                ui.bullet("/go", "preflight + launch (recommended)", icon="▪️"),
                ui.bullet("/run", "launch loaded leads now", icon="▪️"),
                ui.bullet("/pulse", "live conversion intel", icon="▪️"),
                ui.bullet("/dashboard", "pinned control room", icon="▪️"),
                ui.bullet("/status", "quick snapshot", icon="▪️"),
                ui.bullet("/pause", "hold new dials", icon="▪️"),
                ui.bullet("/unpause", "resume dials", icon="▪️"),
                ui.bullet("/stop", "kill this chat's campaign", icon="▪️"),
                ui.bullet("/testcall", "prove press-1 on your handset", icon="▪️"),
                "",
                "⏰ <b>SCHEDULE</b>",
                ui.bullet("/schedule 9am", "queue a run", icon="▪️"),
                ui.bullet("/schedules", "upcoming runs", icon="▪️"),
                "",
                "🎛 <b>SETUP</b>",
                ui.bullet("/audio", "swap IVR recording", icon="▪️"),
                ui.bullet("/settings", "transfer route", icon="▪️"),
                ui.bullet("/testnumber", "your prove-out mobile", icon="▪️"),
                ui.bullet("/clear", "wipe loaded leads + server list", icon="▪️"),
                "",
                "🔐 <b>ACCESS</b> (owner)",
                ui.bullet("/addkey @user 24h", "temp seat on the floor", icon="▪️"),
                ui.bullet("/listkeys", "who has a seat", icon="▪️"),
                ui.bullet("/repair", "re-sync dial stack", icon="▪️"),
            ],
            expandable=True,
        )
        + "\n🔔 <i>/go runs a stack check first — same path that makes /testcall transfer.</i>"
        + "\n🔔 <i>Campaigns get a callsign. Press-1 hits get their own alert.</i>"
    )


def pulse_card(
    st: dict[str, str],
    *,
    callsign: str = "",
    transfer: str = "",
    loaded: int = 0,
) -> str:
    dialed = int(st.get("dialed", 0) or 0)
    answered = int(st.get("answered", 0) or 0)
    press1 = int(st.get("press1", 0) or 0)
    live = int(st.get("live", 0) or 0)
    hopper = int(st.get("hopper", 0) or 0)
    total = int(st.get("list_size", 0) or 0)
    failed = int(st.get("failed", 0) or 0)
    state = st.get("dial_state", "idle")
    badge, blurb = heat_label(dialed=dialed, answered=answered, press1=press1)
    bar = heat_bar(dialed=dialed, answered=answered, press1=press1)
    ans_rate = (answered * 100 / dialed) if dialed else 0.0
    p1_of_ans = (press1 * 100 / answered) if answered else 0.0

    title = f"📡  PULSE · {callsign}" if callsign else "📡  PULSE"
    lines = [
        ui.esc(f"{badge}  {bar}"),
        f"<i>{ui.esc(blurb)}</i>",
        "",
        ui.stat("State", state, icon="🟢" if state == "running" else "⚪"),
        ui.stat("Live legs", live, icon="📡"),
        ui.stat("Dialed", f"{dialed}/{total or '—'}", icon="📞"),
        ui.stat("Waiting", hopper, icon="⏳"),
        "",
        ui.stat("Answer rate", f"{ans_rate:.0f}%", icon="✅"),
        ui.stat("P1 / answer", f"{p1_of_ans:.1f}%", icon="🔥"),
        ui.stat("Press-1s", press1, icon="🎯"),
    ]
    if failed:
        lines.append(ui.stat("Failed", failed, icon="❌"))
    if transfer:
        lines.append("")
        lines.append(ui.stat("Transfer", transfer, icon="🎯"))
    if loaded:
        lines.append(ui.stat("In hopper (bot)", loaded, icon="💾"))
    remaining = max(0, (total or dialed + hopper) - dialed)
    forecast = forecast_line(
        dialed=dialed, answered=answered, press1=press1, remaining=remaining
    )
    if forecast:
        lines.append("")
        lines.append(ui.note("📉", forecast))
    lines.append("")
    lines.append(f"<i>Floor clock {floor_clock()}</i>")
    return ui.card(title, lines)


def hit_alert(*, callsign: str, press1: int, answered: int, lead_hint: str = "") -> str:
    rate = (press1 * 100 / answered) if answered else 0.0
    lines = [
        ui.note("🔥", f"Press-1 #{press1} locked"),
        "",
        ui.stat("Callsign", callsign or "—", icon="🏷️"),
        ui.stat("P1 / answer", f"{rate:.1f}%", icon="📈"),
    ]
    if lead_hint:
        lines.append(ui.stat("Lead", lead_hint, icon="📱"))
    lines.append("")
    lines.append("<i>Transfer path engaged.</i>")
    return ui.card("💥  FLOOR HIT", lines)


def launch_banner(*, callsign: str, count: int, cap: int, gap: float) -> str:
    lines = [
        ui.note("🚀", f"Opening {count} leads on the floor"),
        "",
        ui.stat("Callsign", callsign, icon="🏷️"),
        ui.stat("Ceiling", f"{cap} live", icon="📡"),
        ui.stat("Pace", f"{gap:g}s gap", icon="⚙️"),
        "",
        "<i>Watch for 💥 FLOOR HIT alerts when someone presses 1.</i>",
    ]
    return ui.card("🌊  FLOOR OPEN", lines)


def preflight_card(checks: list[tuple[str, bool, str]]) -> str:
    lines = []
    for label, ok, detail in checks:
        mark = "✅" if ok else "❌"
        lines.append(f"{mark} <b>{ui.esc(label)}</b> — {ui.esc(detail)}")
    all_ok = all(ok for _, ok, _ in checks)
    footer = "All green — launching." if all_ok else "Fix the red lines, then /go again."
    lines.append("")
    lines.append(f"<i>{ui.esc(footer)}</i>")
    return ui.card("🛫  PREFLIGHT", lines)


def finished_banner(*, callsign: str, dialed: int, answered: int, press1: int) -> str:
    badge, blurb = heat_label(dialed=dialed, answered=answered, press1=press1)
    return ui.card(
        "🏁  FLOOR CLOSED",
        [
            ui.stat("Callsign", callsign or "—", icon="🏷️"),
            ui.stat("Dialed", dialed, icon="📞"),
            ui.stat("Answered", answered, icon="✅"),
            ui.stat("Press-1", press1, icon="🔥"),
            "",
            ui.esc(f"{badge} — {blurb}"),
        ],
    )


def eta_minutes(*, count: int, cap: int = 40, gap: float = 0.2) -> int:
    """Rough wall-clock estimate if the hopper stays full at the live ceiling."""
    if count <= 0:
        return 0
    _ = gap
    # Empirical: ~cap/12 leads per second under safe BitCall pacing.
    cps = max(0.5, min(float(cap) / 12.0, 8.0))
    return max(1, int(round(count / cps / 60.0)))


def leads_brief(*, count: int, replaced: int = 0, cap: int = 40, gap: float = 0.2) -> str:
    eta = eta_minutes(count=count, cap=cap, gap=gap)
    note = f" (replaced {replaced})" if replaced > 0 else ""
    return ui.card(
        "📥  HOPPER LOADED",
        [
            ui.stat("Leads", f"{count}{note}", icon="💾"),
            ui.stat("Est. floor time", f"~{eta} min @ {cap} live", icon="⏱️"),
            "",
            "<i>Tap 🛫 GO for preflight + launch — or /go</i>",
        ],
    )


def forecast_line(*, dialed: int, answered: int, press1: int, remaining: int) -> str:
    """Project press-1s if the rest of the list converts like so far."""
    if answered < 8 or remaining <= 0:
        return ""
    rate = press1 / answered
    # Assume remaining answers at same answer-rate as dialed so far is noisy —
    # use press1-per-dial when dialed is healthier.
    if dialed >= 20:
        rate = press1 / dialed
        projected = int(round(press1 + rate * remaining))
        return f"P1 forecast ~{projected} if pace holds"
    projected = int(round(press1 + rate * remaining * 0.35))
    return f"P1 forecast ~{projected} (early read)"
