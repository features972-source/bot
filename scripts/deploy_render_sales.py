#!/usr/bin/env python3
"""Create or update @dolphinsiptrunkbot (Sales) on Render — separate from p1-bot."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
ENV_FILE = Path(os.getenv("SALES_ENV_FILE", REPO_ROOT / ".env.sales"))
SERVICE_NAME = os.getenv("SALES_RENDER_SERVICE", "sales-bot")
REPO = "https://github.com/features972-source/bot"
BRANCH = "main"
ROOT_DIR = "sales-telegram-bot"
P1_SERVICE_ID = "srv-d993f69o3t8c73eq4sog"


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
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


def list_services(key: str) -> list[dict]:
    rows = api(key, "GET", "https://api.render.com/v1/services?limit=100")
    return [row.get("service", row) for row in rows if row.get("service", row).get("id")]


def find_service(services: list[dict]) -> dict | None:
    needles = {SERVICE_NAME.lower(), "sales-bot", "dolphinsiptrunk-bot", "telegram-reminder-bot"}
    for svc in services:
        slug = str(svc.get("slug", "")).lower()
        name = str(svc.get("name", "")).lower()
        if slug in needles or name in needles:
            return svc
        if "sales" in slug or "reminder" in slug or "siptrunk" in slug:
            return svc
    return None


def resolve_api_key() -> str:
    key = os.environ.get("RENDER_API_KEY", "").strip()
    if key:
        return key
    for path in (
        REPO_ROOT / ".env.render",
        Path(r"C:\Users\User\Desktop\telegram-reminder-bot\.env.render"),
    ):
        key = load_env(path).get("RENDER_API_KEY", "").strip()
        if key:
            return key
    raise SystemExit("RENDER_API_KEY not set")


def resolve_bot_env() -> dict[str, str]:
    for path in (
        ENV_FILE,
        Path(r"C:\Users\User\Desktop\telegram-reminder-bot\.env"),
    ):
        data = load_env(path)
        if data.get("TELEGRAM_BOT_TOKEN") or data.get("BOT_TOKEN"):
            return data
    raise SystemExit("TELEGRAM_BOT_TOKEN missing (.env.sales or telegram-reminder-bot/.env)")


def build_env_vars(local: dict[str, str]) -> list[dict]:
    token = local.get("TELEGRAM_BOT_TOKEN") or local.get("BOT_TOKEN", "")
    vars_ = [
        {"key": "PYTHON_VERSION", "value": "3.12.7"},
        {"key": "TELEGRAM_BOT_TOKEN", "value": token},
        {"key": "DB_PATH", "value": "/data/reminders.db"},
        {"key": "TZ", "value": "Europe/London"},
    ]
    allowed = local.get("ALLOWED_USER_IDS", "").strip()
    if allowed:
        vars_.append({"key": "ALLOWED_USER_IDS", "value": allowed})
    return vars_


def wait_health(base: str, minutes: int = 20) -> dict | None:
    url = f"{base.rstrip('/')}/health"
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=45) as r:
                data = json.loads(r.read().decode())
            if data.get("ok"):
                return data
        except Exception as exc:
            print(f"  health: {exc}")
        time.sleep(15)
    return None


def verify_bot(token: str) -> dict:
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=30) as r:
        data = json.loads(r.read().decode())
    if not data.get("ok"):
        raise SystemExit("Telegram getMe failed")
    return data["result"]


def main() -> int:
    api_key = resolve_api_key()
    local = resolve_bot_env()
    env_vars = build_env_vars(local)
    token = local.get("TELEGRAM_BOT_TOKEN") or local.get("BOT_TOKEN", "")

    me = verify_bot(token)
    print(f"Bot: @{me.get('username')} ({me.get('first_name')})")

    services = list_services(api_key)
    svc = find_service(services)
    p1 = next((s for s in services if s.get("id") == P1_SERVICE_ID), services[0])

    owner_id = p1["ownerId"]
    region = p1.get("serviceDetails", {}).get("region", "frankfurt")

    if not svc:
        print(f"Creating service {SERVICE_NAME} in {region} ...")
        body = {
            "type": "web_service",
            "name": SERVICE_NAME,
            "ownerId": owner_id,
            "repo": REPO,
            "branch": BRANCH,
            "autoDeploy": "yes",
            "envVars": env_vars,
            "serviceDetails": {
                "runtime": "python",
                "plan": "starter",
                "region": region,
                "healthCheckPath": "/health",
                "rootDir": ROOT_DIR,
                "envSpecificDetails": {
                    "buildCommand": "pip install -r requirements.txt",
                    "startCommand": "python bot.py",
                },
            },
        }
        try:
            created = api(api_key, "POST", "https://api.render.com/v1/services", body)
        except urllib.error.HTTPError as e:
            print(e.read().decode(), file=sys.stderr)
            raise
        svc = created.get("service", created)
        print(f"Created {svc.get('slug')} ({svc.get('id')})")
    else:
        sid = svc["id"]
        print(f"Updating {svc.get('slug')} ({sid}) ...")
        if svc.get("suspended") and svc["suspended"] != "not_suspended":
            api(api_key, "POST", f"https://api.render.com/v1/services/{sid}/resume", {})
            print("  resumed suspended service")
        api(api_key, "PUT", f"https://api.render.com/v1/services/{sid}/env-vars", env_vars)
        api(
            api_key,
            "PATCH",
            f"https://api.render.com/v1/services/{sid}",
            {
                "repo": REPO,
                "branch": BRANCH,
                "serviceDetails": {
                    "rootDir": ROOT_DIR,
                    "healthCheckPath": "/health",
                    "envSpecificDetails": {
                        "buildCommand": "pip install -r requirements.txt",
                        "startCommand": "python bot.py",
                    },
                },
            },
        )

    sid = svc["id"]
    slug = svc.get("slug", SERVICE_NAME)
    base = f"https://{slug}.onrender.com"

    print("Triggering deploy ...")
    dep = api(api_key, "POST", f"https://api.render.com/v1/services/{sid}/deploys", {"clearCache": "do_not_clear"})
    dep_id = (dep.get("deploy") or dep).get("id", "")
    print(f"Deploy id: {dep_id}")

    status = "?"
    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(15)
        if dep_id:
            d = api(api_key, "GET", f"https://api.render.com/v1/services/{sid}/deploys/{dep_id}")
            status = (d.get("deploy") or d).get("status", "?")
        print(f"  status: {status}")
        if status in ("live", "build_failed", "update_failed", "canceled", "deactivated"):
            break

    health = wait_health(base, minutes=8)
    if health:
        print("READY:", base)
        print("HEALTH:", json.dumps(health))
        return 0

    print(f"Deploy status={status}. Check {base}/health manually.")
    return 0 if status == "live" else 1


if __name__ == "__main__":
    raise SystemExit(main())
