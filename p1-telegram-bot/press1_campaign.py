"""Campaign dashboard formatting, ETA prediction, and progress animation."""

from __future__ import annotations

import time

_ANIM_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def progress_bar(pct: int, width: int = 14) -> str:
    pct = max(0, min(100, pct))
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


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
    current, total_batches = batch_numbers(dialed, total, batch_size)
    if total_batches <= 0:
        return ""
    icon = _ANIM_FRAMES[frame % len(_ANIM_FRAMES)]
    return f"{icon} Dialing batch {current}/{total_batches}"


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
            forecast = f"{low}-{high}"
        elif press1 > 0:
            forecast = str(press1)
    return eta, forecast


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
    include_batch: bool = True,
) -> str:
    progress = progress or {}
    total = int(st.get("list_size", 0) or 0) or total_leads
    dialed = int(st.get("dialed", 0) or 0)
    answered = int(st.get("answered", 0) or 0)
    press1 = int(st.get("press1", 0) or 0)
    hopper = int(st.get("hopper", 0) or 0)
    live = int(st.get("live", 0) or 0)
    failed = int(st.get("failed", 0) or 0)
    dial_state = st.get("dial_state", "")
    pct = (dialed * 100 // total) if total > 0 else 0

    lines: list[str] = []
    if finished or dial_state in ("finished", "stalled"):
        lines.append("✅ Campaign finished\n")
    elif dial_state == "finishing":
        lines.append(f"🟡 Campaign finishing — {total} leads\n")
    else:
        lines.append(f"📊 Campaign live — {total} leads\n")

    lines.append(f"{progress_bar(pct)}  {pct}%  ·  {dialed}/{total}")
    if include_batch and dial_state in ("running", "paused", "finishing") and total > 0:
        batch_line = animated_batch_line(dialed, total, batch_size, frame)
        if batch_line:
            lines.append(batch_line)

    lines.extend(
        [
            "",
            f"📞 Dialed: {dialed}",
            f"⏳ Left: {hopper}",
            f"📡 Live now: {live}",
            f"✅ Answered: {answered}",
            f"🔥 Press-1: {press1}",
        ]
    )
    if failed > 0:
        lines.append(f"❌ Failed: {failed}")

    if dialed > 0:
        ans_pct = answered * 100 / dialed
        p1_pct = press1 * 100 / dialed
        lines.append(f"\n📈 Answer {ans_pct:.1f}%  ·  P1 {p1_pct:.1f}%")

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
    if eta and not finished:
        eta_line = f"⏱ ETA {eta}"
        if forecast:
            eta_line += f"  ·  Expected press-1s: {forecast}"
        lines.append(eta_line)

    return "\n".join(lines)


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
    progress = progress or {}
    dial_state = st.get("dial_state", "idle")
    total = int(st.get("list_size", 0) or 0) or total_leads
    dialed = int(st.get("dialed", 0) or 0)
    state_labels = {
        "running": "🟢 Dialling",
        "paused": "⏸ Paused",
        "finishing": "🟡 Finishing",
        "finished": "✅ Finished",
        "stalled": "⚠️ Stopped early",
        "idle": "⚪ Idle",
    }
    if dial_state == "idle" and total == 0 and dialed == 0:
        lines = [
            "🎛 LIVE DASHBOARD\n",
            "⚪ No active campaign",
            "",
            f"💾 Loaded in bot: {loaded_in_bot}",
            f"⚙️ Gap {call_gap:g}s · batch {batch_size} · max {max_concurrent or '∞'}",
            f"🎯 Transfer: {transfer_label}",
        ]
        if scheduled_count > 0:
            lines.append(f"⏰ Scheduled: {scheduled_count}")
        lines.append("\nLoad leads and /run · updates every 3s")
        return "\n".join(lines)
    body = format_campaign_body(
        st,
        total_leads,
        progress=progress,
        call_gap=call_gap,
        batch_size=batch_size,
        batch_pause=batch_pause,
        frame=frame,
        finished=dial_state in ("finished", "stalled", "idle") and int(st.get("dialed", 0) or 0) == 0,
        include_batch=dial_state in ("running", "paused", "finishing"),
    )
    lines = ["🎛 LIVE DASHBOARD\n", body, "", state_labels.get(dial_state, "⚪ Unknown")]
    lines.append(
        f"⚙️ Gap {call_gap:g}s · batch {batch_size} · max {max_concurrent or '∞'}"
    )
    lines.append(f"🎯 Transfer: {transfer_label}")
    if loaded_in_bot > 0:
        lines.append(f"💾 Loaded in bot: {loaded_in_bot}")
    if scheduled_count > 0:
        lines.append(f"⏰ Scheduled: {scheduled_count}")
    lines.append("\nUpdates every 3s · /stop to end campaign")
    return "\n".join(lines)
