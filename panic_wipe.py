"""Filesystem wipe helpers for /panic."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from config import Settings
from database import wipe_database_file
from database_backup import _backup_dir

logger = logging.getLogger(__name__)


def _data_root(settings: Settings) -> Path:
    if settings.data_dir:
        return Path(settings.data_dir)
    return Path(settings.database_path).resolve().parent


def wipe_instance_storage(settings: Settings) -> list[str]:
    """Reset one bot instance database, backups, and export files."""
    actions: list[str] = []
    db_path = Path(settings.database_path)
    stem = db_path.stem

    wipe_database_file(settings.database_path)
    actions.append(f"Reset database {db_path.name}")

    data_root = _data_root(settings)
    backup_dir = _backup_dir(settings.data_dir, settings.database_path)
    if backup_dir.is_dir():
        for backup in backup_dir.glob(f"{stem}-*{db_path.suffix}"):
            backup.unlink(missing_ok=True)
            actions.append(f"Deleted backup {backup.name}")

    exports_dir = data_root / "exports"
    if exports_dir.is_dir():
        for export in exports_dir.glob("*.xlsx"):
            export.unlink(missing_ok=True)
            actions.append(f"Deleted export {export.name}")

    if settings.payments_onedrive_path:
        export_path = Path(settings.payments_onedrive_path)
        if export_path.is_file() and export_path.parent == exports_dir:
            pass
        elif export_path.is_file():
            export_path.unlink(missing_ok=True)
            actions.append(f"Deleted export {export_path.name}")

    return actions


def wipe_extra_paths_from_env() -> list[str]:
    """Delete optional extra paths listed in PANIC_EXTRA_PATHS (comma-separated)."""
    raw = os.getenv("PANIC_EXTRA_PATHS", "").strip()
    if not raw:
        return []

    actions: list[str] = []
    for part in raw.split(","):
        target = Path(part.strip()).expanduser()
        if not part.strip():
            continue
        try:
            if target.is_file():
                target.unlink(missing_ok=True)
                actions.append(f"Deleted file {target}")
            elif target.is_dir():
                shutil.rmtree(target)
                actions.append(f"Deleted folder {target}")
        except OSError as exc:
            logger.warning("Could not delete panic path %s: %s", target, exc)
            actions.append(f"Failed to delete {target}: {exc}")
    return actions
