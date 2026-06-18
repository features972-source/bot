"""Block local laptop runs — production bot runs on Render only."""

from __future__ import annotations

import os

LOCAL_RUN_MESSAGE = (
    "This bot runs on Render only (24/7 cloud).\n\n"
    "Local execution is disabled so it cannot fight the live bot or wipe data.\n"
    "Manage the bot in the Render dashboard.\n\n"
    "Developers: set ALLOW_LOCAL_RUN=true to override."
)


def local_run_blocked() -> bool:
    if os.getenv("ALLOW_LOCAL_RUN", "").strip().lower() in {"1", "true", "yes"}:
        return False
    if os.getenv("CLOUD_DEPLOYED", "").strip().lower() in {"1", "true", "yes"}:
        return False
    if os.getenv("RENDER", "").strip().lower() in {"true", "1", "yes"}:
        return False
    if os.getenv("RENDER_EXTERNAL_URL", "").strip():
        return False
    return True


def assert_cloud_run_or_exit() -> None:
    if local_run_blocked():
        raise RuntimeError(LOCAL_RUN_MESSAGE)
