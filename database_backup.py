"""SQLite backup helpers for cloud deployments."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_BACKUPS = 14


def _backup_dir(data_dir: str | None, database_path: str) -> Path:
    if data_dir:
        return Path(data_dir) / "backups"
    return Path(database_path).resolve().parent / "backups"


def backup_database(*, data_dir: str | None, database_path: str) -> Path | None:
    """Copy the live SQLite file to /data/backups/ (keeps last MAX_BACKUPS)."""
    src = Path(database_path)
    if not src.is_file() or src.stat().st_size < 16:
        return None

    dest_dir = _backup_dir(data_dir, database_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"{src.stem}-{stamp}{src.suffix}"
    shutil.copy2(src, dest)

    backups = sorted(dest_dir.glob(f"{src.stem}-*{src.suffix}"), reverse=True)
    for old in backups[MAX_BACKUPS:]:
        old.unlink(missing_ok=True)

    logger.info("Database backup saved to %s", dest)
    return dest


def database_stats(database_path: str) -> dict[str, int]:
    stats: dict[str, int] = {}
    try:
        with sqlite3.connect(database_path) as conn:
            for table in (
                "payment_outs",
                "extension_links",
                "credo_credit_cards",
                "credo_card_usage",
            ):
                try:
                    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    stats[table] = int(row[0] if row else 0)
                except sqlite3.Error:
                    stats[table] = 0
    except sqlite3.Error:
        pass
    return stats


def latest_backup(data_dir: str | None, database_path: str) -> Path | None:
    src = Path(database_path)
    dest_dir = _backup_dir(data_dir, database_path)
    if not dest_dir.is_dir():
        return None
    backups = sorted(dest_dir.glob(f"{src.stem}-*{src.suffix}"), reverse=True)
    return backups[0] if backups else None


def restore_database_from_backup(database_path: str, backup_path: Path) -> None:
    dest = Path(database_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.copy2(dest, dest.with_suffix(dest.suffix + ".pre-restore.bak"))
    shutil.copy2(backup_path, dest)
    logger.warning("Restored database from backup %s", backup_path)
