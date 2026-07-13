#!/usr/bin/env python3
"""One-shot: sync P1 secrets to Render p1-bot and redeploy."""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env.press1"
SSH_FILE = ROOT / "p1-telegram-bot" / "RENDER_SSH_KEY_ONE_LINE.txt"
BITCALL_FILE = ROOT / "p1-telegram-bot" / ".bitcall_pw"
P1_SERVICE_ID = "srv-d993f69o3t8c73eq4sog"
DASH_SECRET = "dolphin-p1-x7k9m2q4w8"
DASH_KEYS = "DS-DEMO-2026-KEY1,DS-ADMIN-2026-R8K4N2"


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def api(key: str, method: str, url: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None


def main() -> int:
    api_key = os.environ.get("RENDER_API_KEY", "").strip()
    if not api_key:
        print("RENDER_API_KEY required", file=sys.stderr)
        return 1

    local = load_env(ENV_FILE)
    token = local.get("BOT_TOKEN") or local.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("BOT_TOKEN missing in .env.press1", file=sys.stderr)
        return 1

    ssh = ""
    if SSH_FILE.is_file():
        ssh = SSH_FILE.read_text(encoding="utf-8", errors="replace").strip()
        if "\\n" not in ssh:
            ssh = ssh.replace("\r\n", "\\n").replace("\n", "\\n")

    bitcall_pw = ""
    if BITCALL_FILE.is_file():
        bitcall_pw = BITCALL_FILE.read_text(encoding="utf-8", errors="replace").strip()

    svc = api(api_key, "GET", f"https://api.render.com/v1/services/{P1_SERVICE_ID}")
    slug = svc.get("slug", P1_SERVICE_ID)
    base = f"https://{slug}.onrender.com"
    print(f"Service: {slug} ({P1_SERVICE_ID})")

    env_vars = [
        {"key": "CLOUD_DEPLOYED", "value": "true"},
        {"key": "BOT_TOKEN", "value": token},
        {"key": "WEBHOOK_HOST", "value": "0.0.0.0"},
        {"key": "TELEGRAM_WEBHOOK_URL", "value": f"{base}/telegram/webhook"},
        {"key": "VICIDIAL_SSH_HOST", "value": local.get("VICIDIAL_SSH_HOST", "206.189.118.204")},
        {"key": "VICIDIAL_SSH_USER", "value": local.get("VICIDIAL_SSH_USER", "root")},
        {"key": "VICIDIAL_CAMPAIGN", "value": local.get("VICIDIAL_CAMPAIGN", "press1")},
        {"key": "VICIDIAL_LIST_ID", "value": local.get("VICIDIAL_LIST_ID", "101")},
        {"key": "VICIDIAL_SOUND_NAME", "value": local.get("VICIDIAL_SOUND_NAME", "press1_alice")},
        {"key": "VICIDIAL_SERVER_IP", "value": local.get("VICIDIAL_SERVER_IP", "206.189.118.204")},
        {"key": "VICIDIAL_MAX_CONCURRENT", "value": local.get("VICIDIAL_MAX_CONCURRENT", "0")},
        {"key": "VICIDIAL_DIALER_CAP", "value": "0"},
        {"key": "VICIDIAL_BATCH_SIZE", "value": "100"},
        {"key": "VICIDIAL_CALL_GAP_SEC", "value": "0.1"},
        {"key": "VICIDIAL_CPS", "value": local.get("VICIDIAL_CPS", "10")},
        {"key": "VICIDIAL_CALLER_ID", "value": "442038968062"},
        {"key": "VICIDIAL_TEST_NUMBERS", "value": local.get("VICIDIAL_TEST_NUMBERS", "447769799593")},
        {"key": "PRESS1_OWNER_TEST_NUMBER", "value": "447769799593"},
        {"key": "BITCALL_SIP_USER", "value": "f-features896"},
        {"key": "BITCALL_SIP_REALM", "value": "gateway.bitcall.io"},
        {"key": "DASH_API_SECRET", "value": DASH_SECRET},
        {"key": "DASH_SUBSCRIPTION_KEYS", "value": DASH_KEYS},
    ]
    if local.get("TELEGRAM_ALLOWED_IDS"):
        env_vars.append({"key": "TELEGRAM_ALLOWED_IDS", "value": local["TELEGRAM_ALLOWED_IDS"]})
    if ssh:
        env_vars.append({"key": "VICIDIAL_SSH_KEY", "value": ssh})
    if bitcall_pw:
        env_vars.append({"key": "BITCALL_SIP_PASSWORD", "value": bitcall_pw})

    print("Syncing env vars ...")
    api(api_key, "PUT", f"https://api.render.com/v1/services/{P1_SERVICE_ID}/env-vars", env_vars)

    patch = {
        "serviceDetails": {
            "healthCheckPath": "/health",
            "envSpecificDetails": {
                "dockerfilePath": "Dockerfile",
                "dockerContext": "p1-telegram-bot",
                "dockerCommand": "",
            },
        }
    }
    api(api_key, "PATCH", f"https://api.render.com/v1/services/{P1_SERVICE_ID}", patch)
    print("Patched dockerContext=p1-telegram-bot")

    print("Triggering deploy ...")
    dep = api(api_key, "POST", f"https://api.render.com/v1/services/{P1_SERVICE_ID}/deploys", {"clearCache": "do_not_clear"})
    dep_obj = dep.get("deploy", dep) if isinstance(dep, dict) else {}
    dep_id = dep_obj.get("id", "")
    print(f"Deploy id: {dep_id}")

    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(15)
        if dep_id:
            d = api(api_key, "GET", f"https://api.render.com/v1/services/{P1_SERVICE_ID}/deploys/{dep_id}")
            st = (d.get("deploy") or d).get("status", "?")
        else:
            st = "?"
        print(f"  status: {st}")
        if st in ("live", "build_failed", "update_failed", "canceled", "deactivated"):
            break

    try:
        with urllib.request.urlopen(f"{base}/health", timeout=45) as r:
            health = json.loads(r.read().decode())
        print("Health:", json.dumps(health))
        if health.get("ok"):
            print(f"READY: {base}")
            return 0
    except Exception as e:
        print(f"Health check: {e}")

    print(f"Deploy finished with status {st}. Check {base}/health")
    return 0 if st == "live" else 1


if __name__ == "__main__":
    raise SystemExit(main())
