#!/usr/bin/env python3
"""Press-1 operator cards — plain labels, no metaphor.

Easy to scan on a phone during a live campaign.
"""

from __future__ import annotations

from datetime import datetime, timezone

import press1_ui as ui


def run_label(*, run_since: str = "", run_id: str = "") -> str:
    """Human run title like 'Run 23:12' — not a radio callsign."""
    raw = (run_since or "").strip()
    if raw:
        # Accept "HH:MM:SS" or ISO-ish stamps; take clock if present.
        for part in raw.replace("T", " ").split():
            if ":" in part and part[0].isdigit():
                return f"Run {part[:5]}"
    if run_id:
        # Fallback: last 4 of run token
        tail = re_sub_digits(run_id)
        if tail:
            return f"Run {tail}"
    return f"Run {datetime.now(timezone.utc).strftime('%H:%M')}"


def re_sub_digits(run_id: str) -> str:
    digits = "".join(c for c in run_id if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else ""


def fresh_callsign() -> str:
    """Legacy name kept for callers — returns a plain Run HH:MM label."""
    return run_label()


def callsign_for_run(run_id: str = "", *, chat_id: int = 0) -> str:
    _ = chat_id
    return run_label(run_id=run_id)


def heat_label(*, dialed: int, answered: int, press1: int) -> tuple[str, str]:
    """Kept for compatibility — returns empty so UI never shows mood lines."""
    _ = dialed, answered, press1
    return "", ""


def floor_clock() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def welcome_card(*, transfer: str = "", loaded: int = 0, grant_left: str = "") -> str:
    lines = [
        ui.kv("Route", transfer or "not set"),
        ui.kv("Leads", f"{loaded:,}" if loaded else "none"),
    ]
    if grant_left:
        lines.append(ui.kv("Access", grant_left))
    lines.extend(
        [
            "",
            "Paste numbers or a CSV, then tap Launch.",
            "",
            ui.muted(floor_clock()),
        ]
    )
    return ui.card("Press-1", lines)


def help_card() -> str:
    return ui.card(
        "Commands",
        [
            ui.bullet("/go", "launch"),
            ui.bullet("/stop", "stop campaign"),
            ui.bullet("/pause", "pause new dials"),
            ui.bullet("/unpause", "resume"),
            ui.bullet("/testcall", "ring your test phone"),
            ui.bullet("/settings", "transfer route"),
            ui.bullet("/audio", "change IVR"),
            ui.bullet("/clear", "wipe leads"),
        ],
        expandable=True,
    )


def menu_footer() -> str:
    return "\n" + ui.muted("Paste a list · Launch · Stop when done.")


def pulse_card(
    st: dict[str, str],
    *,
    callsign: str = "",
    transfer: str = "",
    loaded: int = 0,
) -> str:
    from press1_campaign import progress_bar

    dialed = int(st.get("dialed", 0) or 0)
    answered = int(st.get("answered", 0) or 0)
    press1 = int(st.get("press1", 0) or 0)
    live = int(st.get("live", 0) or 0)
    hopper = int(st.get("hopper", 0) or 0)
    total = int(st.get("list_size", 0) or 0)
    failed = int(st.get("failed", 0) or 0)
    state = str(st.get("dial_state", "idle") or "idle")
    ans_rate = (answered * 100 / dialed) if dialed else 0.0
    p1_of_ans = (press1 * 100 / answered) if answered else 0.0
    pct = (dialed * 100 // total) if total > 0 else 0

    chip = {
        "running": "●  live",
        "paused": "⏸  paused",
        "stalled": "⚠  stopped",
        "finished": "✓  done",
        "finishing": "…  finishing",
    }.get(state, "○  idle")

    title = callsign if callsign.startswith("Run") else (callsign or "Status")
    if title and not title.startswith("Run") and title not in ("Status", "STATUS"):
        title = "Status"
    lines: list[str] = [ui.esc(chip), ""]
    if total > 0:
        lines.append(f"{progress_bar(pct)}  <b>{pct}%</b>")
        lines.append(ui.muted(f"{dialed:,} of {total:,} dialed"))
        lines.append("")
    lines.extend(
        [
            f"<b>{live}</b>  live",
            f"<b>{answered}</b>  answered"
            + (f"  ·  {ans_rate:.0f}%" if dialed else ""),
            f"<b>{press1}</b>  press-1"
            + (f"  ·  {p1_of_ans:.1f}% of answers" if answered else ""),
        ]
    )
    if failed:
        lines.append(f"<b>{failed}</b>  failed")
    foot: list[str] = []
    if hopper:
        foot.append(f"{hopper:,} waiting")
    if transfer:
        foot.append(transfer)
    if loaded:
        foot.append(f"{loaded} ready")
    if foot:
        lines.append("")
        lines.append(ui.muted(" · ".join(foot)))
    lines.append(ui.muted(floor_clock()))
    return ui.card(title, lines)


def hit_alert(*, callsign: str = "", press1: int, answered: int, lead_hint: str = "") -> str:
    _ = callsign
    rate = (press1 * 100 / answered) if answered else 0.0
    lines = [f"<b>#{press1}</b>  press-1"]
    if answered:
        lines.append(ui.muted(f"{rate:.0f}% of answers"))
    if lead_hint:
        lines.append(ui.code(lead_hint))
    return ui.card("Press-1", lines)


def launch_banner(*, callsign: str = "", count: int, cap: int, gap: float) -> str:
    title = callsign if str(callsign).startswith("Run") else run_label()
    return ui.card(
        title,
        [
            ui.esc("●  starting"),
            "",
            f"<b>{count:,}</b>  leads",
            ui.muted(f"max {cap} live · {gap:g}s gap"),
        ],
    )


def preflight_card(checks: list[tuple[str, bool, str]]) -> str:
    lines = []
    for label, ok, detail in checks:
        mark = "✓" if ok else "✗"
        lines.append(f"{mark}  <b>{ui.esc(label)}</b>  —  {ui.esc(detail)}")
    all_ok = all(ok for _, ok, _ in checks)
    footer = "Ready — launching." if all_ok else "Fix the red lines, then Launch again."
    lines.extend(["", ui.muted(footer)])
    return ui.card("Check", lines)


def finished_banner(*, callsign: str = "", dialed: int, answered: int, press1: int) -> str:
    ans_rate = (answered * 100 / dialed) if dialed else 0.0
    p1_of_ans = (press1 * 100 / answered) if answered else 0.0
    title = callsign if str(callsign).startswith("Run") else "Done"
    return ui.card(
        title,
        [
            ui.esc("✓  done"),
            "",
            f"<b>{dialed:,}</b>  dialed",
            f"<b>{answered}</b>  answered"
            + (f"  ·  {ans_rate:.0f}%" if dialed else ""),
            f"<b>{press1}</b>  press-1"
            + (f"  ·  {p1_of_ans:.1f}% of answers" if answered else ""),
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
        "Leads",
        [
            ui.kv("Loaded", f"{count:,}{note}"),
            ui.kv("Est. time", f"~{eta} min @ {cap} live"),
            "",
            ui.muted("Tap Launch to start."),
        ],
    )


def forecast_line(*, dialed: int, answered: int, press1: int, remaining: int) -> str:
    if answered < 8 or remaining <= 0:
        return ""
    if dialed >= 20:
        rate = press1 / dialed
        projected = int(round(press1 + rate * remaining))
        return f"~{projected} press-1s if pace holds"
    rate = press1 / answered
    projected = int(round(press1 + rate * remaining * 0.35))
    return f"~{projected} press-1s (early)"


def fail_card(
    *,
    callsign: str = "",
    total: int = 0,
    reason: str = "",
) -> str:
    _ = callsign
    lines = [
        ui.muted("Dialer did not start — leads are still loaded."),
        "",
        ui.kv("Waiting", f"{total:,} leads"),
    ]
    if reason:
        lines.extend(["", ui.muted(reason[:180])])
    lines.extend(["", ui.muted("Tap Retry — no need to re-upload.")])
    return ui.card("Stopped", lines)


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
