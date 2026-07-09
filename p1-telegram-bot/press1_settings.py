"""Press-1 bot settings (transfer target selection, display helpers)."""

from __future__ import annotations

import os

import press1_ui as ui

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
    "hardware3cx": {
        "id": "hardware3cx",
        "label": "Hardware 3CX",
        "fqdn": "46.101.24.34",
        "host": "46.101.24.34",
        "sip_contact": "46.101.24.34",
        "ext": "8000",
    },
    "marty3cx": {
        "id": "marty3cx",
        "label": "Marty 3CX",
        "fqdn": "188.166.173.188",
        "host": "188.166.173.188",
        "sip_contact": "188.166.173.188",
        "ext": "8000",
    },
    "skippascentre": {
        "id": "skippascentre",
        "label": "Skippas Centre",
        "fqdn": "fishers.3cx.uk",
        "host": "178.128.40.189",
        "sip_contact": "fishers.3cx.uk",
        "ext": "8000",
    },
    "money3c": {
        "id": "money3c",
        "label": "Money 3C",
        "fqdn": "work.my3cx.us",
        "host": "45.55.37.188",
        "sip_contact": "work.my3cx.us",
        "ext": "8000",
    },
    "slimzx": {
        "id": "slimzx",
        "label": "Slimzx Untold",
        "fqdn": "slimzxuntold.my3cx.co.uk",
        "host": "178.128.170.37",
        "sip_contact": "slimzxuntold.my3cx.co.uk",
        "ext": "8000",
    },
    "forward020": {
        "id": "forward020",
        "label": "020 3488 3405",
        "mode": "number",
        "number": "442034883405",
        "display": "02034883405",
    },
}

DEFAULT_THREECX = os.getenv("PRESS1_THREECX_DEFAULT", "swapofica")


def profile(profile_id: str) -> dict[str, str]:
    key = (profile_id or "").strip().lower()
    if key not in THREECX_PROFILES:
        raise ValueError(f"Unknown transfer profile: {profile_id!r}")
    return THREECX_PROFILES[key]


def is_number_transfer(p: dict[str, str]) -> bool:
    return p.get("mode") == "number"


def transfer_dial_target(p: dict[str, str]) -> str:
    if is_number_transfer(p):
        return f"PJSIP/{p['number']}@bitcall"
    return f"PJSIP/{p['ext']}@p1-{p['id']}"


def transfer_display(p: dict[str, str]) -> str:
    if is_number_transfer(p):
        return f"{p['label']} ({p.get('display', p['number'])})"
    return f"{p['label']} (ext {p['ext']})"


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
    card = ui.card(
        "⚙️  SETTINGS",
        [
            ui.bullet("IVR audio", sound_name, icon="🔊"),
            ui.bullet("Call gap", f"{call_gap:g}s", icon="⏱"),
            ui.bullet("Batch", f"{batch_size} · pause {batch_pause}s", icon="📦"),
            ui.bullet("Max concurrent", max_concurrent or "unlimited", icon="📡"),
            "",
            ui.bullet("Transfer", transfer_display(p), icon="🎯"),
        ],
    )
    return f"{card}\n<i>Tap a button below — settings apply to <b>this chat only</b>.</i>"
