"""Temporary Telegram access grants (owner-managed)."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

import press1_ui as ui
import vicidial_client as vd

_BUILTIN_OWNERS: set[int] = {8780653370, 8316388420}

OWNERS: set[int] = _BUILTIN_OWNERS | {
    int(x.strip())
    for x in os.getenv("TELEGRAM_ALLOWED_IDS", os.getenv("ADMIN_CHAT_ID", "")).split(",")
    if x.strip().isdigit()
}

_CACHE_TTL_SEC = 30
_cache: dict[str, object] = {"at": 0.0, "grant_ids": set(), "grants": []}


def _now() -> float:
    return time.time()


def _empty() -> dict:
    return {"grants": [], "users": {}}


def load_access_data() -> dict:
    try:
        raw = vd.run_remote(f"cat {vd.ACCESS_PATH} 2>/dev/null", timeout=15).strip()
        if not raw:
            return _empty()
        data = json.loads(raw)
        data.setdefault("grants", [])
        data.setdefault("users", {})
        return data
    except Exception:
        return _empty()


def save_access_data(data: dict) -> None:
    payload = json.dumps(data, indent=2)
    try:
        vd.run_remote(
            f"mkdir -p $(dirname {vd.ACCESS_PATH}); "
            f"cat > {vd.ACCESS_PATH} <<'EOF'\n{payload}\nEOF\n"
            f"chmod 644 {vd.ACCESS_PATH}",
            timeout=20,
        )
    except Exception as e:
        # Never break Telegram commands if dial-server SSH is briefly unavailable.
        print(f"[press1] access save skipped: {e}")
        return
    _cache["at"] = 0.0


def remember_user(user_id: int, username: str | None, full_name: str | None = None) -> None:
    if not user_id:
        return
    try:
        data = load_access_data()
        users = data.setdefault("users", {})
        key = str(user_id)
        entry = users.get(key, {})
        if username:
            entry["username"] = username.lstrip("@").lower()
        if full_name:
            entry["name"] = full_name
        entry["user_id"] = user_id
        users[key] = entry
        save_access_data(data)
    except Exception as e:
        print(f"[press1] remember_user skipped: {e}")


def _prune_expired(data: dict) -> bool:
    now = _now()
    grants = data.get("grants", [])
    kept = [g for g in grants if float(g.get("expires_at", 0)) > now]
    if len(kept) != len(grants):
        data["grants"] = kept
        return True
    return False


def _refresh_cache() -> None:
    data = load_access_data()
    if _prune_expired(data):
        save_access_data(data)
    now = _now()
    active: list[dict] = []
    ids: set[int] = set()
    for g in data.get("grants", []):
        uid = int(g.get("user_id", 0) or 0)
        if uid and float(g.get("expires_at", 0)) > now:
            active.append(g)
            ids.add(uid)
    _cache["at"] = now
    _cache["grant_ids"] = ids
    _cache["grants"] = active


def active_grants() -> list[dict]:
    if _now() - float(_cache.get("at", 0)) > _CACHE_TTL_SEC:
        _refresh_cache()
    return list(_cache.get("grants", []))


def allowed_user_ids() -> set[int]:
    if not OWNERS:
        return set()  # open mode handled in bot
    ids = set(OWNERS)
    if _now() - float(_cache.get("at", 0)) > _CACHE_TTL_SEC:
        _refresh_cache()
    ids |= set(_cache.get("grant_ids", set()))
    return ids


def is_owner(user_id: int) -> bool:
    return bool(OWNERS) and user_id in OWNERS


def is_allowed(user_id: int) -> bool:
    if not OWNERS:
        return True
    if user_id in OWNERS:
        return True
    if _now() - float(_cache.get("at", 0)) > _CACHE_TTL_SEC:
        try:
            _refresh_cache()
        except Exception:
            pass
    return user_id in set(_cache.get("grant_ids", set()))


def parse_duration(text: str) -> int:
    """Parse 30m, 24h, 7d, 1w into seconds."""
    m = re.fullmatch(r"(\d+)\s*(m|h|d|w)", (text or "").strip().lower())
    if not m:
        raise ValueError("Duration must look like 30m, 24h, 7d, or 1w")
    n = int(m.group(1))
    unit = m.group(2)
    mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    seconds = n * mult
    if seconds < 300:
        raise ValueError("Minimum duration is 5m")
    if seconds > 86400 * 90:
        raise ValueError("Maximum duration is 90d")
    return seconds


def resolve_user_id(
    target: str,
    data: dict | None = None,
    extra_users: dict | None = None,
) -> tuple[int, str]:
    """Resolve numeric id or @username to Telegram user id."""
    target = (target or "").strip()
    if not target:
        raise ValueError("User required")
    if target.isdigit():
        return int(target), target
    username = target.lstrip("@").lower()
    if not username:
        raise ValueError("User required")
    if extra_users:
        for uid, info in extra_users.items():
            if str(info.get("username", "")).lower() == username:
                return int(uid), f"@{username}"
    data = data or load_access_data()
    for uid, info in (data.get("users") or {}).items():
        if str(info.get("username", "")).lower() == username:
            return int(uid), f"@{username}"
    raise ValueError(
        f"Cannot find @{username}. They must message this bot once (/start), "
        "or use their numeric Telegram user id."
    )


def add_grant(
    *,
    target: str,
    duration_text: str,
    granted_by: int,
    granter_name: str | None = None,
    extra_users: dict | None = None,
) -> dict:
    if not is_owner(granted_by):
        raise PermissionError("Only the owner can add access keys")
    seconds = parse_duration(duration_text)
    data = load_access_data()
    user_id, label = resolve_user_id(target, data, extra_users)
    if user_id in OWNERS:
        raise ValueError("That user is already a permanent owner")
    now = _now()
    expires_at = now + seconds
    grants = [g for g in data.get("grants", []) if int(g.get("user_id", 0)) != user_id]
    grants.append(
        {
            "user_id": user_id,
            "label": label,
            "granted_by": granted_by,
            "granted_by_name": granter_name or str(granted_by),
            "granted_at": int(now),
            "expires_at": int(expires_at),
            "duration": duration_text,
        }
    )
    data["grants"] = grants
    save_access_data(data)
    return grants[-1]


def revoke_grant(*, target: str, revoked_by: int, extra_users: dict | None = None) -> str:
    if not is_owner(revoked_by):
        raise PermissionError("Only the owner can revoke access keys")
    data = load_access_data()
    user_id, label = resolve_user_id(target, data, extra_users)
    before = len(data.get("grants", []))
    data["grants"] = [g for g in data.get("grants", []) if int(g.get("user_id", 0)) != user_id]
    if len(data["grants"]) == before:
        raise ValueError(f"No active key for {label}")
    save_access_data(data)
    return label


def format_grant_line(g: dict) -> str:
    uid = g.get("user_id")
    label = g.get("label") or str(uid)
    exp = datetime.fromtimestamp(float(g.get("expires_at", 0)), tz=timezone.utc)
    dur = g.get("duration", "?")
    return (
        f"{ui.BULLET} {ui.b(label)} ({ui.code(uid)})\n"
        f"   ⏳ {ui.esc(dur)} · until {ui.esc(exp.strftime('%Y-%m-%d %H:%M UTC'))}"
    )


def format_grant_list() -> str:
    grants = active_grants()
    if not grants:
        return ui.card("🔑  ACCESS KEYS", [ui.note("⚪", "No active access keys.")])
    return ui.card("🔑  ACCESS KEYS", [format_grant_line(g) for g in grants])
