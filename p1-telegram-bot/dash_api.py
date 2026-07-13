"""Web dashboard REST API — dolphinshop.cc P1 command center."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from functools import wraps
from typing import Any, Callable

from flask import Blueprint, jsonify, request

import press1_settings as ps
import vicidial_client as vd

DASH_API_SECRET = os.getenv("DASH_API_SECRET", "").strip()
DASH_KEYS = {
    k.strip().upper()
    for k in os.getenv("DASH_SUBSCRIPTION_KEYS", "").split(",")
    if k.strip()
}

_progress: dict[int, dict[str, Any]] = {}
_lock = threading.Lock()
_dtmf_offsets: dict[int, int] = {}

bp = Blueprint("dash_api", __name__)


def tenant_chat_id(subscription_key: str) -> int:
    digest = hashlib.sha256(subscription_key.strip().upper().encode()).hexdigest()
    return -int(digest[:10], 16)


def _auth_ok() -> tuple[int, str] | tuple[None, None]:
    auth = request.headers.get("Authorization", "")
    secret = auth[7:].strip() if auth.startswith("Bearer ") else ""
    sub_key = request.headers.get("X-Subscription-Key", "").strip().upper()
    if DASH_API_SECRET and secret != DASH_API_SECRET:
        return None, None
    if not sub_key:
        return None, None
    if DASH_KEYS and sub_key not in DASH_KEYS:
        return None, None
    return tenant_chat_id(sub_key), sub_key


def auth_required(fn: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        tenant, _key = _auth_ok()
        if tenant is None:
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        request.dash_tenant = tenant
        return fn(*args, **kwargs)

    return wrapper


def _progress_for(tenant: int) -> dict[str, Any]:
    with _lock:
        if tenant not in _progress:
            _progress[tenant] = {
                "running": False,
                "started": 0,
                "answered": 0,
                "press1": 0,
                "failed": 0,
                "live": 0,
                "total": 0,
                "chat_id": tenant,
                "run_since": "",
                "run_id": "",
            }
        return _progress[tenant]


def _campaign_view(st: dict[str, str], prog: dict[str, Any], tenant: int | None = None) -> dict[str, Any]:
    dial_state = str(st.get("dial_state") or ("running" if prog.get("running") else "idle"))
    running = dial_state in ("running", "paused", "stalled")
    started = int(st.get("dialed") or prog.get("started") or 0)
    answered = int(st.get("answered") or prog.get("answered") or 0)
    press1 = int(st.get("press1") or prog.get("press1") or 0)
    total = int(st.get("list_size") or prog.get("total") or 0)
    failed = int(st.get("failed") or prog.get("failed") or 0)
    live = int(st.get("live") or 0)
    hopper = int(st.get("hopper") or max(0, total - started - failed))

    transfer_label = str(prog.get("transfer_label") or "")
    if not transfer_label and tenant is not None:
        try:
            settings = vd.get_chat_settings(tenant)
            transfer_label = ps.transfer_display(ps.profile(settings["threex_target"]))
        except Exception:
            pass

    conv = round(press1 / started * 100, 1) if started > 0 else 0.0
    ans_rate = round(answered / started * 100, 1) if started > 0 else 0.0
    pct_done = round(started / total * 100, 1) if total > 0 else 0.0

    return {
        "running": running,
        "paused": dial_state == "paused",
        "started": started,
        "answered": answered,
        "press1": press1,
        "live": live,
        "failed": failed,
        "total": total,
        "hopper": hopper,
        "run_id": str(st.get("run_id") or prog.get("run_id") or ""),
        "dial_state": dial_state,
        "transfer_label": transfer_label,
        "error": prog.get("error"),
        "conversion_pct": conv,
        "answer_rate": ans_rate,
        "pct_done": pct_done,
    }


def _apply_transfer(tenant: int, body: dict[str, Any]) -> dict[str, str] | None:
    raw = str(body.get("threex_target") or body.get("transfer_to") or "").strip().lower()
    if not raw:
        return None
    ps.profile(raw)
    return vd.save_chat_settings(tenant, threex_target=raw)


@bp.get("/api/stats")
@auth_required
def api_stats():
    tenant = request.dash_tenant
    prog = _progress_for(tenant)
    since = prog.get("run_since") or None
    try:
        st = vd.get_dial_stats(since, prog)
    except Exception as exc:
        return jsonify({"ok": True, "campaign": _campaign_view({}, prog, tenant), "warning": str(exc)})
    return jsonify({"ok": True, "campaign": _campaign_view(st, prog, tenant)})


@bp.get("/api/profiles")
@auth_required
def api_profiles():
    items = []
    for pid, meta in ps.THREECX_PROFILES.items():
        items.append(
            {
                "id": pid,
                "label": meta.get("label", pid),
                "display": ps.transfer_display(meta),
            }
        )
    tenant = request.dash_tenant
    current = vd.get_chat_settings(tenant).get("threex_target", ps.DEFAULT_THREECX)
    return jsonify({"ok": True, "profiles": items, "current": current})


@bp.get("/api/health")
@auth_required
def api_health():
    try:
        ping = vd.ping()
        live = vd.live_bitcall_channels()
        return jsonify(
            {
                "ok": True,
                "dial_server": ping,
                "live_channels": live,
                "gap_sec": vd.CALL_GAP_SEC,
                "batch_size": vd.BATCH_SIZE,
                "dialer_cap": vd.DIALER_CONCURRENT_CAP,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503


@bp.get("/api/settings")
@auth_required
def api_get_settings():
    tenant = request.dash_tenant
    summary = vd.settings_summary(tenant)
    cfg = vd.get_chat_settings(tenant)
    tn = str(cfg.get("test_number") or "").strip()
    if not tn:
        owner = vd._owner_test_digits()
        if owner:
            tn = owner
    return jsonify(
        {
            "ok": True,
            "settings": {
                **summary,
                "test_number": tn,
                "test_number_display": f"+{tn}" if tn else "",
                "threex_target": cfg.get("threex_target", summary.get("threex_target", "")),
                "transfer_label": ps.transfer_display(ps.profile(cfg["threex_target"])),
            },
        }
    )


@bp.post("/api/settings")
@auth_required
def api_settings():
    body = request.get_json(force=True, silent=True) or {}
    tenant = request.dash_tenant
    try:
        saved = _apply_transfer(tenant, body)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if "test_number" in body:
        raw = str(body.get("test_number") or "").strip()
        digits = __import__("re").sub(r"\D", "", raw)
        if raw and len(digits) < vd.MIN_PHONE_DIGITS + 2:
            return jsonify({"ok": False, "error": "Invalid test number"}), 400
        vd.save_chat_settings(tenant, test_number=digits)
    settings = vd.get_chat_settings(tenant)
    profile = ps.profile(settings["threex_target"])
    summary = vd.settings_summary(tenant)
    return jsonify(
        {
            "ok": True,
            "settings": {
                **summary,
                "threex_target": settings["threex_target"],
                "transfer_label": ps.transfer_display(profile),
                "sound_name": settings["sound_name"],
                "test_number": settings.get("test_number", ""),
            },
            "saved": bool(saved) or "test_number" in body,
        }
    )


@bp.post("/api/campaign/start")
@auth_required
def api_start():
    body = request.get_json(force=True, silent=True) or {}
    numbers = body.get("numbers") or []
    if not numbers:
        return jsonify({"ok": False, "error": "No numbers provided"}), 400

    tenant = request.dash_tenant
    prog = _progress_for(tenant)

    try:
        st = vd.get_dial_stats(prog.get("run_since"), prog)
        if st.get("dial_state") in ("running", "paused"):
            return jsonify({"ok": False, "error": "Campaign already running"}), 409
    except Exception:
        pass

    try:
        _apply_transfer(tenant, body)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    try:
        run_since = vd.server_now()
    except Exception:
        run_since = ""

    prog.update(
        {
            "started": 0,
            "dialed": 0,
            "failed": 0,
            "press1": 0,
            "answered": 0,
            "live": 0,
            "total": len(numbers),
            "running": True,
            "stop": False,
            "chat_id": tenant,
            "run_id": "",
            "run_since": run_since,
            "error": None,
        }
    )

    def _run() -> None:
        try:
            vd.launch_dial_campaign(numbers, prog)
        except Exception as exc:
            prog["error"] = str(exc)
            prog["running"] = False

    threading.Thread(target=_run, daemon=True, name=f"dash-run-{tenant}").start()
    time.sleep(0.8)
    try:
        st = vd.get_dial_stats(run_since, prog)
    except Exception:
        st = {}
    return jsonify({"ok": True, "campaign": _campaign_view(st, prog, tenant)})


@bp.post("/api/campaign/stop")
@auth_required
def api_stop():
    tenant = request.dash_tenant
    prog = _progress_for(tenant)
    run_id = str(prog.get("run_id") or "")
    if not run_id:
        try:
            run_id = vd.resolve_chat_run_id(tenant) or ""
        except Exception:
            run_id = ""
    try:
        vd._stop_remote_dialer(run_id or None)
    except Exception:
        pass
    prog["running"] = False
    prog["stop"] = True
    return jsonify({"ok": True, "campaign": _campaign_view({}, prog, tenant)})


@bp.post("/api/campaign/pause")
@auth_required
def api_pause():
    tenant = request.dash_tenant
    prog = _progress_for(tenant)
    run_id = str(prog.get("run_id") or "") or vd.resolve_chat_run_id(tenant)
    if not run_id:
        return jsonify({"ok": False, "error": "No active campaign"}), 404
    st = vd.pause_dial_campaign(run_id)
    prog["paused"] = True
    prog["running"] = True
    return jsonify({"ok": True, "status": st, "campaign": _campaign_view(st, prog, tenant)})


@bp.post("/api/campaign/unpause")
@auth_required
def api_unpause():
    tenant = request.dash_tenant
    prog = _progress_for(tenant)
    run_id = str(prog.get("run_id") or "") or vd.resolve_chat_run_id(tenant)
    if not run_id:
        return jsonify({"ok": False, "error": "No active campaign"}), 404
    st = vd.unpause_dial_campaign(run_id)
    prog["paused"] = False
    prog["running"] = True
    return jsonify({"ok": True, "status": st, "campaign": _campaign_view(st, prog, tenant)})


@bp.get("/api/dtmf")
@auth_required
def api_dtmf():
    tenant = request.dash_tenant
    offset = _dtmf_offsets.get(tenant, 0)
    try:
        events, new_offset = vd.fetch_dtmf_events(offset)
        _dtmf_offsets[tenant] = new_offset
        return jsonify({"ok": True, "events": events, "offset": new_offset})
    except Exception as exc:
        return jsonify({"ok": True, "events": [], "warning": str(exc)})


@bp.post("/api/testcall")
@auth_required
def api_testcall():
    tenant = request.dash_tenant
    body = request.get_json(force=True, silent=True) or {}
    number = str(body.get("number") or "").strip()
    numbers = body.get("numbers") or []
    try:
        from press1_utils import to_e164
        import re

        def _norm(n: str) -> str:
            d = to_e164(n) or re.sub(r"\D", "", n)
            return d if len(d) >= vd.MIN_PHONE_DIGITS + 2 else ""

        if numbers:
            norm = [_norm(str(n)) for n in numbers]
            norm = [n for n in norm if n]
            if not norm:
                return jsonify({"ok": False, "error": "Invalid test number(s)"}), 400
            placed = vd.test_calls(norm, chat_id=tenant)
        elif number:
            digits = _norm(number)
            if not digits:
                return jsonify({"ok": False, "error": "Invalid test number"}), 400
            vd.save_chat_settings(tenant, test_number=digits)
            placed = vd.test_calls([digits], chat_id=tenant)
        else:
            placed = vd.test_calls(chat_id=tenant)
        return jsonify({"ok": True, "placed": placed})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@bp.post("/api/schedule")
@auth_required
def api_schedule():
    import press1_schedule as sched
    from datetime import datetime

    body = request.get_json(force=True, silent=True) or {}
    numbers = body.get("numbers") or []
    run_at_raw = str(body.get("run_at") or "").strip()
    if not numbers:
        return jsonify({"ok": False, "error": "No numbers loaded"}), 400
    if not run_at_raw:
        return jsonify({"ok": False, "error": "run_at required (ISO datetime)"}), 400
    tenant = request.dash_tenant
    try:
        run_at = datetime.fromisoformat(run_at_raw.replace("Z", "+00:00"))
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=sched.TZ)
        entry = sched.add_schedule(user_id=tenant, chat_id=tenant, numbers=numbers, run_at=run_at)
        data = sched.load_schedules()
        for item in data.get("schedules", []):
            if item.get("id") == entry.get("id"):
                item["source"] = "dashboard"
                break
        sched.save_schedules(data)
        return jsonify({"ok": True, "schedule": entry})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@bp.get("/api/schedules")
@auth_required
def api_schedules():
    import press1_schedule as sched

    tenant = request.dash_tenant
    items = [s for s in sched.list_schedules(tenant) if int(s.get("chat_id", 0)) == tenant]
    return jsonify({"ok": True, "schedules": items})


@bp.post("/api/schedule/cancel")
@auth_required
def api_schedule_cancel():
    import press1_schedule as sched

    body = request.get_json(force=True, silent=True) or {}
    sid = str(body.get("id") or "").strip()
    if not sid:
        return jsonify({"ok": False, "error": "Schedule id required"}), 400
    tenant = request.dash_tenant
    try:
        sched.remove_schedule(sid, tenant)
        return jsonify({"ok": True, "id": sid})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@bp.post("/api/audio")
@auth_required
def api_audio():
    from pathlib import Path
    import tempfile

    from press1_utils import convert_audio_for_asterisk

    tenant = request.dash_tenant
    upload = request.files.get("audio")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "error": "No audio file"}), 400
    name = upload.filename.lower()
    if not any(name.endswith(ext) for ext in (".mp3", ".wav", ".m4a", ".ogg", ".opus", ".sln", ".mpeg", ".mpga")):
        return jsonify({"ok": False, "error": "Unsupported format — use MP3, WAV, M4A, or OGG"}), 400
    try:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / upload.filename
            upload.save(dest)
            sound_name = vd.chat_sound_name(tenant)
            files = convert_audio_for_asterisk(dest, Path(tmp), sound_name)
            deployed = vd.deploy_chat_audio(tenant, files, None)
        return jsonify({"ok": True, "sound_name": deployed})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "ffmpeg not available on dialer — contact support"}), 503
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc) or "Audio processing failed"}), 400


@bp.post("/api/repair")
@auth_required
def api_repair():
    try:
        result = vd.repair_press1_server()
        return jsonify({"ok": True, "result": result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


def launch_dashboard_campaign(tenant: int, numbers: list[str]) -> dict[str, Any]:
    """Used by scheduled dashboard campaigns."""
    prog = _progress_for(tenant)
    try:
        st = vd.get_dial_stats(prog.get("run_since"), prog)
        if st.get("dial_state") in ("running", "paused"):
            raise RuntimeError("Campaign already running")
    except RuntimeError:
        raise
    except Exception:
        pass
    try:
        run_since = vd.server_now()
    except Exception:
        run_since = ""
    prog.update(
        {
            "started": 0,
            "dialed": 0,
            "failed": 0,
            "press1": 0,
            "answered": 0,
            "live": 0,
            "total": len(numbers),
            "running": True,
            "stop": False,
            "chat_id": tenant,
            "run_id": "",
            "run_since": run_since,
            "error": None,
        }
    )

    def _run() -> None:
        try:
            vd.launch_dial_campaign(numbers, prog)
        except Exception as exc:
            prog["error"] = str(exc)
            prog["running"] = False

    threading.Thread(target=_run, daemon=True, name=f"dash-sched-{tenant}").start()
    return prog


def register_dash_routes(app) -> None:
    app.register_blueprint(bp)
