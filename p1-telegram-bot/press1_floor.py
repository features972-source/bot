#!/usr/bin/env python3
"""THE FLOOR — operator identity, callsigns, heat, and pulse intel.

Visual language: calm signal-room cards, sparse icons, clear hierarchy.
Easy to scan on a phone during a live campaign.
"""

from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime, timezone

import press1_ui as ui

_PREFIXES = (
    "TIDE", "NIGHT", "SPARK", "DRIFT", "SIGNAL", "CURRENT", "FLASH",
    "EMBER", "NORTH", "VELOCITY", "ECHO", "MERIDIAN", "HALO", "RIPTIDE",
    "COPPER", "IVORY", "AETHER", "PULSE", "VORTEX", "LANTERN",
)
_SUFFIX_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def callsign_for_run(run_id: str = "", *, chat_id: int = 0) -> str:
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
    if answered <= 0 and dialed < 5:
        return "◌ Quiet", "Waiting for first answers"
    if answered <= 0:
        return "○ Silent", "Answers not converting yet"
    rate = press1 / answered
    if rate >= 0.18:
        return "▲ Hot", f"{rate * 100:.1f}% of answers press 1"
    if rate >= 0.10:
        return "● Warm", f"{rate * 100:.1f}% press-1 rate"
    if rate >= 0.05:
        return "◉ Steady", f"{rate * 100:.1f}% press-1 rate"
    if press1 > 0:
        return "○ Cool", f"{rate * 100:.1f}% — catching some"
    return "◌ Ice", "Answers landing, no press-1s yet"


def heat_bar(*, dialed: int, answered: int, press1: int, width: int = 10) -> str:
    if answered <= 0:
        return "·" * width
    rate = min(1.0, (press1 / answered) / 0.22)
    filled = int(round(width * rate))
    return "▓" * filled + "░" * (width - filled)


def floor_clock() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def welcome_card(*, transfer: str = "", loaded: int = 0, grant_left: str = "") -> str:
    lines = [
        ui.muted("Press-1 operations · live transfers"),
        ui.rule(),
        ui.kv("Route", transfer or "not set", icon="◈"),
        ui.kv("Hopper", f"{loaded:,} leads" if loaded else "empty", icon="▣"),
    ]
    if grant_left:
        lines.append(ui.kv("Access", grant_left, icon="◇"))
    lines.extend(
        [
            ui.rule(),
            "Paste numbers · drop a CSV · tap Launch.",
            "",
            ui.muted(f"Floor clock  {floor_clock()}"),
        ]
    )
    return ui.card("THE FLOOR", lines)


def help_card() -> str:
    return ui.card(
        "COMMANDS",
        [
            ui.muted("Essentials"),
            ui.bullet("/go", "check stack + launch"),
            ui.bullet("/stop", "kill this chat's campaign"),
            ui.bullet("/pulse", "live conversion read"),
            ui.bullet("/testcall", "prove press-1 on your phone"),
            "",
            ui.muted("Campaign"),
            ui.bullet("/pause", "hold new dials"),
            ui.bullet("/unpause", "resume"),
            ui.bullet("/retry", "restart failed hopper"),
            ui.bullet("/dashboard", "pin a live board"),
            "",
            ui.muted("Setup"),
            ui.bullet("/settings", "transfer route"),
            ui.bullet("/audio", "swap IVR"),
            ui.bullet("/testnumber", "your test mobile"),
            ui.bullet("/clear", "wipe loaded leads"),
            ui.bullet("/schedule", "queue a timed run"),
            "",
            ui.muted("Owner"),
            ui.bullet("/addkey", "@user 24h seat"),
            ui.bullet("/listkeys", "who has access"),
            ui.bullet("/repair", "re-sync dial stack"),
        ],
        expandable=True,
    )


def menu_footer() -> str:
    return (
        "\n"
        + ui.muted("Tip: /go runs the same transfer path as /testcall.")
        + "\n"
        + ui.muted("Each campaign gets a callsign · press-1s alert live.")
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
    state = str(st.get("dial_state", "idle") or "idle")
    badge, blurb = heat_label(dialed=dialed, answered=answered, press1=press1)
    bar = heat_bar(dialed=dialed, answered=answered, press1=press1)
    ans_rate = (answered * 100 / dialed) if dialed else 0.0
    p1_of_ans = (press1 * 100 / answered) if answered else 0.0

    state_icon = {
        "running": "●",
        "paused": "⏸",
        "stalled": "⚠",
        "finished": "✓",
        "finishing": "…",
    }.get(state, "○")

    title = f"STATUS  ·  {callsign}" if callsign else "STATUS"
    lines = [
        ui.esc(f"{badge}  {bar}"),
        ui.muted(blurb),
        ui.rule(),
        ui.kv("State", f"{state_icon} {state}"),
        ui.kv("Live", live),
        ui.kv("Dialed", f"{dialed} / {total or '—'}"),
        ui.kv("Waiting", hopper),
        ui.rule(),
        ui.kv("Answer rate", f"{ans_rate:.0f}%"),
        ui.kv("P1 / answer", f"{p1_of_ans:.1f}%"),
        ui.kv("Press-1s", press1),
    ]
    if failed:
        lines.append(ui.kv("Failed", failed))
    if transfer:
        lines.append(ui.rule())
        lines.append(ui.kv("Route", transfer, icon="◈"))
    if loaded:
        lines.append(ui.kv("In bot", f"{loaded} leads", icon="▣"))
    remaining = max(0, (total or dialed + hopper) - dialed)
    forecast = forecast_line(
        dialed=dialed, answered=answered, press1=press1, remaining=remaining
    )
    if forecast:
        lines.append(ui.rule())
        lines.append(ui.muted(forecast))
    lines.append("")
    lines.append(ui.muted(f"Floor clock  {floor_clock()}"))
    return ui.card(title, lines)


def hit_alert(*, callsign: str, press1: int, answered: int, lead_hint: str = "") -> str:
    rate = (press1 * 100 / answered) if answered else 0.0
    lines = [
        ui.kv("Hit", f"#{press1}"),
        ui.kv("Callsign", callsign or "—"),
        ui.kv("P1 / answer", f"{rate:.1f}%"),
    ]
    if lead_hint:
        lines.append(ui.kv("Lead", lead_hint))
    lines.extend(["", ui.muted("Transfer path engaged.")])
    return ui.card("PRESS-1", lines)


def launch_banner(*, callsign: str, count: int, cap: int, gap: float) -> str:
    return ui.card(
        "FLOOR OPEN",
        [
            ui.kv("Callsign", callsign),
            ui.kv("Leads", f"{count:,}"),
            ui.kv("Ceiling", f"{cap} live"),
            ui.kv("Pace", f"{gap:g}s gap"),
            "",
            ui.muted("Watch for PRESS-1 alerts."),
        ],
    )


def preflight_card(checks: list[tuple[str, bool, str]]) -> str:
    lines = []
    for label, ok, detail in checks:
        mark = "✓" if ok else "✗"
        lines.append(f"{mark}  <b>{ui.esc(label)}</b>  —  {ui.esc(detail)}")
    all_ok = all(ok for _, ok, _ in checks)
    footer = "All green — launching." if all_ok else "Fix the red lines, then Launch again."
    lines.extend(["", ui.muted(footer)])
    return ui.card("PREFLIGHT", lines)


def finished_banner(*, callsign: str, dialed: int, answered: int, press1: int) -> str:
    badge, blurb = heat_label(dialed=dialed, answered=answered, press1=press1)
    return ui.card(
        "FLOOR CLOSED",
        [
            ui.kv("Callsign", callsign or "—"),
            ui.kv("Dialed", dialed),
            ui.kv("Answered", answered),
            ui.kv("Press-1", press1),
            "",
            ui.esc(f"{badge} — {blurb}"),
        ],
    )


def eta_minutes(*, count: int, cap: int = 40, gap: float = 0.2) -> int:
    if count <= 0:
        return 0
    _ = gap
    cps = max(0.5, min(float(cap) / 12.0, 8.0))
    return max(1, int(round(count / cps / 60.0)))


def leads_brief(*, count: int, replaced: int = 0, cap: int = 40, gap: float = 0.2) -> str:
    eta = eta_minutes(count=count, cap=cap, gap=gap)
    note = f" (replaced {replaced})" if replaced > 0 else ""
    return ui.card(
        "HOPPER",
        [
            ui.kv("Leads", f"{count:,}{note}"),
            ui.kv("Est. time", f"~{eta} min @ {cap} live"),
            "",
            ui.muted("Tap Launch for preflight + go."),
        ],
    )


def forecast_line(*, dialed: int, answered: int, press1: int, remaining: int) -> str:
    if answered < 8 or remaining <= 0:
        return ""
    if dialed >= 20:
        rate = press1 / dialed
        projected = int(round(press1 + rate * remaining))
        return f"P1 forecast ~{projected} if pace holds"
    rate = press1 / answered
    projected = int(round(press1 + rate * remaining * 0.35))
    return f"P1 forecast ~{projected} (early read)"


def fail_card(
    *,
    callsign: str = "",
    total: int = 0,
    reason: str = "",
) -> str:
    lines = [
        ui.muted("Dialer never started — hopper still on the server."),
        ui.rule(),
        ui.kv("Callsign", callsign or "—"),
        ui.kv("Waiting", f"{total:,} leads"),
    ]
    if reason:
        lines.extend(["", ui.muted(reason[:180])])
    lines.extend(["", ui.muted("Tap Retry — no need to re-upload.")])
    return ui.card("FAULT", lines)


def tidy_reason(err: object) -> str:
    text = str(err or "").strip()
    if not text:
        return "Dial script did not stay running"
    text = text.replace("\n", " · ")
    if "Dialer did not start" in text:
        return "Dial script exited immediately — usually an empty/corrupt upload"
    if "empty" in text.lower() and "script" in text.lower():
        return "Dial script was empty on the server (fixed — use Retry)"
    return text[:160]


def access_denied() -> str:
    return ui.deny()
