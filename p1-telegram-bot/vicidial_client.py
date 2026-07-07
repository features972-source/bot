"""SSH control layer for the press-1 dial server (dialer, IVR, transfer, stats)."""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import paramiko

from press1_settings import DEFAULT_THREECX, THREECX_PROFILES, profile, transfer_dial_target
from press1_utils import normalize_uk, to_e164

HOST = os.getenv("VICIDIAL_SSH_HOST", "206.189.118.204")
USER = os.getenv("VICIDIAL_SSH_USER", "root")
CAMPAIGN = os.getenv("VICIDIAL_CAMPAIGN", "press1")
LIST_ID = int(os.getenv("VICIDIAL_LIST_ID", "101"))
SOUND_NAME = os.getenv("VICIDIAL_SOUND_NAME", "press1_alice")
SOUND_DIRS = (
    "/usr/share/asterisk/sounds/en",
    "/usr/share/asterisk/sounds/custom",
    "/var/lib/asterisk/sounds",
    "/var/lib/asterisk/sounds/en",
    "/var/lib/asterisk/sounds/custom",
)
SERVER_IP = os.getenv("VICIDIAL_SERVER_IP", "206.189.118.204")
MAX_CONCURRENT = int(os.getenv("VICIDIAL_MAX_CONCURRENT", "100"))
DIALER_CONCURRENT_CAP = int(os.getenv("VICIDIAL_DIALER_CAP", "0"))
BATCH_SIZE = int(os.getenv("VICIDIAL_BATCH_SIZE", "100"))
BATCH_PAUSE_SEC = int(os.getenv("VICIDIAL_BATCH_PAUSE_SEC", "0"))
CALL_GAP_SEC = float(os.getenv("VICIDIAL_CALL_GAP_SEC", "0.08"))
MAX_LEADS = int(os.getenv("VICIDIAL_MAX_LEADS", "5000"))
CPS = int(os.getenv("VICIDIAL_CPS", "30"))
# Stable outbound caller ID for BitCall (required — empty CLI causes instant hangup).
DEFAULT_CALLER_ID = re.sub(r"\D", "", os.getenv("VICIDIAL_CALLER_ID", "442038969244")) or "442038969244"
AU_CALLER_ID = re.sub(r"\D", "", os.getenv("VICIDIAL_AU_CALLER_ID", DEFAULT_CALLER_ID)) or DEFAULT_CALLER_ID
MIN_PHONE_DIGITS = 9


def outbound_caller_id(number: str) -> str:
    """Return CLI for call files."""
    return AU_CALLER_ID

DIAL_SCRIPT = "/tmp/press1_dial_run.sh"
DIAL_NUMBERS = "/tmp/press1_dial_numbers.txt"
DIAL_TOTAL = "/tmp/press1_dial_total"
DIAL_STARTED = "/tmp/press1_dial_started"
DIAL_FAILED = "/tmp/press1_dial_failed"
DIAL_STOP = "/tmp/press1_dial_stop"
DIAL_PAUSE = "/tmp/press1_dial_pause"
DIAL_LOG = "/tmp/press1_dial.log"
DIAL_RUN_MARK = "/tmp/press1_dial_run_mark"
DIAL_RUN_ID = "/tmp/press1_dial_run_id"
# Written by the dialplan (System app) — must live under /var/lib/asterisk so the
# SELinux-confined asterisk_t domain is allowed to append to them (not /tmp).
DIAL_RUN_PRESS1 = "/var/lib/asterisk/press1_run_press1"
DIAL_RUN_ANSWERED = "/var/lib/asterisk/press1_run_answered"
DIAL_STATS_DIR = "/var/lib/asterisk/stats"
DTMF_EVENTS_FILE = "/var/lib/asterisk/press1_dtmf_events.jsonl"
DIAL_LOCK = "/tmp/press1_dial.lock"
SETTINGS_PATH = "/var/lib/asterisk/press1_bot_settings.json"
CHAT_SETTINGS_PATH = "/var/lib/asterisk/press1_chat_settings.json"
ACCESS_PATH = "/var/lib/asterisk/press1_access.json"
SCHEDULES_PATH = "/var/lib/asterisk/press1_schedules.json"
DASHBOARDS_PATH = "/var/lib/asterisk/press1_dashboards.json"
PJSIP_CONF = "/etc/asterisk/pjsip.conf"

# Keep bridged BitCall <-> 3CX calls up (RTP keepalive, session timers, no RTP kill on hold).
_PJSIP_TRUNK_STABILITY = (
    "direct_media=no\n"
    "rtp_symmetric=yes\n"
    "force_rport=yes\n"
    "rewrite_contact=yes\n"
    "rtp_keepalive=30\n"
    "rtp_timeout=0\n"
    "rtp_timeout_hold=0\n"
    "timers=always\n"
    "timers_min_se=90\n"
    "timers_sess_expires=3600\n"
    "send_connected_line=no\n"
    "connected_line_method=invite\n"
)

_PJSIP_STABILITY_KV = {
    "rtp_keepalive": "30",
    "rtp_timeout": "0",
    "rtp_timeout_hold": "0",
    "timers": "always",
    "timers_min_se": "90",
    "timers_sess_expires": "3600",
}


def load_dashboards() -> list[dict]:
    """Persisted pinned dashboards: [{chat_id, message_id, user_id}]."""
    try:
        raw = run_remote(f"cat {DASHBOARDS_PATH} 2>/dev/null", timeout=15).strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("dashboards", [])
        return [d for d in data if isinstance(d, dict) and d.get("chat_id")]
    except Exception:
        return []


def save_dashboards(items: list[dict]) -> None:
    payload = json.dumps({"dashboards": items}, indent=2)
    run_remote(
        f"mkdir -p $(dirname {DASHBOARDS_PATH}); "
        f"cat > {DASHBOARDS_PATH} <<'EOF'\n{payload}\nEOF\n"
        f"chmod 644 {DASHBOARDS_PATH}",
        timeout=20,
    )


def _dial_done_path(run_id: str) -> str:
    return f"/tmp/press1_dial_done_{run_id}.txt"


def _safe_run_token(run_id: str) -> str:
    token = re.sub(r"[^0-9_]", "", str(run_id or ""))
    return token or "0"


def chat_cfg_run_id(chat_id: int) -> str:
    """Stable numeric Asterisk cfg key for a Telegram chat (test calls + settings)."""
    return f"9{abs(int(chat_id))}"


def _run_paths(run_id: str) -> dict[str, str]:
    """Per-campaign file paths on the dial server (supports parallel group runs)."""
    rid = _safe_run_token(run_id)
    return {
        "numbers": f"/tmp/press1_numbers_{rid}.txt",
        "script": f"/tmp/press1_dial_{rid}.sh",
        "pause": f"/tmp/press1_pause_{rid}",
        "stop": f"/tmp/press1_stop_{rid}",
        "started": f"/tmp/press1_started_{rid}",
        "failed": f"/tmp/press1_failed_{rid}",
        "total": f"/tmp/press1_total_{rid}",
        "lock": f"/tmp/press1_lock_{rid}",
        "done": _dial_done_path(rid),
    }


def _stats_answered_path(run_id: str) -> str:
    return f"{DIAL_STATS_DIR}/{run_id}/answered"


def _stats_press1_path(run_id: str) -> str:
    return f"{DIAL_STATS_DIR}/{run_id}/press1"


def _default_chat_settings() -> dict[str, str]:
    return {"threex_target": DEFAULT_THREECX, "sound_name": SOUND_NAME}


def chat_sound_name(chat_id: int) -> str:
    """Unique Asterisk sound stem per Telegram chat."""
    return f"p1c{abs(int(chat_id))}"


def _chat_key(chat_id: int) -> str:
    return str(int(chat_id))


def _load_chat_settings_store() -> dict:
    try:
        raw = run_remote(f"cat {CHAT_SETTINGS_PATH} 2>/dev/null", timeout=15).strip()
        if not raw:
            legacy = _default_chat_settings()
            try:
                old = run_remote(f"cat {SETTINGS_PATH} 2>/dev/null", timeout=15).strip()
                if old:
                    data = json.loads(old)
                    if data.get("threex_target"):
                        legacy["threex_target"] = str(data["threex_target"]).strip().lower()
            except Exception:
                pass
            return {"chats": {}, "default": legacy}
        data = json.loads(raw)
        data.setdefault("chats", {})
        data.setdefault("default", _default_chat_settings())
        return data
    except Exception:
        return {"chats": {}, "default": _default_chat_settings()}


def _save_chat_settings_store(data: dict) -> None:
    payload = json.dumps(data, indent=2)
    run_remote(
        f"mkdir -p $(dirname {CHAT_SETTINGS_PATH}); "
        f"cat > {CHAT_SETTINGS_PATH} <<'EOF'\n{payload}\nEOF\n"
        f"chmod 644 {CHAT_SETTINGS_PATH}",
        timeout=20,
    )


def get_chat_settings(chat_id: int) -> dict[str, str]:
    store = _load_chat_settings_store()
    key = _chat_key(chat_id)
    cfg = dict(store.get("default") or _default_chat_settings())
    cfg.update(store.get("chats", {}).get(key, {}))
    target = str(cfg.get("threex_target", DEFAULT_THREECX)).strip().lower()
    if target not in THREECX_PROFILES:
        target = DEFAULT_THREECX
    sound = str(cfg.get("sound_name", SOUND_NAME)).strip() or SOUND_NAME
    return {"threex_target": target, "sound_name": sound}


def save_chat_settings(chat_id: int, **fields: str) -> dict[str, str]:
    store = _load_chat_settings_store()
    key = _chat_key(chat_id)
    current = get_chat_settings(chat_id)
    if "threex_target" in fields:
        pid = str(fields["threex_target"]).strip().lower()
        profile(pid)
        current["threex_target"] = pid
    if "sound_name" in fields:
        current["sound_name"] = str(fields["sound_name"]).strip() or SOUND_NAME
    store.setdefault("chats", {})[key] = current
    _save_chat_settings_store(store)
    return current


def _default_settings() -> dict[str, str]:
    return _default_chat_settings()


def load_bot_settings() -> dict[str, str]:
    """Legacy global settings read (defaults only)."""
    store = _load_chat_settings_store()
    cfg = dict(store.get("default") or _default_chat_settings())
    target = str(cfg.get("threex_target", DEFAULT_THREECX)).strip().lower()
    if target not in THREECX_PROFILES:
        target = DEFAULT_THREECX
    return {"threex_target": target}


def save_bot_settings(data: dict[str, str]) -> None:
    store = _load_chat_settings_store()
    default = dict(store.get("default") or _default_chat_settings())
    if data.get("threex_target"):
        default["threex_target"] = str(data["threex_target"]).strip().lower()
        profile(default["threex_target"])
    store["default"] = default
    _save_chat_settings_store(store)


def get_threex_target(chat_id: int | None = None) -> str:
    if chat_id is not None:
        return get_chat_settings(chat_id)["threex_target"]
    return load_bot_settings().get("threex_target", DEFAULT_THREECX)


def _put_press1_db_entries(entries: dict[str, str]) -> None:
    """Write Asterisk DB keys (family press1) with verify — avoids shell quoting bugs."""
    import base64

    clean = {str(k): str(v) for k, v in entries.items() if k and v is not None}
    if not clean:
        return
    payload = base64.b64encode(json.dumps(clean).encode()).decode()
    run_remote(
        f"python3 <<'PY'\n"
        f"import base64, json, shlex, subprocess\n"
        f"entries = json.loads(base64.b64decode('{payload}').decode())\n"
        f"def db_ok(r):\n"
        f"    body = ((r.stdout or '') + (r.stderr or '')).strip()\n"
        f"    return r.returncode == 0 or 'Updated database successfully' in body or 'New entry added' in body\n"
        f"for key, val in entries.items():\n"
        f"    put = f'database put press1 {{shlex.quote(key)}} {{shlex.quote(val)}}'\n"
        f"    r = subprocess.run(['/usr/sbin/asterisk', '-rx', put], capture_output=True, text=True)\n"
        f"    body = ((r.stdout or '') + (r.stderr or '')).strip()\n"
        f"    if not db_ok(r):\n"
        f"        raise SystemExit(body or 'db put failed')\n"
        f"    get = f'database get press1 {{shlex.quote(key)}}'\n"
        f"    got = subprocess.run(['/usr/sbin/asterisk', '-rx', get], capture_output=True, text=True)\n"
        f"    gbody = (got.stdout or '') + (got.stderr or '')\n"
        f"    if val not in gbody:\n"
        f"        raise SystemExit(f'verify failed {{key}}={{val!r}} got {{gbody!r}}')\n"
        f"PY",
        timeout=45,
    )


def stop_all_dialers() -> str:
    """Kill every press-1 dial script and touch all stop markers."""
    return run_remote(
        "for pid in $(pgrep -f '/tmp/press1_dial_' 2>/dev/null); do kill -9 \"$pid\" 2>/dev/null || true; done; "
        "pkill -9 -f 'press1_dial_run.sh' 2>/dev/null || true; "
        "pkill -9 -f 'press1_dial_' 2>/dev/null || true; "
        "touch /tmp/press1_dial_stop 2>/dev/null || true; "
        "for f in /tmp/press1_stop_*; do touch \"$f\" 2>/dev/null || true; done; "
        "rm -f /tmp/press1_pause_* 2>/dev/null || true; "
        "sleep 2; "
        "pgrep -af '/tmp/press1_dial_' 2>/dev/null || echo 'all dialers stopped'",
        timeout=40,
    ).strip()


def _write_run_xfer_file(run_id: str, xfer: str) -> None:
    rid = _safe_run_token(run_id)
    run_remote(
        f"python3 <<'PY'\n"
        f"from pathlib import Path\n"
        f"Path('/tmp/press1_xfer_{rid}.txt').write_text({xfer!r})\n"
        f"PY",
        timeout=15,
    )


def apply_run_config(run_id: str, chat_id: int) -> dict[str, str]:
    """Bind IVR audio + transfer destination to a campaign run."""
    cfg = get_chat_settings(chat_id)
    p = profile(cfg["threex_target"])
    xfer = transfer_dial_target(p)
    sound = cfg["sound_name"]
    rid = _safe_run_token(run_id)
    entries: dict[str, str] = {
        f"cfg/{rid}/sound": sound,
        f"cfg/{rid}/xfer": xfer,
    }
    chat_rid = chat_cfg_run_id(chat_id)
    if chat_rid != rid:
        entries[f"cfg/{chat_rid}/sound"] = sound
        entries[f"cfg/{chat_rid}/xfer"] = xfer
    try:
        _put_press1_db_entries(entries)
        _write_run_xfer_file(rid, xfer)
    except Exception as e:
        raise RuntimeError(f"Failed to apply xfer {xfer} for run {rid}: {e}") from e
    return {
        "sound_name": sound,
        "xfer_dial": xfer,
        "threex_target": cfg["threex_target"],
        "label": p["label"],
        "run_id": rid,
    }


def apply_lead_run_config(lead_digits: str, chat_id: int, *, run_id: str | None = None) -> dict[str, str]:
    """Apply per-chat IVR/xfer for a single test call."""
    rid = _safe_run_token(run_id or chat_cfg_run_id(chat_id))
    cfg = apply_run_config(rid, chat_id)
    digits = re.sub(r"\D", "", lead_digits)
    _put_press1_db_entries(
        {
            "lead": digits,
            f"runs/{digits}": rid,
            f"lead/{digits}": digits,
            f"leadxfer/{digits}": cfg["xfer_dial"],
        }
    )
    return cfg


def ensure_all_threex_endpoints() -> str:
    """Provision one PJSIP endpoint per 3CX profile (parallel group campaigns)."""
    blocks: list[str] = []
    for pid, p in THREECX_PROFILES.items():
        if p.get("mode") == "number":
            continue
        ep = f"p1-{pid}"
        host = p["host"]
        contact = p["sip_contact"]
        blocks.append(
            f"\n[{ep}]\n"
            f"type=endpoint\n"
            f"context=from-trunk\n"
            f"disallow=all\n"
            f"allow=alaw,ulaw\n"
            f"{_PJSIP_TRUNK_STABILITY}"
            f"aors={ep}-aor\n"
            f"\n[{ep}-aor]\n"
            f"type=aor\n"
            f"contact=sip:{contact}:5060\n"
            f"\n[{ep}-identify]\n"
            f"type=identify\n"
            f"endpoint={ep}\n"
            f"match={host}\n"
        )
    marker = "# P1 per-profile 3CX endpoints"
    body = marker + "".join(blocks) + f"\n{marker}-end\n"
    return run_remote(
        f"python3 <<'PY'\n"
        f"from pathlib import Path\n"
        f"import re\n"
        f"p = Path('{PJSIP_CONF}')\n"
        f"text = p.read_text()\n"
        f"block = {body!r}\n"
        f"text = re.sub(r'\\n# P1 per-profile 3CX endpoints[\\s\\S]*?# P1 per-profile 3CX endpoints-end\\n?', '\\n', text)\n"
        f"if '{marker}' not in text:\n"
        f"    text = text.rstrip() + block\n"
        f"else:\n"
        f"    text = re.sub(r'# P1 per-profile 3CX endpoints[\\s\\S]*?# P1 per-profile 3CX endpoints-end', block.strip(), text)\n"
        f"p.write_text(text)\n"
        f"print('OK: p1 3cx endpoints')\n"
        f"PY\n"
        f"asterisk -rx 'module reload res_pjsip.so' >/dev/null 2>&1",
        timeout=60,
    ).strip()


def apply_threex_target(profile_id: str, chat_id: int | None = None) -> dict[str, str]:
    """Save transfer target for a chat and push xfer to Asterisk immediately."""
    p = profile(profile_id)
    if chat_id is None:
        save_bot_settings({"threex_target": profile_id})
    else:
        save_chat_settings(chat_id, threex_target=profile_id)
        apply_run_config(chat_cfg_run_id(chat_id), chat_id)
    ensure_all_threex_endpoints()
    return p


def ensure_threex_target(chat_id: int | None = None) -> dict[str, str]:
    target = get_threex_target(chat_id)
    return profile(target)


def sync_all_chat_xfer_configs() -> int:
    """Push saved per-chat transfer targets into Asterisk DB (survives restarts)."""
    store = _load_chat_settings_store()
    synced = 0
    for key, _cfg in store.get("chats", {}).items():
        try:
            chat_id = int(key)
        except (TypeError, ValueError):
            continue
        try:
            apply_run_config(chat_cfg_run_id(chat_id), chat_id)
            synced += 1
        except Exception:
            pass
    return synced


def ensure_press1_stack(chat_id: int | None = None, *, full: bool = False) -> dict[str, str]:
    """Refresh dial server stack. Use full=True only on boot (slow)."""
    if full:
        ensure_all_threex_endpoints()
        ensure_press1_dialplan()
        ensure_dtmf_listener()
        try:
            sync_all_chat_xfer_configs()
        except Exception:
            pass
    if chat_id is not None:
        return profile(get_threex_target(chat_id))
    return profile(get_threex_target())


def cleanup_stale_dialers(chat_id: int | None = None) -> str:
    """Stop orphaned per-chat dial scripts (and legacy press1_dial_run.sh)."""
    parts = [
        "pkill -9 -f 'press1_dial_run.sh' 2>/dev/null; true",
    ]
    if chat_id is not None:
        cid = abs(int(chat_id))
        parts.append(
            f"for f in /tmp/press1_dial_*_{cid}.sh; do "
            f'[ -f "$f" ] || continue; '
            f'pkill -9 -f "$f" 2>/dev/null; '
            f'rid=$(basename "$f" .sh | sed "s/^press1_dial_//"); '
            f'touch "/tmp/press1_stop_$rid" 2>/dev/null; '
            f"done"
        )
    parts.append("pgrep -af press1_dial 2>/dev/null | head -8 || echo 'no dialers'")
    return run_remote(" ; ".join(parts), timeout=25)


def unstick_dial_server() -> str:
    """Clear stale spool files and stuck dialers so new test/campaign calls are not blocked."""
    return run_remote(
        "pkill -9 -f '/tmp/press1_dial_' 2>/dev/null || true; "
        "pkill -9 -f 'press1_dial_run.sh' 2>/dev/null || true; "
        "find /var/spool/asterisk/outgoing -name 'press1_*.call' -delete 2>/dev/null || true; "
        "find /var/spool/asterisk/tmp -name 'press1_*.call' -delete 2>/dev/null || true; "
        "rm -f /tmp/press1_dial.lock /tmp/press1_dial_stop 2>/dev/null || true; "
        "echo cleared",
        timeout=25,
    ).strip()


def fix_bitcall_endpoint() -> str:
    """Normalize [bitcall] PJSIP endpoint — fixes duplicate allow= lines that break outbound dials."""
    trunk = _PJSIP_TRUNK_STABILITY.replace("\n", "\\n")
    return run_remote(
        f"python3 <<'PY'\n"
        f"import re\nfrom pathlib import Path\n"
        f"p = Path('{PJSIP_CONF}')\n"
        f"t = p.read_text()\n"
        f"m = re.search(r'\\[bitcall\\][\\s\\S]*?(?=\\n\\[|\\Z)', t)\n"
        f"if not m:\n"
        f"    print('no bitcall section')\n"
        f"    raise SystemExit(0)\n"
        f"old = m.group(0)\n"
        f"def grab(key, default=''):\n"
        f"    g = re.search(r'^{{key}}=(.*)$', old, re.M)\n"
        f"    return (g.group(1).strip() if g else default)\n"
        f"block = (\n"
        f"    '[bitcall]\\n'\n"
        f"    'type=endpoint\\n'\n"
        f"    'transport=transport-udp\\n'\n"
        f"    'context=from-trunk\\n'\n"
        f"    'disallow=all\\n'\n"
        f"    'allow=ulaw\\n'\n"
        f"    'allow=alaw\\n'\n"
        f"    'allow=telephone-event\\n'\n"
        f"    f'from_user={{grab(\"from_user\", \"f-features896\")}}\\n'\n"
        f"    f'from_domain={{grab(\"from_domain\", \"gateway.bitcall.io\")}}\\n'\n"
        f"    f'outbound_auth={{grab(\"outbound_auth\", \"bitcall-auth\")}}\\n'\n"
        f"    f'aors={{grab(\"aors\", \"bitcall-aor\")}}\\n'\n"
        f"    '{trunk}'\n"
        f"    'trust_id_outbound=yes\\n'\n"
        f"    'send_pai=yes\\n'\n"
        f"    'dtmf_mode=rfc4733\\n'\n"
        f")\n"
        f"t = t[:m.start()] + block + t[m.end():]\n"
        f"p.write_text(t)\n"
        f"print('OK: bitcall endpoint normalized')\n"
        f"PY\n"
        f"asterisk -rx 'module reload res_pjsip.so' 2>&1 | tail -1; "
        f"rm -f /var/spool/asterisk/outgoing/press1_test*.call 2>/dev/null; "
        f"asterisk -rx 'pjsip show endpoint bitcall' 2>&1 | grep -E '^allow|^Endpoint' | head -3",
        timeout=45,
    ).strip()


def repair_press1_server(chat_id: int | None = None, *, stop_stale: bool = False) -> dict[str, str]:
    """Re-deploy dialplan/DTMF; optionally stop stale dialers for one chat."""
    result: dict[str, str] = {}
    try:
        result["unstick"] = unstick_dial_server()
    except Exception as e:
        result["unstick"] = f"error: {e}"
    try:
        result["bitcall"] = fix_bitcall_endpoint()
    except Exception as e:
        result["bitcall"] = f"error: {e}"
    try:
        result["endpoints"] = ensure_all_threex_endpoints()
    except Exception as e:
        result["endpoints"] = f"error: {e}"
    try:
        result["dialplan"] = ensure_press1_dialplan()
    except Exception as e:
        result["dialplan"] = f"error: {e}"
    try:
        result["dtmf"] = ensure_dtmf_listener()
    except Exception as e:
        result["dtmf"] = f"error: {e}"
    try:
        result["xfer_sync"] = f"synced {sync_all_chat_xfer_configs()} chats"
    except Exception as e:
        result["xfer_sync"] = f"error: {e}"
    if stop_stale or chat_id is not None:
        try:
            result["stale_dialers"] = cleanup_stale_dialers(chat_id)
        except Exception as e:
            result["stale_dialers"] = f"error: {e}"
    return result


def bootstrap_press1_stack() -> dict[str, str]:
    """Full dialplan + endpoint + xfer sync (run in background on boot)."""
    repair_press1_server()
    return profile(get_threex_target())


def _dialplan_resolve_leadnum() -> str:
    """Recover LEADNUM when channel vars are missing (concurrent campaigns)."""
    return """ same => n,Set(LEADNUM=${FILTER(0-9,${LEADNUM})})
 same => n,Set(LEADNUM=${IF($[${LEN(${LEADNUM})}>=10]?${LEADNUM}:${FILTER(0-9,${GLOBAL(P1LEAD_${CHANNEL(uniqueid)})})})})
 same => n,Set(LEADNUM=${IF($[${LEN(${LEADNUM})}>=10]?${LEADNUM}:${FILTER(0-9,${__LEADNUM})})})
 same => n,Set(LEADNUM=${IF($[${LEN(${LEADNUM})}>=10]?${LEADNUM}:${FILTER(0-9,${DB(press1/lead/${FILTER(0-9,${LEADNUM})})})})})
 same => n,Set(LEADNUM=${IF($[${LEN(${LEADNUM})}>=10]?${LEADNUM}:${FILTER(0-9,${DB(press1/lead)})})})
 same => n,Set(LEADNUM=${IF($[${LEN(${LEADNUM})}>=10]?${LEADNUM}:${FILTER(0-9,${CALLERID(dnid)})})})
 same => n,Set(LEADNUM=${IF($[${LEN(${LEADNUM})}>=10]?${LEADNUM}:${FILTER(0-9,${PJSIP_HEADER(read,To)})})})
 same => n,Set(__LEADNUM=${LEADNUM})"""


def _dialplan_resolve_xfer(*, default_sound: str, default_xfer: str, allow_default: bool) -> str:
    """Resolve P1RUN/P1XFER from leadxfer then per-run cfg; optional Swapofica fallback."""
    xfer_fallback = (
        f'\n same => n,ExecIf($["${{LEN(${{P1XFER}})}}" = "0"]?Set(P1XFER={default_xfer}))'
        if allow_default
        else ""
    )
    return f""" same => n,Set(P1RUN=${{IF($[${{LEN(${{P1RUN}})}}>0]?${{P1RUN}}:${{DB(press1/runs/${{FILTER(0-9,${{LEADNUM}})}})}})}})
 same => n,ExecIf($["${{LEN(${{P1RUN}})}}" = "0"]?Set(P1RUN=0))
 same => n,Set(P1XFER=${{DB(press1/leadxfer/${{FILTER(0-9,${{LEADNUM}})}})}})
 same => n,ExecIf($["${{LEN(${{P1XFER}})}}" = "0"]?Set(P1XFER=${{DB(press1/cfg/${{P1RUN}}/xfer)}}))
 same => n,ExecIf($["${{LEN(${{P1SOUND}})}}" = "0"]?Set(P1SOUND=${{DB(press1/cfg/${{P1RUN}}/sound)}}))
 same => n,ExecIf($["${{LEN(${{P1SOUND}})}}" = "0"]?Set(P1SOUND={default_sound})){xfer_fallback}"""


def _press1_ivr_dialplan(*, server_ip: str, default_sound: str, default_xfer: str) -> str:
    """Canonical press1-ivr: per-run sound/xfer from Asterisk DB."""
    return f"""[press1-ivr]
exten => _X.,1,Set(LEADNUM=${{FILTER(0-9,${{EXTEN}})}})
 same => n,Set(__LEADNUM=${{LEADNUM}})
 same => n,Set(P1UID=${{CHANNEL(uniqueid)}})
 same => n,Set(GLOBAL(P1LEAD_${{P1UID}})=${{LEADNUM}})
 same => n,Goto(ivr,1)

exten => s,1,Set(LEADNUM=${{FILTER(0-9,${{LEADNUM}})}})
 same => n,Set(LEADNUM=${{IF($[${{LEN(${{LEADNUM}})}}>=10]?${{LEADNUM}}:${{FILTER(0-9,${{DB(press1/lead)}})}})}})
 same => n,Goto(ivr,1)

exten => ivr,1,Answer()
 same => n,Wait(2)
 same => n,Set(LEADNUM=${{IF($[${{LEN(${{LEADNUM}})}}>=10]?${{LEADNUM}}:${{FILTER(0-9,${{__LEADNUM}})}})}})
 same => n,Set(LEADNUM=${{IF($[${{LEN(${{LEADNUM}})}}>=10]?${{LEADNUM}}:${{FILTER(0-9,${{DB(press1/lead/${{FILTER(0-9,${{LEADNUM}})}})}})}})}})
 same => n,Set(LEADNUM=${{IF($[${{LEN(${{LEADNUM}})}}>=10]?${{LEADNUM}}:${{FILTER(0-9,${{DB(press1/lead)}})}})}})
 same => n,Set(LEADNUM=${{IF($[${{LEN(${{LEADNUM}})}}>=10]?${{LEADNUM}}:${{FILTER(0-9,${{CALLERID(dnid)}})}})}})
 same => n,Set(LEADNUM=${{IF($[${{LEN(${{LEADNUM}})}}>=10]?${{LEADNUM}}:${{FILTER(0-9,${{PJSIP_HEADER(read,To)}})}})}})
 same => n,Set(__LEADNUM=${{LEADNUM}})
 same => n,Set(P1UID=${{CHANNEL(uniqueid)}})
 same => n,Set(GLOBAL(P1LEAD_${{P1UID}})=${{LEADNUM}})
 same => n,Set(CHANNEL(language)=en)
 same => n,NoOp(IVR lead=${{LEADNUM}})
{_dialplan_resolve_xfer(default_sound=default_sound, default_xfer=default_xfer, allow_default=False)}
 same => n,System(mkdir -p {DIAL_STATS_DIR}/${{P1RUN}})
 same => n,System(echo 1 >> {DIAL_STATS_DIR}/${{P1RUN}}/answered)
 same => n,NoOp(IVR sound=${{P1SOUND}} xfer=${{P1XFER}} run=${{P1RUN}})
 same => n,Set(TIMEOUT(digit)=5)
 same => n,Set(TIMEOUT(response)=45)
 same => n,Background(${{P1SOUND}})
 same => n,WaitExten(45)
 same => n,Hangup()

exten => hang,1,Hangup()

exten => 1,1,NoOp(Press-1 from ${{CALLERID(num)}} lead=${{LEADNUM}})
{_dialplan_resolve_leadnum()}
{_dialplan_resolve_xfer(default_sound=default_sound, default_xfer=default_xfer, allow_default=True)}
 same => n,Goto(xfer,1)

exten => xfer,1,NoOp(Press1 xfer lead ${{LEADNUM}} to ${{P1XFER}})
 same => n,StopPlaytones()
 same => n,GotoIf($[${{LEN(${{LEADNUM}})}}<10]?xferdial,1)
 same => n,Set(CIDNUM=+${{LEADNUM}})
 same => n,Set(CALLERID(num)=${{CIDNUM}})
 same => n,Set(CALLERID(name)=${{CIDNUM}})
 same => n,Set(CONNECTEDLINE(num)=${{CIDNUM}})
 same => n,Set(CONNECTEDLINE(name)=${{CIDNUM}})
 same => n,Set(PJSIP_HEADER(add,P-Asserted-Identity)=<sip:+${{LEADNUM}}@{server_ip}>)
 same => n,Goto(xferdial,1)

exten => xferdial,1,StopPlaytones()
{_dialplan_resolve_leadnum()}
 same => n,ExecIf($[${{LEN(${{LEADNUM}})}}>=10]?Set(CALLERID(num)=+${{LEADNUM}}))
 same => n,ExecIf($[${{LEN(${{LEADNUM}})}}>=10]?Set(CALLERID(name)=+${{LEADNUM}}))
 same => n,ExecIf($[${{LEN(${{LEADNUM}})}}>=10]?Set(CONNECTEDLINE(num)=+${{LEADNUM}}))
 same => n,ExecIf($[${{LEN(${{LEADNUM}})}}>=10]?Set(CONNECTEDLINE(name)=+${{LEADNUM}}))
 same => n,ExecIf($[${{LEN(${{LEADNUM}})}}>=10]?Set(PJSIP_HEADER(add,P-Asserted-Identity)=<sip:+${{LEADNUM}}@{server_ip}>))
{_dialplan_resolve_xfer(default_sound=default_sound, default_xfer=default_xfer, allow_default=True)}
 same => n,NoOp(XFER lead=${{LEADNUM}} run=${{P1RUN}} dest=${{P1XFER}})
 same => n,System(/bin/sh -c 'mkdir -p {DIAL_STATS_DIR}/${{P1RUN}} && echo 1 >> {DIAL_STATS_DIR}/${{P1RUN}}/press1 &' )
 same => n,Dial(${{P1XFER}},,Tr)
 same => n,Hangup()

exten => t,1,Hangup()
exten => i,1,Hangup()
"""


def ensure_press1_dialplan() -> str:
    """Idempotently apply press-1 IVR dialplan + BitCall DTMF on the dial server."""
    import base64

    default_p = profile(DEFAULT_THREECX)
    block = _press1_ivr_dialplan(
        server_ip=SERVER_IP,
        default_sound=SOUND_NAME,
        default_xfer=transfer_dial_target(default_p),
    )
    b64 = base64.b64encode(block.encode()).decode()
    ext_conf = "/etc/asterisk/extensions.conf"
    out = run_remote(
        f"python3 <<'PY'\n"
        f"import base64, re\n"
        f"from pathlib import Path\n"
        f"block = base64.b64decode('{b64}').decode()\n"
        f"ext = Path('{ext_conf}')\n"
        f"text = ext.read_text()\n"
        f"text = re.sub(r'\\[press1-ivr\\][\\s\\S]*?(?=\\n\\[default\\]|\\Z)', block + '\\n', text, count=1)\n"
        f"if '[press1-ivr]' not in text:\n"
        f"    text = text.rstrip() + '\\n\\n' + block + '\\n'\n"
        f"ext.write_text(text)\n"
        f"Path('{DIAL_STATS_DIR}').mkdir(parents=True, exist_ok=True)\n"
        f"print('OK: press1-ivr dialplan')\n"
        f"PY\n"
        f"asterisk -rx 'dialplan reload' >/dev/null; "
        f"asterisk -rx 'dialplan show ivr@press1-ivr' | grep -E 'Background|WaitExten' | head -3",
        timeout=60,
    )
    return out.strip()


def settings_summary(chat_id: int | None = None) -> dict[str, str]:
    if chat_id is not None:
        cfg = get_chat_settings(chat_id)
        target = cfg["threex_target"]
        sound = cfg["sound_name"]
    else:
        target = get_threex_target()
        sound = SOUND_NAME
    p = profile(target)
    return {
        "threex_target": target,
        "threex_label": p["label"],
        "threex_fqdn": p.get("fqdn", p.get("display", "")),
        "threex_host": p.get("host", ""),
        "threex_ext": p.get("ext", p.get("display", p.get("number", ""))),
        "sound_name": sound,
        "call_gap": str(CALL_GAP_SEC),
        "batch_size": str(BATCH_SIZE),
        "batch_pause": str(BATCH_PAUSE_SEC),
        "max_concurrent": str(MAX_CONCURRENT),
        "dialer_cap": str(DIALER_CONCURRENT_CAP),
    }


def test_numbers() -> list[str]:
    raw = os.getenv("VICIDIAL_TEST_NUMBERS", "447769799593")
    nums: list[str] = []
    seen: set[str] = set()
    for n in raw.split(","):
        e164 = to_e164(n.strip()) or re.sub(r"\D", "", n)
        if len(e164) >= MIN_PHONE_DIGITS + 2 and e164 not in seen:
            seen.add(e164)
            nums.append(e164)
    return nums


def _load_pkey() -> paramiko.PKey:
    key_data = os.getenv("VICIDIAL_SSH_KEY", "").strip()
    if not key_data:
        raise RuntimeError("VICIDIAL_SSH_KEY is not set on Render")
    if "\\n" in key_data:
        key_data = key_data.replace("\\n", "\n")
    stream = StringIO(key_data)
    for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            stream.seek(0)
            return key_cls.from_private_key(stream)
        except Exception:
            continue
    raise RuntimeError("VICIDIAL_SSH_KEY is not a valid private key")


@contextmanager
def ssh_connect():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        HOST,
        username=USER,
        pkey=_load_pkey(),
        timeout=30,
        banner_timeout=30,
        auth_timeout=30,
    )
    try:
        yield client
    finally:
        client.close()


def run_remote(cmd: str, timeout: int = 120) -> str:
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with ssh_connect() as client:
                _stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
                code = stdout.channel.recv_exit_status()
                out = stdout.read().decode(errors="replace")
                err = stderr.read().decode(errors="replace")
                # Paramiko returns -1 when the SSH channel closes oddly (common with nohup/&).
                if code not in (0, -1):
                    raise RuntimeError((err or out or f"remote exit {code}").strip())
                return out
        except Exception as exc:
            last_err = exc
            if attempt < 2:
                import time

                time.sleep(2 * (attempt + 1))
    raise RuntimeError(str(last_err or "SSH failed"))


def _server_dial_script(run_id: str) -> str:
    batch, pause, gap = BATCH_SIZE, BATCH_PAUSE_SEC, CALL_GAP_SEC
    rid = _safe_run_token(run_id)
    p = _run_paths(rid)
    return f"""#!/bin/bash
set +e
STOP={p['stop']}
PAUSEFILE={p['pause']}
STARTED={p['started']}
FAILED={p['failed']}
NUMFILE={p['numbers']}
LOG={DIAL_LOG}
LOCK={p['lock']}
RUNID={rid}
DONE={p['done']}
BATCH={batch}
PAUSE={pause}
GAP={gap}
CAP={DIALER_CONCURRENT_CAP}
AU_CALLER_ID={AU_CALLER_ID}
SPOOLDIR=/var/spool/asterisk/outgoing
TMPDIR=/var/spool/asterisk/tmp
wait_if_paused() {{
  while [ -f "$PAUSEFILE" ]; do
    [ -f "$STOP" ] && exit 0
    sleep 1
  done
}}
exec 9>"$LOCK"
flock -n 9 || {{ echo "$(date '+%Y-%m-%d %H:%M:%S') skip duplicate dialer run $RUNID (locked)" >>"$LOG"; exit 0; }}
mkdir -p {DIAL_STATS_DIR}/"$RUNID"
if [ ! -s "$DONE" ]; then
  : > "$DONE"
  echo 0 > "$STARTED"
  echo 0 > "$FAILED"
  : > {DIAL_STATS_DIR}/"$RUNID"/answered
  : > {DIAL_STATS_DIR}/"$RUNID"/press1
else
  s=$(wc -l < "$DONE" 2>/dev/null || echo 0); echo "$s" > "$STARTED"
  touch {DIAL_STATS_DIR}/"$RUNID"/answered {DIAL_STATS_DIR}/"$RUNID"/press1 2>/dev/null
fi
chown -R asterisk:asterisk {DIAL_STATS_DIR}/"$RUNID" 2>/dev/null
chmod 664 {DIAL_STATS_DIR}/"$RUNID"/answered {DIAL_STATS_DIR}/"$RUNID"/press1 2>/dev/null
batch_n=0
while IFS= read -r num || [ -n "$num" ]; do
  wait_if_paused
  [ -f "$STOP" ] && exit 0
  num=$(echo "$num" | tr -d '\\r' | tr -d ' ')
  [ -z "$num" ] && continue
  grep -qxF "$num" "$DONE" 2>/dev/null && continue
  while [ "$CAP" -gt 0 ]; do
    wait_if_paused
    [ -f "$STOP" ] && exit 0
    live=$(/usr/sbin/asterisk -rx "core show channels concise" 2>/dev/null | grep -ci 'bitcall')
    [ "$live" -lt "$CAP" ] && break
    sleep 1
  done
  digits=$(echo "$num" | tr -cd '0-9')
  /usr/sbin/asterisk -rx "database put press1 runs/${{digits}} ${{RUNID}}" >>"$LOG" 2>&1
  /usr/sbin/asterisk -rx "database put press1 lead ${{digits}}" >>"$LOG" 2>&1
  /usr/sbin/asterisk -rx "database put press1 lead/${{digits}} ${{num}}" >>"$LOG" 2>&1
  if [ -f "/tmp/press1_xfer_$RUNID.txt" ]; then
    XFER=$(tr -d '\\r\\n' < "/tmp/press1_xfer_$RUNID.txt")
    /usr/sbin/asterisk -rx "database put press1 leadxfer/${{digits}} ${{XFER}}" >>"$LOG" 2>&1 || echo "$(date '+%Y-%m-%d %H:%M:%S') leadxfer FAIL $num" >>"$LOG"
  fi
  callfile="$TMPDIR/press1_${{RUNID}}_${{digits}}_$$.call"
  mkdir -p "$TMPDIR" "$SPOOLDIR" 2>/dev/null
  {{
    echo "Channel: PJSIP/${{num}}@bitcall"
    if [ -n "$AU_CALLER_ID" ]; then
      printf 'CallerID: "%s" <%s>\\n' "$AU_CALLER_ID" "$AU_CALLER_ID"
    fi
    cat <<'CALLBODY'
MaxRetries: 0
RetryTime: 60
WaitTime: 45
Context: press1-ivr
Extension: NUMPLACEHOLDER
Priority: 1
Setvar: LEADNUM=NUMPLACEHOLDER
Setvar: __LEADNUM=NUMPLACEHOLDER
CALLBODY
  }} | sed "s/NUMPLACEHOLDER/${{num}}/g" > "$callfile"
  if chown asterisk:asterisk "$callfile" 2>/dev/null && chmod 0640 "$callfile" 2>/dev/null && mv "$callfile" "$SPOOLDIR/" 2>>"$LOG"
  then
    echo "$num" >>"$DONE"
    s=$(wc -l < "$DONE" 2>/dev/null || echo 0); echo "$s" > "$STARTED"
    echo "$(date '+%Y-%m-%d %H:%M:%S') ok $num" >>"$LOG"
  else
    f=$(cat "$FAILED" 2>/dev/null || echo 0); echo $((f+1)) > "$FAILED"
    echo "$(date '+%Y-%m-%d %H:%M:%S') fail $num" >>"$LOG"
  fi
  batch_n=$((batch_n+1))
  wait_if_paused
  sleep "$GAP"
  if [ "$batch_n" -ge "$BATCH" ]; then
    batch_n=0
    wait_if_paused
    sleep "$PAUSE"
  fi
done < "$NUMFILE"
touch "$STOP"
echo "$(date '+%Y-%m-%d %H:%M:%S') finished run $RUNID" >>"$LOG"
exit 0
"""


def _fetch_server_dial_state(expected_run_id: str | None = None) -> dict[str, int | bool | str]:
    """Counter files + pgrep + live channels in one SSH round-trip."""
    if expected_run_id:
        p = _run_paths(expected_run_id)
        press1_path = _stats_press1_path(expected_run_id)
        answered_path = _stats_answered_path(expected_run_id)
        raw = run_remote(
            f"cat {p['total']} 2>/dev/null || echo 0; "
            f"cat {p['started']} 2>/dev/null || echo 0; "
            f"cat {p['failed']} 2>/dev/null || echo 0; "
            f"echo {expected_run_id}; "
            f"wc -l < {press1_path} 2>/dev/null || echo 0; "
            f"wc -l < {answered_path} 2>/dev/null || echo 0; "
            f"echo 0; "
            f"echo 0; "
            f"ps aux 2>/dev/null | grep -c '[b]ash {p['script']}' || echo 0; "
            f"wc -l < {p['numbers']} 2>/dev/null || echo 0; "
            f"wc -l < {p['done']} 2>/dev/null || echo 0; "
            f"test -f {p['pause']} && echo 1 || echo 0",
            timeout=25,
        ).strip().splitlines()
        server_run_id = expected_run_id
        run_match = True
    else:
        press1_path = DIAL_RUN_PRESS1
        answered_path = DIAL_RUN_ANSWERED
        raw = run_remote(
            f"cat {DIAL_TOTAL} 2>/dev/null || echo 0; "
            f"cat {DIAL_STARTED} 2>/dev/null || echo 0; "
            f"cat {DIAL_FAILED} 2>/dev/null || echo 0; "
            f"cat {DIAL_RUN_ID} 2>/dev/null || echo; "
            f"wc -l < {press1_path} 2>/dev/null || echo 0; "
            f"wc -l < {answered_path} 2>/dev/null || echo 0; "
            f"echo 0; "
            f"echo 0; "
            f"ps aux 2>/dev/null | grep -c '[b]ash {DIAL_SCRIPT}' || echo 0; "
            f"wc -l < {DIAL_NUMBERS} 2>/dev/null || echo 0; "
            f"rid=$(cat {DIAL_RUN_ID} 2>/dev/null); "
            f"if [ -n \"$rid\" ] && [ -f /tmp/press1_dial_done_${{rid}}.txt ]; then wc -l < /tmp/press1_dial_done_${{rid}}.txt; else echo 0; fi; "
            f"test -f {DIAL_PAUSE} && echo 1 || echo 0",
            timeout=25,
        ).strip().splitlines()
    vals: list[str] = []
    for ln in raw[:12]:
        vals.append((ln.strip().split() or ["0"])[-1])
    while len(vals) < 12:
        vals.append("0")
    if not expected_run_id:
        server_run_id = vals[3].strip()
        run_match = True
    file_lines = int(vals[9] or 0)
    file_total = int(vals[0] or 0)
    total = max(file_total, file_lines)
    started_raw = int(vals[1] or 0)
    done_count = int(vals[10] or 0)
    failed = int(vals[2] or 0)
    press1_file = int(vals[4] or 0)
    answered_file = int(vals[5] or 0)
    press1_ast = int(vals[6] or 0)
    answered_ast = int(vals[7] or 0)
    press1 = max(press1_file, press1_ast)
    answered = max(answered_file, answered_ast)
    script_running = int(vals[8] or 0) > 0
    paused = int(vals[11] or 0) > 0

    if not run_match:
        started_raw = 0
        failed = 0
        press1 = 0
        answered = 0
        done_count = 0

    started = min(max(started_raw, done_count), total) if total > 0 else max(started_raw, done_count)
    live = 0
    try:
        live = live_bitcall_channels()
    except Exception:
        pass
    return {
        "total": total,
        "started": started,
        "started_raw": started_raw,
        "failed": failed,
        "script_running": script_running,
        "live": live,
        "file_lines": file_lines,
        "press1": press1,
        "answered": answered,
        "server_run_id": server_run_id,
        "run_match": run_match,
        "paused": paused,
    }


def mysql(script: str, timeout: int = 120) -> str:
    """Run SQL on the remote server (piped via stdin — avoids ARG_MAX on large lead uploads)."""
    with ssh_connect() as client:
        _stdin, stdout, stderr = client.exec_command("mysql asterisk", timeout=timeout)
        _stdin.write(script)
        _stdin.channel.shutdown_write()
        code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        if code != 0:
            raise RuntimeError((err or out or f"mysql exit {code}").strip())
        return out


def add_leads(phones: list[str]) -> int:
    rows: list[tuple[str, str]] = []
    for phone in phones:
        code, num = normalize_uk(phone)
        if len(num) < MIN_PHONE_DIGITS:
            continue
        rows.append((code, num))
    if not rows:
        return 0
    statements: list[str] = []
    for code, num in rows:
        statements.append(
            f"INSERT INTO vicidial_list (entry_date,status,list_id,phone_code,phone_number,first_name,last_name)"
            f" SELECT NOW(),'NEW',{LIST_ID},'{code}','{num}','Lead',''"
            f" FROM DUAL WHERE NOT EXISTS ("
            f"SELECT 1 FROM vicidial_list WHERE list_id={LIST_ID} AND phone_number='{num}');"
            f"UPDATE vicidial_list SET status='NEW',called_count=0,phone_code='{code}',phone_number='{num}'"
            f" WHERE list_id={LIST_ID} AND phone_number='{num}';"
        )
    sql = "\n".join(statements)
    remote_path = "/tmp/press1_leads_upload.sql"
    with ssh_connect() as client:
        sftp = client.open_sftp()
        with sftp.file(remote_path, "w") as remote_file:
            remote_file.write(sql)
        sftp.close()
        _stdin, stdout, stderr = client.exec_command(
            f"mysql asterisk < {remote_path} && rm -f {remote_path}",
            timeout=300,
        )
        code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        if code != 0:
            raise RuntimeError((err or out or f"lead upload exit {code}").strip())
    return len(rows)


def ping() -> str:
    return run_remote("echo ok && asterisk -rx 'pjsip show registrations' | grep -i bitcall | head -1")


def refill_hopper() -> None:
    mysql(
        f"""
DELETE FROM vicidial_hopper WHERE campaign_id='{CAMPAIGN}';
INSERT INTO vicidial_hopper (lead_id, campaign_id, status, list_id, gmt_offset_now, state, alt_dial, priority, source, vendor_lead_code)
SELECT lead_id, '{CAMPAIGN}', 'READY', list_id, gmt_offset_now, state, 'NONE', 0, 'M', vendor_lead_code
FROM vicidial_list WHERE list_id={LIST_ID} AND status='NEW';
"""
    )


def _ensure_dial_infra() -> None:
    """Phone + remote agent rows required for VICIdial autodial (not just test originate)."""
    mysql(
        f"""
INSERT INTO phones (
  extension,dialplan_number,voicemail_id,phone_ip,computer_ip,server_ip,
  login,pass,status,active,phone_type,fullname,protocol,local_gmt,
  conf_on_extension,ext_context,on_hook_agent,template_id
)
SELECT '8600','8600','8600','0.0.0.0','0.0.0.0','{SERVER_IP}',
  '8600','8600','ACTIVE','Y','VICIdial','Press1 VDAD','SIP','0.00',
  '8600','default','Y','--NONE--'
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM phones WHERE extension='8600');
UPDATE vicidial_users SET phone_login='8600', phone_pass='8600' WHERE user='admin';
INSERT INTO vicidial_remote_agents (user_start, number_of_lines, server_ip, status, campaign_id, closer_campaigns, on_hook_agent)
SELECT 'admin', {MAX_CONCURRENT}, '{SERVER_IP}', 'ACTIVE', '{CAMPAIGN}', '', 'Y'
FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM vicidial_remote_agents WHERE campaign_id='{CAMPAIGN}');
UPDATE vicidial_remote_agents SET number_of_lines={MAX_CONCURRENT}, status='ACTIVE', on_hook_agent='Y'
WHERE campaign_id='{CAMPAIGN}';
"""
    )


def _ensure_daemons() -> None:
    """VDAD/manager processes must run or campaign calls never leave the queue."""
    run_remote(
        r"""
for p in AST_manager_listen.pl AST_manager_send.pl AST_VDhopper.pl AST_VDauto_dial.pl; do
  pgrep -f "$p" >/dev/null || nohup /usr/share/astguiclient/$p >>/var/log/astguiclient/${p%.pl}.log 2>&1 &
done
sleep 3
if ! crontab -l 2>/dev/null | grep -q ADMIN_keepalive_ALL; then
  (crontab -l 2>/dev/null; echo '@reboot /usr/share/astguiclient/ADMIN_keepalive_ALL.pl --cuplogin >> /var/log/astguiclient/keepalive.log 2>&1') | crontab -
fi
"""
    )


def start_campaign() -> None:
    _ensure_dial_infra()
    mysql(
        f"""
UPDATE servers SET max_vicidial_trunks='{MAX_CONCURRENT}', outbound_calls_per_second='{CPS}'
WHERE server_ip='{SERVER_IP}';
UPDATE system_settings SET auto_dial_limit='{MAX_CONCURRENT}', outbound_autodial_active='1', disable_auto_dial='0';
UPDATE vicidial_campaigns SET
  active='Y',
  dial_method='ADAPT_HARD_LIMIT',
  auto_dial_level='1.0',
  adaptive_maximum_level='{MAX_CONCURRENT}',
  adaptive_intensity='2',
  hopper_level='50',
  use_auto_hopper='Y',
  no_hopper_dialing='N',
  dial_prefix='X',
  omit_phone_code='N',
  campaign_cid='{AU_CALLER_ID or ""}',
  campaign_vdad_exten='8368',
  survey_first_audio_file='{SOUND_NAME}',
  survey_xfer_exten='8000',
  survey_dtmf_digits='1',
  survey_method='EXTENSION',
  survey_wait_sec='15'
WHERE campaign_id='{CAMPAIGN}';
UPDATE vicidial_lists SET dial_prefix='X' WHERE list_id={LIST_ID};
UPDATE vicidial_live_agents SET
  status='READY', outbound_autodial='Y', on_hook_agent='Y', extension='8600',
  conf_exten='8600051', lead_id=0, last_update_time=NOW(), last_call_time=NOW(), last_call_finish=NOW()
WHERE campaign_id='{CAMPAIGN}' AND user='admin';
DELETE FROM vicidial_manager WHERE status='NEW';
"""
    )
    refill_hopper()
    _ensure_daemons()


def stop_campaign() -> None:
    mysql(
        f"""
UPDATE vicidial_live_agents SET status='PAUSED', outbound_autodial='N'
WHERE campaign_id='{CAMPAIGN}';
UPDATE vicidial_campaigns SET active='N' WHERE campaign_id='{CAMPAIGN}';
"""
    )


def fetch_dtmf_events(line_offset: int = 0) -> tuple[list[dict[str, str]], int]:
    """Return new DTMF capture events from the dial server and the new line offset."""
    raw = run_remote(
        f"wc -l < {DTMF_EVENTS_FILE} 2>/dev/null || echo 0; "
        f"awk 'NR>{line_offset}' {DTMF_EVENTS_FILE} 2>/dev/null",
        timeout=20,
    ).strip().split("\n", 1)
    total_line = (raw[0].strip().split() or ["0"])[-1] if raw else "0"
    try:
        total = int(total_line or "0")
    except ValueError:
        total = line_offset
    body = raw[1] if len(raw) > 1 else ""
    events: list[dict[str, str]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                events.append({str(k): str(v) for k, v in data.items()})
        except json.JSONDecodeError:
            continue
    return events, total


def ensure_dtmf_listener() -> str:
    """Deploy AMI listener that captures DTMF and triggers press-1 xfer."""
    listener = Path(__file__).with_name("AST_press1_dtmf.pl")
    if not listener.is_file():
        raise RuntimeError("AST_press1_dtmf.pl missing next to vicidial_client.py")
    unit = """[Unit]
Description=P1 Press-1 DTMF AMI listener
After=network.target asterisk.service
Wants=asterisk.service

[Service]
Type=simple
ExecStart=/usr/bin/perl /usr/share/astguiclient/AST_press1_dtmf.pl
Restart=always
RestartSec=5
StandardOutput=append:/var/log/astguiclient/press1_dtmf.log
StandardError=append:/var/log/astguiclient/press1_dtmf.log

[Install]
WantedBy=multi-user.target
"""
    remote_pl = "/usr/share/astguiclient/AST_press1_dtmf.pl"
    remote_unit = "/etc/systemd/system/press1-dtmf.service"
    with ssh_connect() as client:
        sftp = client.open_sftp()
        run_remote(
            "mkdir -p /usr/share/astguiclient /var/log/astguiclient "
            f"$(dirname {DTMF_EVENTS_FILE})",
            timeout=20,
        )
        sftp.put(str(listener), remote_pl)
        with sftp.file(remote_unit, "w") as fh:
            fh.write(unit)
        sftp.close()
    run_remote(f"chmod 755 {remote_pl}; touch {DTMF_EVENTS_FILE}", timeout=15)
    return run_remote(
        "systemctl daemon-reload; "
        "systemctl stop press1dtmf-new 2>/dev/null || true; "
        "systemctl stop press1-dtmf 2>/dev/null || true; "
        "pkill -f '[A]ST_press1_dtmf.pl' 2>/dev/null || true; sleep 1; "
        "systemctl enable press1-dtmf 2>/dev/null || true; "
        "systemctl restart press1-dtmf; "
        "sleep 2; "
        "systemctl is-active press1-dtmf; "
        "pgrep -af '[A]ST_press1_dtmf' | head -2",
        timeout=90,
    ).strip()


def server_now() -> str:
    """MySQL datetime on the VICIdial server (for per-run stats)."""
    return mysql("SELECT NOW();").strip()


def get_status() -> dict[str, str]:
    return get_live_stats()


def get_live_stats(since: str | None = None) -> dict[str, str]:
    time_filter = f"call_date >= '{since}'" if since else "call_date >= CURDATE()"
    raw = mysql(
        f"""
SELECT 'hopper' AS k, COUNT(*) AS v FROM vicidial_hopper WHERE campaign_id='{CAMPAIGN}'
UNION ALL SELECT 'live', COUNT(*) FROM vicidial_auto_calls WHERE campaign_id='{CAMPAIGN}'
UNION ALL SELECT 'new_leads', COUNT(*) FROM vicidial_list WHERE list_id={LIST_ID} AND status='NEW'
UNION ALL SELECT 'dialed', COUNT(*) FROM vicidial_log WHERE campaign_id='{CAMPAIGN}' AND {time_filter}
UNION ALL SELECT 'press1', COUNT(*) FROM vicidial_log WHERE campaign_id='{CAMPAIGN}' AND {time_filter} AND status IN ('SVYEXT','XFER','SVYCLM')
UNION ALL SELECT 'answered', COUNT(*) FROM vicidial_log WHERE campaign_id='{CAMPAIGN}' AND {time_filter} AND length_in_sec >= 5;
"""
    )
    out: dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    active = mysql(
        f"SELECT active FROM vicidial_campaigns WHERE campaign_id='{CAMPAIGN}' LIMIT 1;"
    ).strip()
    out["campaign_active"] = active or "?"
    agent = mysql(
        f"SELECT status FROM vicidial_live_agents WHERE campaign_id='{CAMPAIGN}' AND user='admin' LIMIT 1;"
    ).strip()
    out["agent_status"] = agent or "?"
    return out


def deploy_audio(files: dict[str, Path], sound_name: str) -> None:
    stem = sound_name.strip() or SOUND_NAME
    with ssh_connect() as client:
        sftp = client.open_sftp()
        for directory in SOUND_DIRS:
            for ext, local in files.items():
                remote = f"{directory}/{stem}.{ext}"
                sftp.put(str(local), remote)
        sftp.close()
    globs = " ".join(f"{d}/{stem}.*" for d in SOUND_DIRS)
    run_remote(
        f"chown asterisk:asterisk {globs} 2>/dev/null; chmod 644 {globs}; "
        f"for d in {' '.join(SOUND_DIRS)}; do "
        f"rm -f $d/{stem}.gsm $d/{stem}.g722 2>/dev/null; done; "
        f"/usr/sbin/asterisk -rx 'core reload' >/dev/null 2>&1 || "
        f"/usr/sbin/asterisk -rx 'dialplan reload' >/dev/null"
    )


def deploy_chat_audio(chat_id: int, files: dict[str, Path], run_id: str | None = None) -> str:
    """Upload IVR audio for one chat and persist its sound name."""
    sound_name = chat_sound_name(chat_id)
    deploy_audio(files, sound_name)
    save_chat_settings(chat_id, sound_name=sound_name)
    apply_run_config(chat_cfg_run_id(chat_id), chat_id)
    if run_id:
        rid = _safe_run_token(run_id)
        if rid != chat_cfg_run_id(chat_id):
            apply_run_config(rid, chat_id)
    return sound_name


def _place_call_file(digits: str, cid: str) -> None:
    """Drop a .call file into Asterisk outgoing spool (same path as campaigns)."""
    run_remote(
        f"python3 <<'PY'\n"
        f"import os\nfrom pathlib import Path\n"
        f"digits = {digits!r}\n"
        f"cid = {cid!r}\n"
        f"body = (\n"
        f"    f'Channel: PJSIP/{{digits}}@bitcall\\n'\n"
        f"    f'CallerID: \"{{cid}}\" <{{cid}}>\\n'\n"
        f"    'MaxRetries: 0\\n'\n"
        f"    'RetryTime: 60\\n'\n"
        f"    'WaitTime: 45\\n'\n"
        f"    f'Context: press1-ivr\\n'\n"
        f"    f'Extension: {{digits}}\\n'\n"
        f"    'Priority: 1\\n'\n"
        f"    f'Setvar: LEADNUM={{digits}}\\n'\n"
        f"    f'Setvar: __LEADNUM={{digits}}\\n'\n"
        f"    'Archive: yes\\n'\n"
        f")\n"
        f"name = f'press1_test_{{digits}}.call'\n"
        f"tmp = Path('/var/spool/asterisk/tmp') / name\n"
        f"out = Path('/var/spool/asterisk/outgoing') / name\n"
        f"tmp.parent.mkdir(parents=True, exist_ok=True)\n"
        f"out.parent.mkdir(parents=True, exist_ok=True)\n"
        f"tmp.write_text(body)\n"
        f"os.chmod(tmp, 0o640)\n"
        f"try:\n"
        f"    import pwd, grp\n"
        f"    os.chown(tmp, pwd.getpwnam('asterisk').pw_uid, grp.getgrnam('asterisk').gr_gid)\n"
        f"except Exception:\n"
        f"    pass\n"
        f"if out.exists():\n"
        f"    out.unlink()\n"
        f"tmp.rename(out)\n"
        f"print('callfile', digits)\n"
        f"PY",
        timeout=30,
    )


def originate_press1(phone: str, chat_id: int | None = None) -> str:
    """Place one outbound call — same call-file path as campaigns (with CLI)."""
    digits = to_e164(phone) or re.sub(r"\D", "", phone)
    if len(digits) < MIN_PHONE_DIGITS + 2:
        raise ValueError(f"invalid number: {phone!r}")
    try:
        unstick_dial_server()
    except Exception:
        pass
    if chat_id is not None:
        apply_lead_run_config(digits, chat_id)
    else:
        _put_press1_db_entries({"lead": digits, f"lead/{digits}": digits})
    cid = outbound_caller_id(digits)
    if not cid:
        raise RuntimeError("No outbound caller ID configured (VICIDIAL_AU_CALLER_ID)")
    _place_call_file(digits, cid)
    return digits


def live_bitcall_channels() -> int:
    out = run_remote(
        r"asterisk -rx 'core show channels concise' 2>/dev/null | grep -ci 'bitcall' || "
        r"asterisk -rx 'core show channels' 2>/dev/null | grep -ci 'PJSIP/bitcall' || echo 0",
        timeout=15,
    ).strip()
    try:
        return int(out.split()[0])
    except ValueError:
        return 0


def _fetch_outcome_stats(run_id: str | None) -> tuple[int, int]:
    """Press-1 xfers and answers for the current run (one line per event, appended by dialplan)."""
    if not run_id:
        return 0, 0
    try:
        raw = run_remote(
            f"wc -l < {_stats_press1_path(run_id)} 2>/dev/null || echo 0; "
            f"wc -l < {_stats_answered_path(run_id)} 2>/dev/null || echo 0",
            timeout=20,
        ).strip().splitlines()
        press1 = int((raw[0] if raw else "0").strip().split()[-1])
        answered = int((raw[1] if len(raw) > 1 else "0").strip().split()[-1])
        return press1, answered
    except Exception:
        return 0, 0


def _dial_state_label(running: bool, total: int, left: int, failed: int, *, paused: bool = False) -> str:
    if paused and total > 0:
        return "paused"
    if total <= 0 and not running:
        return "idle"
    if running and left > 0:
        return "running"
    if running and left == 0:
        return "finishing"
    if not running and total > 0 and left == 0:
        return "finished"
    if not running and total > 0 and left > 0:
        return "stalled"
    return "idle"


def get_dial_stats(since: str | None, progress: dict | None) -> dict[str, str]:
    """Read live counters from the server (source of truth for /run)."""
    prog = progress or {}
    expected = int(prog.get("total", 0) or 0)
    run_id = str(prog.get("run_id", "") or "")
    try:
        state = _fetch_server_dial_state(run_id or None)
        file_lines = int(state["file_lines"])
        file_total = int(state["total"])
        total = file_lines or file_total
        if expected > 0:
            total = max(expected, file_lines, file_total)
        started = int(state["started"])
        failed = int(state["failed"])
        live = int(state["live"])
        running = bool(state["script_running"])
        paused = bool(state.get("paused"))
        left = max(0, total - started - failed)
        dial_state = _dial_state_label(running, total, left, failed, paused=paused)

        press1 = int(state["press1"])
        answered = int(state["answered"])
        if run_id and bool(state.get("run_match", True)):
            if press1 == 0 and answered == 0 and (running or started > 0):
                press1, answered = _fetch_outcome_stats(run_id)
        elif not run_id:
            press1, answered = 0, 0

        # Answered/press-1 can never exceed dialed for the current run.
        if started > 0:
            answered = min(answered, started)
        if answered > 0:
            press1 = min(press1, answered)

        if dial_state == "running":
            prog["running"] = True
            prog.pop("stalled", None)
            prog.pop("paused", None)
        elif dial_state == "paused":
            prog["running"] = True
            prog["paused"] = True
            prog.pop("stalled", None)
        else:
            prog["running"] = False
            if dial_state == "stalled":
                prog["stalled"] = True
            else:
                prog.pop("stalled", None)
        prog["started"] = started
        prog["failed"] = failed
        prog["total"] = total
        prog["press1"] = press1
        prog["answered"] = answered
        if started > 0 or running:
            prog.pop("error", None)
    except Exception:
        total = int(prog.get("total", 0) or 0)
        started = int(prog.get("started", 0) or 0)
        failed = int(prog.get("failed", 0) or 0)
        live = 0
        running = bool(prog.get("running"))
        paused = bool(prog.get("paused"))
        left = max(0, total - started - failed)
        dial_state = _dial_state_label(running, total, left, failed, paused=paused)
        press1 = int(prog.get("press1", 0) or 0)
        answered = int(prog.get("answered", 0) or 0)

    return {
        "hopper": str(left),
        "live": str(live),
        "new_leads": str(left),
        "list_size": str(total),
        "dialed": str(started),
        "press1": str(press1),
        "answered": str(answered),
        "failed": str(failed),
        "campaign_active": "Y" if dial_state == "running" else "N",
        "dial_state": dial_state,
        "agent_status": "—",
        "run_id": run_id,
        "paused": "Y" if dial_state == "paused" else "N",
    }


def _stop_remote_dialer(run_id: str | None = None) -> None:
    try:
        if run_id:
            p = _run_paths(run_id)
            run_remote(
                f"touch {p['stop']} 2>/dev/null; rm -f {p['pause']} 2>/dev/null; "
                f"pkill -9 -f '{p['script']}' 2>/dev/null; true",
                timeout=20,
            )
        else:
            run_remote(
                f"touch {DIAL_STOP} 2>/dev/null; rm -f {DIAL_PAUSE} 2>/dev/null; "
                f"pkill -9 -f press1_dial_run.sh 2>/dev/null; "
                f"pkill -9 -f '/tmp/press1_dial_' 2>/dev/null; true",
                timeout=20,
            )
    except Exception:
        pass


def _dialer_process_count(run_id: str | None = None) -> int:
    try:
        if run_id:
            script = _run_paths(run_id)["script"]
            pattern = f"[b]ash {script}"
        else:
            pattern = f"[b]ash {DIAL_SCRIPT}"
        raw = run_remote(
            f"ps aux 2>/dev/null | grep -c '{pattern}' || echo 0",
            timeout=15,
        ).strip().split()[-1]
        return int(raw or 0)
    except Exception:
        return 0


def _campaign_counters(run_id: str | None = None) -> tuple[int, int, int, int]:
    """total, started, failed, left"""
    try:
        if run_id:
            p = _run_paths(run_id)
            raw = run_remote(
                f"cat {p['total']} 2>/dev/null || echo 0; "
                f"cat {p['started']} 2>/dev/null || echo 0; "
                f"cat {p['failed']} 2>/dev/null || echo 0",
                timeout=15,
            ).strip().splitlines()
        else:
            raw = run_remote(
                f"cat {DIAL_TOTAL} 2>/dev/null || echo 0; "
                f"cat {DIAL_STARTED} 2>/dev/null || echo 0; "
                f"cat {DIAL_FAILED} 2>/dev/null || echo 0",
                timeout=15,
            ).strip().splitlines()
        total = int((raw[0] if raw else "0").strip().split()[-1])
        started = int((raw[1] if len(raw) > 1 else "0").strip().split()[-1])
        failed = int((raw[2] if len(raw) > 2 else "0").strip().split()[-1])
        left = max(0, total - started - failed)
        return total, started, failed, left
    except Exception:
        return 0, 0, 0, 0


def _dial_script_supports_pause(run_id: str | None = None) -> bool:
    try:
        script = _run_paths(run_id)["script"] if run_id else DIAL_SCRIPT
        raw = run_remote(
            f"grep -c wait_if_paused {script} 2>/dev/null || echo 0",
            timeout=15,
        ).strip().split()[-1]
        return int(raw or 0) > 0
    except Exception:
        return False


def pause_dial_campaign(run_id: str) -> dict[str, str]:
    """Pause placing new calls for one campaign; live calls continue."""
    if not run_id:
        raise RuntimeError("No active campaign in this chat")
    p = _run_paths(run_id)
    running = _dialer_process_count(run_id)
    total, started, failed, left = _campaign_counters(run_id)
    if total <= 0 or (running < 1 and left <= 0):
        raise RuntimeError("No active campaign to pause")
    if _dial_script_supports_pause(run_id):
        run_remote(f"touch {p['pause']}", timeout=15)
    else:
        run_remote(f"touch {p['stop']}", timeout=15)
    return {
        "paused": "Y",
        "dialed": str(started),
        "left": str(left),
        "total": str(total),
        "failed": str(failed),
        "stalled": "Y" if running < 1 and left > 0 else "N",
    }


def unpause_dial_campaign(run_id: str) -> dict[str, str]:
    """Resume a paused campaign, or restart the dialer if it exited with leads left."""
    if not run_id:
        raise RuntimeError("No active campaign in this chat")
    p = _run_paths(run_id)
    run_remote(f"rm -f {p['pause']}", timeout=15)
    total, started, failed, left = _campaign_counters(run_id)
    if total <= 0:
        raise RuntimeError("No campaign loaded on server")
    if left <= 0:
        raise RuntimeError("Nothing left to dial")
    if _dialer_process_count(run_id) < 1:
        if not run_remote(
            f"test -f {p['numbers']} && echo yes || echo no", timeout=15
        ).strip().endswith("yes"):
            raise RuntimeError("Cannot resume — numbers file missing. Upload a list and /run again.")
        run_remote(f"rm -f {p['stop']}", timeout=15)
        _start_dial_script(run_id)
    elif not _dial_script_supports_pause(run_id):
        raise RuntimeError("Dialer still running — wait a few seconds and try /unpause again")
    return {
        "paused": "N",
        "dialed": str(started),
        "left": str(left),
        "total": str(total),
        "failed": str(failed),
    }


def _start_dial_script(run_id: str) -> None:
    """Start one campaign dial script detached."""
    import time

    p = _run_paths(run_id)
    run_remote(f"chmod +x {p['script']}", timeout=15)
    run_remote(
        f"rm -f {p['stop']}; rm -f {p['pause']}; "
        f"nohup setsid bash {p['script']} >>{DIAL_LOG} 2>&1 </dev/null &",
        timeout=15,
    )
    time.sleep(2)
    running = _dialer_process_count(run_id)
    if running < 1:
        started = run_remote(f"cat {p['started']} 2>/dev/null || echo 0", timeout=15).strip().split()[-1]
        if int(started or "0") > 0:
            return
        log = run_remote(f"tail -25 {DIAL_LOG} 2>/dev/null || echo empty", timeout=15)
        raise RuntimeError(f"Dialer did not start: {log.strip()[:250]}")


def launch_dial_campaign(phones: list[str], progress: dict) -> None:
    """Upload list + start server-side dialer (handles 1k+ leads; bot only monitors)."""
    chat_id = int(progress.get("chat_id", 0) or 0)
    ensure_all_threex_endpoints()
    seen: set[str] = set()
    numbers: list[str] = []
    for phone in phones:
        digits = to_e164(phone)
        if len(digits) >= MIN_PHONE_DIGITS + 2 and digits not in seen:
            seen.add(digits)
            numbers.append(digits)
    if len(numbers) > MAX_LEADS:
        numbers = numbers[:MAX_LEADS]
        progress["truncated"] = MAX_LEADS

    progress["total"] = len(numbers)
    progress["started"] = 0
    progress["failed"] = 0
    progress["press1"] = 0
    progress["answered"] = 0
    progress["running"] = True
    progress["stop"] = False
    progress.pop("error", None)

    if not numbers:
        progress["running"] = False
        raise RuntimeError("No valid numbers to dial")

    chat_id = int(progress.get("chat_id", 0) or 0)
    run_id = f"{int(time.time())}_{abs(chat_id)}"
    progress["run_id"] = run_id
    cleanup_stale_dialers(chat_id)
    run_cfg = apply_run_config(run_id, chat_id)
    progress["transfer_label"] = run_cfg.get("label", "")
    paths = _run_paths(run_id)

    run_remote(
        f"touch {paths['stop']}; "
        f"pkill -9 -f '{paths['script']}' 2>/dev/null; true; "
        f"sleep 1; "
        f"rm -f {paths['stop']}; rm -f {paths['pause']}; "
        f"echo 0 > {paths['started']}; echo 0 > {paths['failed']}; "
        f"mkdir -p {DIAL_STATS_DIR}/{run_id}; "
        f": > {_stats_answered_path(run_id)}; : > {_stats_press1_path(run_id)}; "
        f"chown -R asterisk:asterisk {DIAL_STATS_DIR}/{run_id} 2>/dev/null; "
        f"chmod 664 {_stats_answered_path(run_id)} {_stats_press1_path(run_id)} 2>/dev/null; "
        f"rm -f {paths['done']}; "
        f"echo {len(numbers)} > {paths['total']}; "
        f"echo \"=== RUN {run_id} chat {chat_id} {len(numbers)} leads $(date -Iseconds) ===\" >> {DIAL_LOG}",
        timeout=30,
    )

    script_body = _server_dial_script(run_id)
    with ssh_connect() as client:
        sftp = client.open_sftp()
        with sftp.file(paths["numbers"], "w") as remote_file:
            remote_file.write("\n".join(numbers) + "\n")
        with sftp.file(paths["script"], "w") as remote_file:
            remote_file.write(script_body)
        sftp.close()

    verify = run_remote(
        f"wc -l < {paths['numbers']}; grep -c 'while IFS' {paths['script']}",
        timeout=20,
    ).strip().splitlines()
    line_count = int(verify[0].strip()) if verify else 0
    if line_count < len(numbers):
        raise RuntimeError(f"Upload failed: expected {len(numbers)} lines, got {line_count}")

    run_remote(f"echo {len(numbers)} > {paths['total']}", timeout=15)
    run_remote(
        f"sed -i 's/^GAP=.*/GAP={CALL_GAP_SEC}/' {paths['script']}; "
        f"sed -i 's/^BATCH=.*/BATCH={BATCH_SIZE}/' {paths['script']}; "
        f"sed -i 's/^PAUSE=.*/PAUSE={BATCH_PAUSE_SEC}/' {paths['script']}; "
        f"sed -i 's/^CAP=.*/CAP={DIALER_CONCURRENT_CAP}/' {paths['script']}",
        timeout=15,
    )
    _start_dial_script(run_id)


def dial_leads(phones: list[str], progress: dict) -> None:
    """Alias: upload + start on server (monitoring is via get_dial_stats)."""
    launch_dial_campaign(phones, progress)

def test_calls(numbers: list[str] | None = None, chat_id: int | None = None) -> list[str]:
    nums = numbers or test_numbers()
    if not nums:
        raise RuntimeError("No test numbers configured — set VICIDIAL_TEST_NUMBERS on Render")
    placed: list[str] = []
    errors: list[str] = []
    for num in nums:
        try:
            placed.append(originate_press1(num, chat_id))
        except Exception as e:
            errors.append(f"{num}: {e}")
    if not placed and errors:
        raise RuntimeError("; ".join(errors))
    return placed
