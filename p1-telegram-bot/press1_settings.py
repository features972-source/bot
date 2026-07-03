"""Press-1 bot settings (transfer target selection, display helpers)."""

from __future__ import annotations

import os

THREECX_PROFILES: dict[str, dict[str, str]] = {
    "swapofica": {
        "id": "swapofica",
        "label": "Swapofica",
        "fqdn": "swapofica.ga.3cx.us",
        "host": "146.190.173.110",
        "sip_contact": "146.190.173.110",
        "ext": "8000",
    },
    "4wf": {
        "id": "4wf",
        "label": "4wf",
        "fqdn": "142.93.125.92",
        "host": "142.93.125.92",
        "sip_contact": "142.93.125.92",
        "ext": "8000",
    },
    "q2premium": {
        "id": "q2premium",
        "label": "Q2 Premium",
        "fqdn": "q2premium.3cx.uk",
        "host": "144.126.202.88",
        "sip_contact": "q2premium.3cx.uk",
        "ext": "8000",
    },
    "legacy": {
        "id": "legacy",
        "label": "Legacy",
        "fqdn": "46.101.77.174",
        "host": "46.101.77.174",
        "sip_contact": "46.101.77.174",
        "ext": "8000",
    },
    "usnow": {
        "id": "usnow",
        "label": "US Now",
        "fqdn": "137.184.78.236",
        "host": "137.184.78.236",
        "sip_contact": "137.184.78.236",
        "ext": "8000",
    },
}

DEFAULT_THREECX = os.getenv("PRESS1_THREECX_DEFAULT", "swapofica")


def profile(profile_id: str) -> dict[str, str]:
    key = (profile_id or "").strip().lower()
    if key not in THREECX_PROFILES:
        raise ValueError(f"Unknown transfer profile: {profile_id!r}")
    return THREECX_PROFILES[key]


def format_settings_text(
    *,
    threex_id: str,
    sound_name: str,
    call_gap: float,
    batch_size: int,
    batch_pause: int,
    max_concurrent: int,
) -> str:
    p = profile(threex_id)
    lines = [
        "⚙️ Press-1 settings\n",
        f"🔊 IVR audio: {sound_name}",
        f"⏱ Call gap: {call_gap:g}s | Batch: {batch_size} | Pause: {batch_pause}s",
        f"📡 Max concurrent: {max_concurrent or 'unlimited'}",
        "",
        f"🎯 Press-1 transfer: {p['label']}",
        f"   • Extension: {p['ext']}",
        "",
        "Tap a button below to change the transfer destination.",
    ]
    return "\n".join(lines)
