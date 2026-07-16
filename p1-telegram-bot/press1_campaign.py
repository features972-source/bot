"""Campaign live board — one clean card, scannable on a phone.

Telegram HTML only. Visual priority: callsign → progress → Live / Answered / Press-1.
"""

from __future__ import annotations

import time

import press1_ui as ui

_PROGRESS_WIDTH = 10


def progress_bar(pct: int, width: int = _PROGRESS_WIDTH) -> str:
    pct = max(0, min(100, pct))
    filled = int(round(width * pct / 100))
    # Always show at least one tick once dialing has started.
    if pct > 0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (width - filled)


def gauge(pct: int, width: int = _PROGRESS_WIDTH) -> str:
    return progress_bar(pct, width)


def progress_line(pct: int, dialed: int, total: int) -> str:
    return f"{progress_bar(pct)}  <b>{pct}%</b>"


def batch_numbers(dialed: int, total: int, batch_size: int) -> tuple[int, int]:
    batch_size = max(1, batch_size)
    total_batches = max(1, (total + batch_size - 1) // batch_size) if total > 0 else 0
    if total <= 0:
        return 0, 0
    if dialed >= total:
        return total_batches, total_batches
    current = min(total_batches, dialed // batch_size + 1)
    return current, total_batches


def animated_batch_line(
    dialed: int,
    total: int,
    batch_size: int,
    frame: int,
) -> str:
    # Intentionally unused in the live card — batch noise made campaigns feel busy.
    return ""


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem = minutes % 60
    if rem:
        return f"{hours}h {rem}m"
    return f"{hours}h"


def _dial_rate(
    dialed: int,
    progress: dict,
    *,
    call_gap: float,
    batch_size: int,
    batch_pause: int,
) -> float:
    now = time.time()
    samples: list[tuple[float, int]] = progress.setdefault("pace_samples", [])
    if not samples or samples[-1][1] != dialed:
        samples.append((now, dialed))
    samples[:] = [(t, d) for t, d in samples if now - t <= 300][-15:]
    if len(samples) >= 2:
        t0, d0 = samples[0]
        t1, d1 = samples[-1]
        dt = t1 - t0
        dd = d1 - d0
        if dt >= 8 and dd > 0:
            return dd / dt
    per_call = call_gap + (batch_pause / max(1, batch_size))
    return 1.0 / per_call if per_call > 0 else 0.0


def predict_eta(
    *,
    dialed: int,
    total: int,
    hopper: int,
    answered: int,
    press1: int,
    progress: dict,
    call_gap: float,
    batch_size: int,
    batch_pause: int,
    dial_state: str,
) -> tuple[str | None, str | None]:
    if dial_state in ("finished", "stalled", "idle") or total <= 0 or hopper <= 0:
        return None, None
    rate = _dial_rate(
        dialed,
        progress,
        call_gap=call_gap,
        batch_size=batch_size,
        batch_pause=batch_pause,
    )
    if rate <= 0:
        return None, None
    eta = f"~{_format_duration(hopper / rate)}"
    forecast = None
    if dialed >= 10 and press1 >= 0:
        p1_rate = press1 / dialed
        remaining_p1 = hopper * p1_rate
        low = max(press1, int(press1 + remaining_p1 * 0.65))
        high = max(low, int(press1 + remaining_p1 * 1.35) + 1)
        if high > press1:
            forecast = f"{low}–{high}"
        elif press1 > 0:
            forecast = str(press1)
    return eta, forecast


def _status_chip(dial_state: str, finished: bool, frame: int = 0) -> str:
    import press1_fx as fx

    if finished or dial_state == "finished":
        return "✓  closed"
    if dial_state == "stalled":
        return "⚠  stalled"
    if dial_state == "finishing":
        return "…  wrapping up"
    if dial_state == "paused":
        return "⏸  paused"
    if dial_state == "running":
        return f"{fx.live_pulse(frame)}  live"
    return "○  standby"


def _metric(label: str, value: object, detail: str = "") -> str:
    """Big number row — value first, label soft."""
    if detail:
        return f"<b>{ui.esc(value)}</b>  {ui.esc(detail)}\n{ui.muted(label)}"
    return f"<b>{ui.esc(value)}</b>\n{ui.muted(label)}"


def format_campaign_body(
    st: dict[str, str],
    total_leads: int,
    *,
    progress: dict | None = None,
    call_gap: float = 0.2,
    batch_size: int = 100,
    batch_pause: int = 0,
    frame: int = 0,
    finished: bool = False,
    transfer_label: str = "",
    max_concurrent: int = 0,
) -> str:
    import press1_floor as floor
    import press1_fx as fx

    progress = progress or {}
    total = int(st.get("list_size", 0) or 0) or total_leads
    dialed = int(st.get("dialed", 0) or 0)
    answered = int(st.get("answered", 0) or 0)
    press1 = int(st.get("press1", 0) or 0)
    hopper = int(st.get("hopper", 0) or 0)
    live = int(st.get("live", 0) or 0)
    failed = int(st.get("failed", 0) or 0)
    dial_state = st.get("dial_state", "")
    callsign = str(progress.get("callsign") or "")
    err = str(progress.get("error") or "").strip()
    pct = (dialed * 100 // total) if total > 0 else 0

    if dial_state == "stalled" and dialed <= 0:
        return floor.fail_card(
            callsign=callsign,
            total=total or total_leads,
            reason=floor.tidy_reason(err),
        )

    title = callsign or "CAMPAIGN"
    chip = _status_chip(dial_state, finished, frame=frame)
    mood, mood_blurb = floor.heat_label(dialed=dialed, answered=answered, press1=press1)
    incline = fx.record_incline(progress, answered=answered, press1=press1)
    spark = fx.incline_spark(incline)

    bar = (
        fx.progress_shimmer(pct, frame)
        if dial_state == "running" and not finished
        else progress_bar(pct)
    )

    lines: list[str] = [
        ui.esc(chip),
        "",
        f"{bar}  <b>{pct}%</b>",
        ui.muted(f"{dialed:,} of {total:,} dialed"),
        "",
    ]

    # Hero trio — the only numbers that matter at a glance
    ans_tail = f"  ·  {answered * 100 / dialed:.0f}%" if dialed > 0 else ""
    p1_tail = (
        f"  ·  {press1 * 100 / answered:.1f}% of answers" if answered > 0 else ""
    )
    lines.append(f"<b>{live}</b>  live")
    lines.append(f"<b>{answered}</b>  answered{ui.esc(ans_tail)}")
    lines.append(f"<b>{press1}</b>  press-1{ui.esc(p1_tail)}")
    if spark and (answered >= 2 or press1 > 0):
        lines.append(ui.muted(f"incline  {spark}"))

    if failed > 0:
        lines.append(f"<b>{failed}</b>  failed")

    # Soft mood — skip harsh early noise; only after we have signal
    if answered >= 3 or press1 > 0:
        lines.append("")
        lines.append(ui.muted(f"{mood} · {mood_blurb}"))

    eta, forecast = predict_eta(
        dialed=dialed,
        total=total,
        hopper=hopper,
        answered=answered,
        press1=press1,
        progress=progress,
        call_gap=call_gap,
        batch_size=batch_size,
        batch_pause=batch_pause,
        dial_state=dial_state,
    )

    footer_bits: list[str] = []
    if eta and not finished:
        footer_bits.append(f"{eta} left")
    if hopper > 0 and not finished:
        footer_bits.append(f"{hopper:,} waiting")
    if forecast and not finished and press1 > 0:
        footer_bits.append(f"P1 ~{forecast}")
    route = (transfer_label or "").strip()
    if route:
        footer_bits.append(route)

    if footer_bits:
        lines.append("")
        lines.append(ui.muted(" · ".join(footer_bits)))

    return ui.card(title, lines)


def format_dashboard(
    st: dict[str, str],
    *,
    total_leads: int,
    loaded_in_bot: int,
    progress: dict | None,
    call_gap: float,
    batch_size: int,
    batch_pause: int,
    max_concurrent: int,
    transfer_label: str,
    frame: int,
    scheduled_count: int = 0,
) -> str:
    """Pinned board — same live card language, no nested SETUP block."""
    progress = progress or {}
    dial_state = st.get("dial_state", "idle")
    total = int(st.get("list_size", 0) or 0) or total_leads
    dialed = int(st.get("dialed", 0) or 0)
    callsign = str((progress or {}).get("callsign") or "")
    route = transfer_label or "—"
    pace = f"{call_gap:g}s · max {max_concurrent or 40}"

    if dial_state == "idle" and total == 0 and dialed == 0:
        lines = [
            ui.esc("○  standby"),
            "",
            ui.muted(route),
            ui.muted(pace),
        ]
        if loaded_in_bot > 0:
            lines.append("")
            lines.append(f"<b>{loaded_in_bot:,}</b>  ready to launch")
        if scheduled_count > 0:
            lines.append(ui.muted(f"{scheduled_count} scheduled"))
        title = callsign or "THE FLOOR"
        return (
            ui.card(title, lines)
            + "\n"
            + ui.muted("Paste a list · tap Launch")
        )

    body = format_campaign_body(
        st,
        total_leads,
        progress=progress,
        call_gap=call_gap,
        batch_size=batch_size,
        batch_pause=batch_pause,
        frame=frame,
        finished=dial_state in ("finished", "stalled", "idle") and dialed == 0,
        transfer_label=transfer_label,
        max_concurrent=max_concurrent,
    )
    tip = (
        ui.muted("Tap Retry · or Stop to close")
        if dial_state == "stalled"
        else ui.muted("Live board · Stop to close")
    )
    return f"{body}\n{tip}"
