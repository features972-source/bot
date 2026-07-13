"""SQLite storage for reminders and sales."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).resolve().parent / "reminders.db")))


@dataclass
class Reminder:
    id: int
    chat_id: int
    created_by: int
    username: str
    reason: str
    remind_at: datetime
    sent: bool


@dataclass
class Sale:
    id: int
    chat_id: int
    created_by: int
    buyer: str
    product: str
    remind_at: datetime | None
    reminder_id: int | None
    created_at: datetime


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                username TEXT NOT NULL,
                reason TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                buyer TEXT NOT NULL,
                product TEXT NOT NULL,
                created_at TEXT NOT NULL,
                remind_at TEXT,
                reminder_id INTEGER
            )
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sales)").fetchall()}
        if "remind_at" not in cols:
            conn.execute("ALTER TABLE sales ADD COLUMN remind_at TEXT")
        if "reminder_id" not in cols:
            conn.execute("ALTER TABLE sales ADD COLUMN reminder_id INTEGER")


def add_sale(
    chat_id: int,
    created_by: int,
    buyer: str,
    product: str,
    *,
    remind_at: datetime | None = None,
    reminder_id: int | None = None,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO sales (chat_id, created_by, buyer, product, created_at, remind_at, reminder_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                created_by,
                buyer,
                product,
                datetime.now().isoformat(),
                remind_at.isoformat() if remind_at else None,
                reminder_id,
            ),
        )
        return int(cur.lastrowid)


def get_sale(sale_id: int) -> Sale | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, chat_id, created_by, buyer, product, created_at, remind_at, reminder_id
            FROM sales
            WHERE id = ?
            """,
            (sale_id,),
        ).fetchone()
    return _row_to_sale(row) if row else None


def update_sale_created_at(sale_id: int, created_at: datetime) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE sales SET created_at = ? WHERE id = ?",
            (created_at.isoformat(), sale_id),
        )
        return cur.rowcount > 0


def delete_sale(sale_id: int) -> Sale | None:
    sale = get_sale(sale_id)
    if sale is None:
        return None
    with connect() as conn:
        conn.execute("DELETE FROM sales WHERE id = ?", (sale_id,))
    return sale


def list_sales(limit: int = 50) -> list[Sale]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, chat_id, created_by, buyer, product, created_at, remind_at, reminder_id
            FROM sales
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_row_to_sale(row) for row in rows]


def add_reminder(
    chat_id: int,
    created_by: int,
    username: str,
    reason: str,
    remind_at: datetime,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO reminders (chat_id, created_by, username, reason, remind_at, sent)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (chat_id, created_by, username, reason, remind_at.isoformat()),
        )
        return int(cur.lastrowid)


def list_active() -> list[Reminder]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, chat_id, created_by, username, reason, remind_at, sent
            FROM reminders
            WHERE sent = 0
            ORDER BY remind_at ASC
            """
        ).fetchall()
    return [_row_to_reminder(row) for row in rows]


def get_reminder(reminder_id: int) -> Reminder | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, chat_id, created_by, username, reason, remind_at, sent
            FROM reminders
            WHERE id = ?
            """,
            (reminder_id,),
        ).fetchone()
    return _row_to_reminder(row) if row else None


def mark_sent(reminder_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))


def update_reminder_remind_at(reminder_id: int, remind_at: datetime) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE reminders SET remind_at = ? WHERE id = ?",
            (remind_at.isoformat(), reminder_id),
        )


def update_sale_remind_at_by_reminder(reminder_id: int, remind_at: datetime) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE sales SET remind_at = ? WHERE reminder_id = ?",
            (remind_at.isoformat(), reminder_id),
        )


def _row_to_reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(
        id=row["id"],
        chat_id=row["chat_id"],
        created_by=row["created_by"],
        username=row["username"],
        reason=row["reason"],
        remind_at=datetime.fromisoformat(row["remind_at"]),
        sent=bool(row["sent"]),
    )


def _row_to_sale(row: sqlite3.Row) -> Sale:
    remind_at = row["remind_at"]
    return Sale(
        id=row["id"],
        chat_id=row["chat_id"],
        created_by=row["created_by"],
        buyer=row["buyer"],
        product=row["product"],
        remind_at=datetime.fromisoformat(remind_at) if remind_at else None,
        reminder_id=row["reminder_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )
