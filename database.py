import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class BotAdmin:
    telegram_user_id: int
    telegram_username: str | None
    display_name: str | None
    added_at: str


@dataclass
class CredoWhitelistUser:
    telegram_user_id: int
    telegram_username: str | None
    display_name: str | None
    added_at: str


@dataclass
class Q1PremiumUser:
    telegram_user_id: int
    telegram_username: str | None
    display_name: str | None
    added_at: str


@dataclass
class PassQueueVip:
    telegram_user_id: int
    telegram_username: str | None
    display_name: str | None
    added_at: str


@dataclass
class ChatBlacklistEntry:
    chat_id: int
    telegram_username: str
    telegram_user_id: int | None
    display_name: str | None
    reason: str | None
    blocked_by_user_id: int | None
    blocked_by_username: str | None
    added_at: str


@dataclass
class CredoProfile:
    id: int
    name: str
    photo_file_id: str
    created_by_user_id: int
    created_by_username: str | None
    created_at: str


@dataclass
class CredoCreditCard:
    name: str
    photo_file_id: str | None
    logo_file_id: str | None
    capacity: float
    added_at: str
    card_last4: str | None = None


@dataclass
class CredoCardUsage:
    id: int
    card_name: str
    telegram_user_id: int
    telegram_username: str | None
    display_name: str | None
    amount: float
    created_at: str


@dataclass
class ExtensionLink:
    extension: str
    telegram_user_id: int
    telegram_username: str | None
    display_name: str | None


@dataclass
class AgentCallStats:
    extension: str
    telegram_username: str | None
    display_name: str | None
    call_count: int
    total_seconds: int
    avg_seconds: float


@dataclass
class PaymentSender:
    telegram_user_id: int
    telegram_username: str | None
    display_name: str | None
    latest_amount: float
    total_amount: float
    payment_count: int
    latest_at: str


PAYMENT_STATUS_NOT_CLEARED = 0
PAYMENT_STATUS_CLEARED = 1
PAYMENT_STATUS_PENDING = 2


@dataclass
class PaymentRecord:
    id: int
    amount: float
    raw_text: str
    created_at: str
    finisher_user_id: int
    finisher_username: str | None
    finisher_display_name: str | None
    starter_user_id: int | None
    starter_username: str | None
    starter_display_name: str | None
    cleared: bool | None = None
    card_last4: str | None = None


@dataclass
class ExpenseRecord:
    id: int
    amount: float
    raw_text: str
    reason: str
    created_at: str
    telegram_user_id: int
    telegram_username: str | None
    display_name: str | None


@dataclass
class ExpenseSpendingEntry:
    user_id: int
    telegram_username: str | None
    display_name: str | None
    expense_count: int
    total_amount: float


@dataclass
class PaymentLeaderboardEntry:
    user_id: int
    telegram_username: str | None
    display_name: str | None
    payment_count: int
    total_amount: float


@dataclass
class PaymentNemesis:
    chat_id: int
    user_a_id: int
    user_a_username: str | None
    user_a_display: str | None
    user_b_id: int
    user_b_username: str | None
    user_b_display: str | None
    created_by_id: int
    created_at: str


@dataclass
class PassQueueEntry:
    user_id: int
    telegram_username: str | None
    display_name: str | None
    joined_at: str
    is_vip: bool = False


@dataclass
class PassOffer:
    id: int
    chat_id: int
    notes_message_id: int
    offer_message_id: int | None
    starter_user_id: int
    starter_username: str | None
    starter_display_name: str | None
    assigned_user_id: int
    assigned_username: str | None
    assigned_display_name: str | None
    notes_text: str
    status: str
    created_at: str
    last_reminder_at: str | None = None


@dataclass
class PendingPassNote:
    id: int
    chat_id: int
    notes_message_id: int
    starter_user_id: int
    starter_username: str | None
    starter_display_name: str | None
    notes_text: str
    created_at: str


@dataclass
class CompletedCall:
    id: int
    extension: str
    telegram_username: str | None
    display_name: str | None
    duration_seconds: int
    caller_name: str
    caller_number: str
    call_kind: str
    started_at: str
    ended_at: str


@dataclass
class MissedCall:
    id: int
    extension: str
    telegram_user_id: int
    telegram_username: str | None
    display_name: str | None
    caller_name: str
    caller_number: str
    callid: int | None
    ring_seconds: int
    missed_at: str
    source: str


@dataclass
class MailerLogEntry:
    id: int
    session_id: str
    event_type: str
    telegram_user_id: int
    telegram_username: str | None
    display_name: str | None
    detail: str
    recipient: str | None
    destination: str | None
    content: str | None
    created_at: str


def init_db(path: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS extension_links (
                extension TEXT PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL,
                telegram_username TEXT,
                display_name TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS completed_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                extension TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                telegram_username TEXT,
                display_name TEXT,
                duration_seconds INTEGER NOT NULL,
                caller_name TEXT NOT NULL DEFAULT '',
                caller_number TEXT NOT NULL DEFAULT '',
                call_kind TEXT NOT NULL DEFAULT 'normal',
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_completed_calls_ended_at
            ON completed_calls (ended_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_completed_calls_extension
            ON completed_calls (extension)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_admins (
                telegram_user_id INTEGER PRIMARY KEY,
                telegram_username TEXT,
                display_name TEXT,
                added_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_outs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                telegram_username TEXT,
                display_name TEXT,
                amount REAL NOT NULL,
                raw_text TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                telegram_message_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_payment_outs_user
            ON payment_outs (telegram_user_id, created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                telegram_username TEXT,
                display_name TEXT,
                amount REAL NOT NULL,
                raw_text TEXT NOT NULL,
                reason TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                telegram_message_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_expenses_chat_message
            ON expenses (chat_id, telegram_message_id)
            WHERE telegram_message_id IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_expenses_created
            ON expenses (created_at ASC, id ASC)
            """
        )
        _ensure_payment_out_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS credo_whitelist (
                telegram_user_id INTEGER PRIMARY KEY,
                telegram_username TEXT,
                display_name TEXT,
                added_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS credo_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                photo_file_id TEXT NOT NULL,
                created_by_user_id INTEGER NOT NULL,
                created_by_username TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS credo_credit_cards (
                name TEXT PRIMARY KEY,
                photo_file_id TEXT,
                added_at TEXT NOT NULL
            )
            """
        )
        _ensure_credo_credit_card_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS credo_card_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_name TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                telegram_username TEXT,
                display_name TEXT,
                amount REAL NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_credo_card_usage_card
            ON credo_card_usage (card_name, created_at DESC)
            """
        )
        _ensure_credo_card_usage_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_blacklist (
                chat_id INTEGER NOT NULL,
                telegram_username TEXT NOT NULL,
                telegram_user_id INTEGER,
                display_name TEXT,
                added_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, telegram_username)
            )
            """
        )
        _ensure_chat_blacklist_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mailer_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                telegram_username TEXT,
                display_name TEXT,
                detail TEXT NOT NULL DEFAULT '',
                recipient TEXT,
                destination TEXT,
                content TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mailer_logs_session
            ON mailer_logs (session_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mailer_logs_user
            ON mailer_logs (telegram_user_id, created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_mailer_logs_created
            ON mailer_logs (created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS missed_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                extension TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                telegram_username TEXT,
                display_name TEXT,
                caller_name TEXT NOT NULL DEFAULT '',
                caller_number TEXT NOT NULL DEFAULT '',
                callid INTEGER,
                ring_seconds INTEGER NOT NULL DEFAULT 0,
                missed_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '3cx'
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_missed_calls_missed_at
            ON missed_calls (missed_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_missed_calls_extension
            ON missed_calls (extension, missed_at DESC)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_missed_calls_callid
            ON missed_calls (extension, callid)
            WHERE callid IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS q1_premium_users (
                telegram_user_id INTEGER PRIMARY KEY,
                telegram_username TEXT,
                display_name TEXT,
                added_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quiet_win_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                win_type TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_quiet_win_log_user_type
            ON quiet_win_log (telegram_user_id, win_type, created_at DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ready_check_sent (
                telegram_user_id INTEGER NOT NULL,
                local_date TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (telegram_user_id, local_date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_nemesis (
                chat_id INTEGER PRIMARY KEY,
                user_a_id INTEGER NOT NULL,
                user_a_username TEXT,
                user_a_display TEXT,
                user_b_id INTEGER NOT NULL,
                user_b_username TEXT,
                user_b_display TEXT,
                created_by_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pass_queue (
                telegram_user_id INTEGER PRIMARY KEY,
                telegram_username TEXT,
                display_name TEXT,
                joined_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pass_queue_vips (
                telegram_user_id INTEGER PRIMARY KEY,
                telegram_username TEXT,
                display_name TEXT,
                added_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pass_offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                notes_message_id INTEGER NOT NULL,
                offer_message_id INTEGER,
                starter_user_id INTEGER NOT NULL,
                starter_username TEXT,
                starter_display_name TEXT,
                assigned_user_id INTEGER NOT NULL,
                assigned_username TEXT,
                assigned_display_name TEXT,
                notes_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                last_reminder_at TEXT,
                UNIQUE(chat_id, notes_message_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pass_notes_pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                notes_message_id INTEGER NOT NULL,
                starter_user_id INTEGER NOT NULL,
                starter_username TEXT,
                starter_display_name TEXT,
                notes_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(chat_id, notes_message_id)
            )
            """
        )
        _ensure_pass_offer_columns(conn)
        conn.commit()


PAIDSIDE_EPOCH_KEY = "paidside_export_epoch"
MS_GRAPH_REFRESH_TOKEN_KEY = "ms_graph_refresh_token"
EXCEL_WEB_URL_KEY = "excel_web_url"
NOTIFY_CHAT_ID_KEY = "notify_chat_id"


def get_notify_chat_id(path: str) -> int | None:
    raw = _get_bot_setting(path, NOTIFY_CHAT_ID_KEY)
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def set_notify_chat_id(path: str, chat_id: int) -> None:
    _set_bot_setting(path, NOTIFY_CHAT_ID_KEY, str(chat_id))


PAYMENT_NOTIFY_CHAT_ID_KEY = "payment_notify_chat_id"


def get_payment_notify_chat_id(path: str) -> int | None:
    raw = _get_bot_setting(path, PAYMENT_NOTIFY_CHAT_ID_KEY)
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def set_payment_notify_chat_id(path: str, chat_id: int) -> None:
    _set_bot_setting(path, PAYMENT_NOTIFY_CHAT_ID_KEY, str(chat_id))


PAYMENT_NOTIFY_MESSAGE_ID_KEY = "payment_notify_message_id"


def get_payment_notify_message_id(path: str) -> int | None:
    raw = _get_bot_setting(path, PAYMENT_NOTIFY_MESSAGE_ID_KEY)
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def set_payment_notify_message_id(path: str, message_id: int) -> None:
    _set_bot_setting(path, PAYMENT_NOTIFY_MESSAGE_ID_KEY, str(message_id))


def clear_payment_notify_message_id(path: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "DELETE FROM bot_settings WHERE key = ?",
            (PAYMENT_NOTIFY_MESSAGE_ID_KEY,),
        )
        conn.commit()


EXPENSE_LOGGING_CHAT_ID_KEY = "expense_logging_chat_id"
EXPENSE_REPORT_CHAT_ID_KEY = "expense_report_chat_id"
EXPENSE_REPORT_MESSAGE_ID_KEY = "expense_report_message_id"
# Legacy keys (read as fallback until groups are re-configured)
EXPENSE_NOTIFY_CHAT_ID_KEY = "expense_notify_chat_id"
EXPENSE_NOTIFY_MESSAGE_ID_KEY = "expense_notify_message_id"


def _parse_stored_chat_id(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def get_expense_logging_chat_id(path: str) -> int | None:
    chat_id = _parse_stored_chat_id(_get_bot_setting(path, EXPENSE_LOGGING_CHAT_ID_KEY))
    if chat_id is not None:
        return chat_id
    legacy = _parse_stored_chat_id(_get_bot_setting(path, EXPENSE_NOTIFY_CHAT_ID_KEY))
    if legacy is not None:
        return legacy
    return get_expense_report_chat_id(path)


def set_expense_logging_chat_id(path: str, chat_id: int) -> None:
    _set_bot_setting(path, EXPENSE_LOGGING_CHAT_ID_KEY, str(chat_id))


def get_expense_report_chat_id(path: str) -> int | None:
    chat_id = _parse_stored_chat_id(_get_bot_setting(path, EXPENSE_REPORT_CHAT_ID_KEY))
    if chat_id is not None:
        return chat_id
    return _parse_stored_chat_id(_get_bot_setting(path, EXPENSE_NOTIFY_CHAT_ID_KEY))


def get_expense_table_chat_id(path: str) -> int | None:
    """Group where the live expense table image is posted."""
    chat_id = _parse_stored_chat_id(_get_bot_setting(path, EXPENSE_REPORT_CHAT_ID_KEY))
    if chat_id is not None:
        return chat_id
    chat_id = _parse_stored_chat_id(_get_bot_setting(path, EXPENSE_LOGGING_CHAT_ID_KEY))
    if chat_id is not None:
        return chat_id
    return _parse_stored_chat_id(_get_bot_setting(path, EXPENSE_NOTIFY_CHAT_ID_KEY))


def set_expense_report_chat_id(path: str, chat_id: int) -> None:
    _set_bot_setting(path, EXPENSE_REPORT_CHAT_ID_KEY, str(chat_id))


def get_expense_report_message_id(path: str) -> int | None:
    message_id = _parse_stored_chat_id(
        _get_bot_setting(path, EXPENSE_REPORT_MESSAGE_ID_KEY)
    )
    if message_id is not None:
        return message_id
    return _parse_stored_chat_id(_get_bot_setting(path, EXPENSE_NOTIFY_MESSAGE_ID_KEY))


def set_expense_report_message_id(path: str, message_id: int) -> None:
    _set_bot_setting(path, EXPENSE_REPORT_MESSAGE_ID_KEY, str(message_id))


def clear_expense_report_message_id(path: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "DELETE FROM bot_settings WHERE key IN (?, ?)",
            (EXPENSE_REPORT_MESSAGE_ID_KEY, EXPENSE_NOTIFY_MESSAGE_ID_KEY),
        )
        conn.commit()


def get_expense_notify_chat_id(path: str) -> int | None:
    """Legacy alias — prefer get_expense_logging_chat_id or get_expense_report_chat_id."""
    return get_expense_logging_chat_id(path)


def set_expense_notify_chat_id(path: str, chat_id: int) -> None:
    set_expense_logging_chat_id(path, chat_id)


def get_expense_notify_message_id(path: str) -> int | None:
    return get_expense_report_message_id(path)


def set_expense_notify_message_id(path: str, message_id: int) -> None:
    set_expense_report_message_id(path, message_id)


def clear_expense_notify_message_id(path: str) -> None:
    clear_expense_report_message_id(path)


def _get_bot_setting(path: str, key: str) -> str | None:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = ?",
            (key,),
        ).fetchone()
    return row[0] if row else None


def _set_bot_setting(path: str, key: str, value: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO bot_settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def get_paidside_epoch(path: str) -> datetime | None:
    raw = _get_bot_setting(path, PAIDSIDE_EPOCH_KEY)
    if not raw:
        return None
    try:
        text = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def set_paidside_epoch(path: str, when: datetime) -> None:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    _set_bot_setting(path, PAIDSIDE_EPOCH_KEY, when.astimezone(timezone.utc).isoformat())


def clear_paidside_epoch(path: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "DELETE FROM bot_settings WHERE key = ?",
            (PAIDSIDE_EPOCH_KEY,),
        )
        conn.commit()


def get_ms_graph_refresh_token(path: str) -> str | None:
    return _get_bot_setting(path, MS_GRAPH_REFRESH_TOKEN_KEY)


def set_ms_graph_refresh_token(path: str, token: str) -> None:
    _set_bot_setting(path, MS_GRAPH_REFRESH_TOKEN_KEY, token.strip())


def get_excel_web_url(path: str) -> str | None:
    return _get_bot_setting(path, EXCEL_WEB_URL_KEY)


def set_excel_web_url(path: str, url: str) -> None:
    _set_bot_setting(path, EXCEL_WEB_URL_KEY, url.strip())


def _ensure_chat_blacklist_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(chat_blacklist)").fetchall()
    }
    if "reason" not in columns:
        conn.execute("ALTER TABLE chat_blacklist ADD COLUMN reason TEXT")
    if "blocked_by_user_id" not in columns:
        conn.execute(
            "ALTER TABLE chat_blacklist ADD COLUMN blocked_by_user_id INTEGER"
        )
    if "blocked_by_username" not in columns:
        conn.execute(
            "ALTER TABLE chat_blacklist ADD COLUMN blocked_by_username TEXT"
        )


def _ensure_credo_credit_card_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(credo_credit_cards)").fetchall()
    }
    if "photo_file_id" not in columns:
        conn.execute("ALTER TABLE credo_credit_cards ADD COLUMN photo_file_id TEXT")
    if "capacity" not in columns:
        conn.execute(
            "ALTER TABLE credo_credit_cards ADD COLUMN capacity REAL NOT NULL DEFAULT 0"
        )
    if "logo_file_id" not in columns:
        conn.execute("ALTER TABLE credo_credit_cards ADD COLUMN logo_file_id TEXT")
    if "card_last4" not in columns:
        conn.execute("ALTER TABLE credo_credit_cards ADD COLUMN card_last4 TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_credo_credit_cards_last4
        ON credo_credit_cards (card_last4)
        WHERE card_last4 IS NOT NULL
        """
    )


def _ensure_credo_card_usage_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(credo_card_usage)").fetchall()
    }
    if "source_payment_id" not in columns:
        conn.execute("ALTER TABLE credo_card_usage ADD COLUMN source_payment_id INTEGER")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_credo_usage_payment
        ON credo_card_usage (source_payment_id)
        WHERE source_payment_id IS NOT NULL
        """
    )


def _ensure_payment_out_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(payment_outs)").fetchall()
    }
    if "telegram_message_id" not in columns:
        conn.execute("ALTER TABLE payment_outs ADD COLUMN telegram_message_id INTEGER")
    if "starter_telegram_user_id" not in columns:
        conn.execute("ALTER TABLE payment_outs ADD COLUMN starter_telegram_user_id INTEGER")
    if "starter_telegram_username" not in columns:
        conn.execute("ALTER TABLE payment_outs ADD COLUMN starter_telegram_username TEXT")
    if "starter_display_name" not in columns:
        conn.execute("ALTER TABLE payment_outs ADD COLUMN starter_display_name TEXT")
    if "cleared" not in columns:
        conn.execute(
            "ALTER TABLE payment_outs ADD COLUMN cleared INTEGER NOT NULL DEFAULT 0"
        )
    if "card_last4" not in columns:
        conn.execute("ALTER TABLE payment_outs ADD COLUMN card_last4 TEXT")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_outs_message
        ON payment_outs (chat_id, telegram_message_id)
        WHERE telegram_message_id IS NOT NULL
        """
    )


def _ensure_pass_offer_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(pass_offers)").fetchall()
    }
    if "last_reminder_at" not in columns:
        conn.execute("ALTER TABLE pass_offers ADD COLUMN last_reminder_at TEXT")


def link_extension(
    path: str,
    *,
    extension: str,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str | None,
) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO extension_links (extension, telegram_user_id, telegram_username, display_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(extension) DO UPDATE SET
                telegram_user_id = excluded.telegram_user_id,
                telegram_username = excluded.telegram_username,
                display_name = excluded.display_name
            """,
            (extension, telegram_user_id, telegram_username, display_name),
        )
        conn.commit()


def unlink_extension(path: str, extension: str) -> bool:
    with _connect(path) as conn:
        cursor = conn.execute(
            "DELETE FROM extension_links WHERE extension = ?",
            (extension,),
        )
        conn.commit()
        return cursor.rowcount > 0


def unlink_by_telegram_user_id(path: str, telegram_user_id: int) -> ExtensionLink | None:
    link = get_link_by_telegram_user_id(path, telegram_user_id)
    if link is None:
        return None
    if unlink_extension(path, link.extension):
        return link
    return None


def get_link_by_extension(path: str, extension: str) -> ExtensionLink | None:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT extension, telegram_user_id, telegram_username, display_name
            FROM extension_links
            WHERE extension = ?
            """,
            (extension,),
        ).fetchone()

    if row is None:
        return None
    return ExtensionLink(
        extension=row[0],
        telegram_user_id=row[1],
        telegram_username=row[2],
        display_name=row[3],
    )


def get_link_by_telegram_user_id(path: str, telegram_user_id: int) -> ExtensionLink | None:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT extension, telegram_user_id, telegram_username, display_name
            FROM extension_links
            WHERE telegram_user_id = ?
            """,
            (telegram_user_id,),
        ).fetchone()

    if row is None:
        return None
    return ExtensionLink(
        extension=row[0],
        telegram_user_id=row[1],
        telegram_username=row[2],
        display_name=row[3],
    )


def list_links(path: str) -> list[ExtensionLink]:
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT extension, telegram_user_id, telegram_username, display_name
            FROM extension_links
            ORDER BY extension
            """
        ).fetchall()

    return [
        ExtensionLink(
            extension=row[0],
            telegram_user_id=row[1],
            telegram_username=row[2],
            display_name=row[3],
        )
        for row in rows
    ]


def record_completed_call(
    path: str,
    *,
    extension: str,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str | None,
    duration_seconds: int,
    caller_name: str = "",
    caller_number: str = "",
    call_kind: str = "normal",
    started_at_utc: float | None = None,
) -> None:
    ended_at = datetime.now(timezone.utc)
    if started_at_utc is not None and started_at_utc > 0:
        started_at = datetime.fromtimestamp(started_at_utc, tz=timezone.utc)
    else:
        started_at = ended_at - timedelta(seconds=max(0, duration_seconds))

    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO completed_calls (
                extension,
                telegram_user_id,
                telegram_username,
                display_name,
                duration_seconds,
                caller_name,
                caller_number,
                call_kind,
                started_at,
                ended_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                extension,
                telegram_user_id,
                telegram_username,
                display_name,
                max(0, duration_seconds),
                caller_name or "",
                caller_number or "",
                call_kind or "normal",
                started_at.isoformat(),
                ended_at.isoformat(),
            ),
        )
        conn.commit()


def get_agent_call_stats(path: str, *, since: datetime | None = None) -> list[AgentCallStats]:
    query = """
        SELECT
            c.extension,
            COALESCE(l.telegram_username, MAX(c.telegram_username)),
            COALESCE(l.display_name, MAX(c.display_name)),
            COUNT(*) AS call_count,
            COALESCE(SUM(c.duration_seconds), 0) AS total_seconds,
            COALESCE(AVG(c.duration_seconds), 0) AS avg_seconds
        FROM completed_calls AS c
        LEFT JOIN extension_links AS l ON l.extension = c.extension
    """
    params: list = []
    if since is not None:
        query += " WHERE c.ended_at >= ?"
        params.append(since.isoformat())
    query += """
        GROUP BY c.extension
        ORDER BY call_count DESC, total_seconds DESC, c.extension ASC
    """
    with _connect(path) as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        AgentCallStats(
            extension=row[0],
            telegram_username=row[1],
            display_name=row[2],
            call_count=int(row[3]),
            total_seconds=int(row[4]),
            avg_seconds=float(row[5]),
        )
        for row in rows
    ]


def get_call_stats_totals(path: str, *, since: datetime | None = None) -> tuple[int, int]:
    query = "SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0) FROM completed_calls"
    params: list = []
    if since is not None:
        query += " WHERE ended_at >= ?"
        params.append(since.isoformat())
    with _connect(path) as conn:
        row = conn.execute(query, params).fetchone()
    if row is None:
        return 0, 0
    return int(row[0]), int(row[1])


def list_recent_completed_calls(path: str, *, limit: int = 15, since: datetime | None = None) -> list[CompletedCall]:
    query = """
        SELECT id, extension, telegram_username, display_name, duration_seconds,
               caller_name, caller_number, call_kind, started_at, ended_at
        FROM completed_calls
    """
    params: list = []
    if since is not None:
        query += " WHERE ended_at >= ?"
        params.append(since.isoformat())
    query += " ORDER BY ended_at DESC LIMIT ?"
    params.append(limit)
    with _connect(path) as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        CompletedCall(
            id=row[0],
            extension=row[1],
            telegram_username=row[2],
            display_name=row[3],
            duration_seconds=int(row[4]),
            caller_name=row[5] or "",
            caller_number=row[6] or "",
            call_kind=row[7] or "normal",
            started_at=row[8],
            ended_at=row[9],
        )
        for row in rows
    ]


def add_bot_admin(
    path: str,
    *,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str | None,
) -> None:
    added_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO bot_admins (telegram_user_id, telegram_username, display_name, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                telegram_username = excluded.telegram_username,
                display_name = excluded.display_name
            """,
            (telegram_user_id, telegram_username, display_name, added_at),
        )
        conn.commit()


def remove_bot_admin(path: str, telegram_user_id: int) -> bool:
    with _connect(path) as conn:
        cursor = conn.execute(
            "DELETE FROM bot_admins WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def list_bot_admins(path: str) -> list[BotAdmin]:
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT telegram_user_id, telegram_username, display_name, added_at
            FROM bot_admins
            ORDER BY added_at ASC
            """
        ).fetchall()
    return [
        BotAdmin(
            telegram_user_id=row[0],
            telegram_username=row[1],
            display_name=row[2],
            added_at=row[3],
        )
        for row in rows
    ]


def _cleared_from_db(value: object) -> bool | None:
    if value is None:
        return None
    try:
        status = int(value)
    except (TypeError, ValueError):
        return False
    if status == PAYMENT_STATUS_PENDING:
        return None
    return status == PAYMENT_STATUS_CLEARED


def _payment_record_from_row(row: tuple) -> PaymentRecord:
    return PaymentRecord(
        id=row[0],
        amount=float(row[1]),
        raw_text=row[2],
        created_at=row[3],
        finisher_user_id=row[4],
        finisher_username=row[5],
        finisher_display_name=row[6],
        starter_user_id=row[7],
        starter_username=row[8],
        starter_display_name=row[9],
        cleared=_cleared_from_db(row[10]),
        card_last4=row[11] if len(row) > 11 else None,
    )


_PAYMENT_SELECT = """
    SELECT
        id, amount, raw_text, created_at,
        telegram_user_id, telegram_username, display_name,
        starter_telegram_user_id, starter_telegram_username, starter_display_name,
        cleared, card_last4
    FROM payment_outs
"""


def get_payment_by_id(path: str, payment_id: int) -> PaymentRecord | None:
    with _connect(path) as conn:
        row = conn.execute(
            f"{_PAYMENT_SELECT} WHERE id = ? LIMIT 1",
            (payment_id,),
        ).fetchone()
    if row is None:
        return None
    return _payment_record_from_row(row)


def get_payment_by_message(
    path: str, *, chat_id: int, telegram_message_id: int
) -> PaymentRecord | None:
    with _connect(path) as conn:
        row = conn.execute(
            f"""
            {_PAYMENT_SELECT}
            WHERE chat_id = ? AND telegram_message_id = ?
            LIMIT 1
            """,
            (chat_id, telegram_message_id),
        ).fetchone()
    if row is None:
        return None
    return _payment_record_from_row(row)


def update_payment_cleared(path: str, payment_id: int, *, cleared: bool) -> bool:
    status = PAYMENT_STATUS_CLEARED if cleared else PAYMENT_STATUS_NOT_CLEARED
    with _connect(path) as conn:
        cursor = conn.execute(
            "UPDATE payment_outs SET cleared = ? WHERE id = ?",
            (status, payment_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_payment_out(path: str, payment_id: int) -> bool:
    with _connect(path) as conn:
        cursor = conn.execute("DELETE FROM payment_outs WHERE id = ?", (payment_id,))
        conn.commit()
        return cursor.rowcount > 0


def update_payment_amount(path: str, payment_id: int, *, amount: float) -> bool:
    if amount <= 0:
        return False
    with _connect(path) as conn:
        cursor = conn.execute(
            "UPDATE payment_outs SET amount = ? WHERE id = ?",
            (amount, payment_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def record_payment_out(
    path: str,
    *,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str | None,
    amount: float,
    raw_text: str,
    chat_id: int,
    telegram_message_id: int | None = None,
    created_at: str | None = None,
    starter_user_id: int | None = None,
    starter_username: str | None = None,
    starter_display_name: str | None = None,
    card_last4: str | None = None,
) -> int | None:
    """Insert payment; return new row id, or None if duplicate message."""
    if telegram_message_id is not None and payment_message_exists(
        path, chat_id=chat_id, telegram_message_id=telegram_message_id
    ):
        return None
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO payment_outs (
                telegram_user_id,
                telegram_username,
                display_name,
                amount,
                raw_text,
                chat_id,
                created_at,
                telegram_message_id,
                starter_telegram_user_id,
                starter_telegram_username,
                starter_display_name,
                cleared,
                card_last4
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_user_id,
                telegram_username,
                display_name,
                amount,
                raw_text,
                chat_id,
                created_at,
                telegram_message_id,
                starter_user_id,
                starter_username,
                starter_display_name,
                PAYMENT_STATUS_PENDING,
                card_last4,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid) if cursor.lastrowid else None


def payment_message_exists(
    path: str, *, chat_id: int, telegram_message_id: int
) -> bool:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM payment_outs
            WHERE chat_id = ? AND telegram_message_id = ?
            LIMIT 1
            """,
            (chat_id, telegram_message_id),
        ).fetchone()
    return row is not None


def list_payment_senders(path: str) -> list[PaymentSender]:
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT
                telegram_user_id,
                MAX(telegram_username) AS telegram_username,
                MAX(display_name) AS display_name,
                (
                    SELECT amount FROM payment_outs p2
                    WHERE p2.telegram_user_id = payment_outs.telegram_user_id
                    ORDER BY p2.created_at DESC, p2.id DESC
                    LIMIT 1
                ) AS latest_amount,
                SUM(amount) AS total_amount,
                COUNT(*) AS payment_count,
                MAX(created_at) AS latest_at
            FROM payment_outs
            GROUP BY telegram_user_id
            ORDER BY latest_at DESC
            """
        ).fetchall()
    return [
        PaymentSender(
            telegram_user_id=row[0],
            telegram_username=row[1],
            display_name=row[2],
            latest_amount=float(row[3]),
            total_amount=float(row[4]),
            payment_count=int(row[5]),
            latest_at=row[6],
        )
        for row in rows
    ]


def list_recent_payments(path: str, *, limit: int = 30) -> list[PaymentRecord]:
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            {_PAYMENT_SELECT}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_payment_record_from_row(row) for row in rows]


def list_payments_since(path: str, *, since: datetime) -> list[PaymentRecord]:
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            {_PAYMENT_SELECT}
            WHERE created_at >= ?
            ORDER BY created_at ASC, id ASC
            """,
            (since.isoformat(),),
        ).fetchall()
    return [_payment_record_from_row(row) for row in rows]


def finisher_payment_streak_today(
    path: str,
    *,
    finisher_user_id: int,
    since: datetime,
) -> int:
    """Consecutive today payments by finisher, counting back from the latest."""
    streak = 0
    for record in reversed(list_payments_since(path, since=since)):
        if record.finisher_user_id == finisher_user_id:
            streak += 1
        else:
            break
    return streak


def count_user_calls_since(
    path: str,
    *,
    telegram_user_id: int,
    since: datetime,
) -> int:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM completed_calls
            WHERE telegram_user_id = ? AND ended_at >= ?
            """,
            (telegram_user_id, since.isoformat()),
        ).fetchone()
    return int(row[0]) if row else 0


def count_user_finishes_since(
    path: str,
    *,
    telegram_user_id: int,
    since: datetime,
) -> tuple[int, float]:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(amount), 0)
            FROM payment_outs
            WHERE telegram_user_id = ? AND created_at >= ?
            """,
            (telegram_user_id, since.isoformat()),
        ).fetchone()
    if row is None:
        return 0, 0.0
    return int(row[0]), float(row[1])


def count_user_opens_since(
    path: str,
    *,
    telegram_user_id: int,
    since: datetime,
) -> tuple[int, float]:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(amount), 0)
            FROM payment_outs
            WHERE starter_telegram_user_id = ? AND created_at >= ?
            """,
            (telegram_user_id, since.isoformat()),
        ).fetchone()
    if row is None:
        return 0, 0.0
    return int(row[0]), float(row[1])


def list_all_payments(path: str) -> list[PaymentRecord]:
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            {_PAYMENT_SELECT}
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
    return [_payment_record_from_row(row) for row in rows]


def clear_all_payments(path: str) -> int:
    with _connect(path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM payment_outs").fetchone()
        count = int(row[0]) if row else 0
        conn.execute("DELETE FROM payment_outs")
        conn.commit()
    return count


def count_payments_before(path: str, before: datetime) -> int:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM payment_outs WHERE created_at < ?",
            (before.isoformat(),),
        ).fetchone()
    return int(row[0]) if row else 0


def clear_payments_before(path: str, before: datetime) -> int:
    """Delete payments strictly before `before` (UTC). Returns rows removed."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM payment_outs WHERE created_at < ?",
            (before.isoformat(),),
        ).fetchone()
        count = int(row[0]) if row else 0
        conn.execute(
            "DELETE FROM payment_outs WHERE created_at < ?",
            (before.isoformat(),),
        )
        conn.commit()
    return count


def get_payment_totals(
    path: str,
    *,
    since: datetime | None = None,
    cleared: bool | None = None,
    pending: bool = False,
) -> tuple[int, float]:
    clauses: list[str] = []
    params: list[str | int] = []
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since.isoformat())
    if pending:
        clauses.append("cleared = ?")
        params.append(PAYMENT_STATUS_PENDING)
    elif cleared is not None:
        clauses.append("cleared = ?")
        params.append(PAYMENT_STATUS_CLEARED if cleared else PAYMENT_STATUS_NOT_CLEARED)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect(path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payment_outs {where}",
            params,
        ).fetchone()
    if row is None:
        return 0, 0.0
    return int(row[0]), float(row[1])


_EXPENSE_SELECT = """
    SELECT
        id,
        amount,
        raw_text,
        reason,
        created_at,
        telegram_user_id,
        telegram_username,
        display_name
    FROM expenses
"""


def _expense_record_from_row(row: tuple) -> ExpenseRecord:
    return ExpenseRecord(
        id=row[0],
        amount=float(row[1]),
        raw_text=row[2],
        reason=row[3],
        created_at=row[4],
        telegram_user_id=row[5],
        telegram_username=row[6],
        display_name=row[7],
    )


def expense_message_exists(
    path: str, *, chat_id: int, telegram_message_id: int
) -> bool:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM expenses
            WHERE chat_id = ? AND telegram_message_id = ?
            LIMIT 1
            """,
            (chat_id, telegram_message_id),
        ).fetchone()
    return row is not None


def record_expense(
    path: str,
    *,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str | None,
    amount: float,
    raw_text: str,
    reason: str,
    chat_id: int,
    telegram_message_id: int | None = None,
    created_at: str | None = None,
) -> int | None:
    if telegram_message_id is not None and expense_message_exists(
        path, chat_id=chat_id, telegram_message_id=telegram_message_id
    ):
        return None
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO expenses (
                telegram_user_id,
                telegram_username,
                display_name,
                amount,
                raw_text,
                reason,
                chat_id,
                created_at,
                telegram_message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_user_id,
                telegram_username,
                display_name,
                amount,
                raw_text,
                reason.strip(),
                chat_id,
                created_at,
                telegram_message_id,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid) if cursor.lastrowid else None


def get_expense_by_id(path: str, expense_id: int) -> ExpenseRecord | None:
    with _connect(path) as conn:
        row = conn.execute(
            f"{_EXPENSE_SELECT} WHERE id = ? LIMIT 1",
            (expense_id,),
        ).fetchone()
    return _expense_record_from_row(row) if row else None


def get_expense_by_message(
    path: str, *, chat_id: int, telegram_message_id: int
) -> ExpenseRecord | None:
    with _connect(path) as conn:
        row = conn.execute(
            f"""
            {_EXPENSE_SELECT}
            WHERE chat_id = ? AND telegram_message_id = ?
            LIMIT 1
            """,
            (chat_id, telegram_message_id),
        ).fetchone()
    return _expense_record_from_row(row) if row else None


def list_recent_expenses(path: str, *, limit: int = 30) -> list[ExpenseRecord]:
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            {_EXPENSE_SELECT}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_expense_record_from_row(row) for row in rows]


def delete_expense(path: str, expense_id: int) -> bool:
    with _connect(path) as conn:
        cursor = conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        conn.commit()
        return cursor.rowcount > 0


def list_expenses_since(path: str, *, since: datetime) -> list[ExpenseRecord]:
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            {_EXPENSE_SELECT}
            WHERE created_at >= ?
            ORDER BY created_at ASC, id ASC
            """,
            (since.isoformat(),),
        ).fetchall()
    return [_expense_record_from_row(row) for row in rows]


def get_expense_totals(
    path: str,
    *,
    since: datetime | None = None,
) -> tuple[int, float]:
    clauses: list[str] = []
    params: list[str] = []
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect(path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM expenses {where}",
            params,
        ).fetchone()
    if row is None:
        return 0, 0.0
    return int(row[0]), float(row[1])


def get_expense_spending_by_user(
    path: str,
    *,
    since: datetime | None = None,
) -> list[ExpenseSpendingEntry]:
    params: list[str] = []
    since_clause = ""
    if since is not None:
        since_clause = "WHERE created_at >= ?"
        params.append(since.isoformat())
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
                telegram_user_id,
                telegram_username,
                display_name,
                COUNT(*) AS expense_count,
                COALESCE(SUM(amount), 0) AS total_amount
            FROM expenses
            {since_clause}
            GROUP BY telegram_user_id
            ORDER BY total_amount DESC, expense_count DESC, telegram_user_id ASC
            """,
            params,
        ).fetchall()
    return [
        ExpenseSpendingEntry(
            user_id=int(row[0]),
            telegram_username=row[1],
            display_name=row[2],
            expense_count=int(row[3]),
            total_amount=float(row[4]),
        )
        for row in rows
    ]


def get_payment_leaderboard(
    path: str,
    *,
    since: datetime | None = None,
) -> list[PaymentLeaderboardEntry]:
    params: list[str] = []
    since_clause = ""
    if since is not None:
        since_clause = "WHERE created_at >= ?"
        params.append(since.isoformat())
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
                telegram_user_id AS user_id,
                telegram_username,
                display_name,
                COUNT(*) AS payment_count,
                SUM(amount) AS total_amount
            FROM payment_outs
            {since_clause}
            GROUP BY telegram_user_id
            ORDER BY total_amount DESC, payment_count DESC, user_id ASC
            """,
            params,
        ).fetchall()
    return [
        PaymentLeaderboardEntry(
            user_id=int(row[0]),
            telegram_username=row[1],
            display_name=row[2],
            payment_count=int(row[3]),
            total_amount=float(row[4]),
        )
        for row in rows
    ]


def get_payment_starter_leaderboard(
    path: str,
    *,
    since: datetime | None = None,
) -> list[PaymentLeaderboardEntry]:
    params: list[str] = []
    clauses = ["starter_telegram_user_id IS NOT NULL"]
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since.isoformat())
    where = "WHERE " + " AND ".join(clauses)
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
                starter_telegram_user_id AS user_id,
                starter_telegram_username,
                starter_display_name,
                COUNT(*) AS payment_count,
                SUM(amount) AS total_amount
            FROM payment_outs
            {where}
            GROUP BY starter_telegram_user_id
            ORDER BY total_amount DESC, payment_count DESC, user_id ASC
            """,
            params,
        ).fetchall()
    return [
        PaymentLeaderboardEntry(
            user_id=int(row[0]),
            telegram_username=row[1],
            display_name=row[2],
            payment_count=int(row[3]),
            total_amount=float(row[4]),
        )
        for row in rows
    ]


def get_user_payment_totals(
    path: str,
    user_id: int,
    *,
    since: datetime | None = None,
) -> tuple[int, float]:
    clauses = ["telegram_user_id = ?"]
    params: list[str | int] = [user_id]
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since.isoformat())
    where = " AND ".join(clauses)
    with _connect(path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payment_outs WHERE {where}",
            params,
        ).fetchone()
    if row is None:
        return 0, 0.0
    return int(row[0]), float(row[1])


def _payment_nemesis_from_row(row: tuple) -> PaymentNemesis:
    return PaymentNemesis(
        chat_id=int(row[0]),
        user_a_id=int(row[1]),
        user_a_username=row[2],
        user_a_display=row[3],
        user_b_id=int(row[4]),
        user_b_username=row[5],
        user_b_display=row[6],
        created_by_id=int(row[7]),
        created_at=row[8],
    )


def set_payment_nemesis(
    path: str,
    *,
    chat_id: int,
    user_a_id: int,
    user_a_username: str | None,
    user_a_display: str | None,
    user_b_id: int,
    user_b_username: str | None,
    user_b_display: str | None,
    created_by_id: int,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO payment_nemesis (
                chat_id,
                user_a_id,
                user_a_username,
                user_a_display,
                user_b_id,
                user_b_username,
                user_b_display,
                created_by_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                user_a_id = excluded.user_a_id,
                user_a_username = excluded.user_a_username,
                user_a_display = excluded.user_a_display,
                user_b_id = excluded.user_b_id,
                user_b_username = excluded.user_b_username,
                user_b_display = excluded.user_b_display,
                created_by_id = excluded.created_by_id,
                created_at = excluded.created_at
            """,
            (
                chat_id,
                user_a_id,
                user_a_username,
                user_a_display,
                user_b_id,
                user_b_username,
                user_b_display,
                created_by_id,
                now,
            ),
        )
        conn.commit()


def get_payment_nemesis(path: str, chat_id: int) -> PaymentNemesis | None:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT
                chat_id,
                user_a_id,
                user_a_username,
                user_a_display,
                user_b_id,
                user_b_username,
                user_b_display,
                created_by_id,
                created_at
            FROM payment_nemesis
            WHERE chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
    return _payment_nemesis_from_row(row) if row else None


def list_payment_nemesis(path: str) -> list[PaymentNemesis]:
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT
                chat_id,
                user_a_id,
                user_a_username,
                user_a_display,
                user_b_id,
                user_b_username,
                user_b_display,
                created_by_id,
                created_at
            FROM payment_nemesis
            ORDER BY chat_id ASC
            """
        ).fetchall()
    return [_payment_nemesis_from_row(row) for row in rows]


def clear_payment_nemesis(path: str, chat_id: int) -> bool:
    with _connect(path) as conn:
        cursor = conn.execute(
            "DELETE FROM payment_nemesis WHERE chat_id = ?",
            (chat_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def _pass_queue_entry_from_row(row) -> PassQueueEntry:
    return PassQueueEntry(
        user_id=int(row[0]),
        telegram_username=row[1],
        display_name=row[2],
        joined_at=row[3],
        is_vip=bool(row[4]) if len(row) > 4 else False,
    )


_PASS_QUEUE_SELECT = """
    SELECT
        pq.telegram_user_id,
        pq.telegram_username,
        pq.display_name,
        pq.joined_at,
        CASE WHEN v.telegram_user_id IS NOT NULL THEN 1 ELSE 0 END AS is_vip
    FROM pass_queue pq
    LEFT JOIN pass_queue_vips v ON v.telegram_user_id = pq.telegram_user_id
"""


def _parse_queue_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _timestamp_between(start: datetime, end: datetime) -> str:
    if end <= start:
        return (start + timedelta(microseconds=500)).isoformat()
    gap = (end - start).total_seconds()
    if gap > 1:
        return (start + timedelta(seconds=gap / 2)).isoformat()
    return (start + timedelta(microseconds=max(1, int(gap * 500_000)))).isoformat()


def _vip_join_timestamp(path: str, *, exclude_user_id: int | None = None) -> str:
    """Place a VIP at the end of the VIP tier (after VIPs, before standard users)."""
    now = datetime.now(timezone.utc)
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            {_PASS_QUEUE_SELECT}
            ORDER BY
                CASE WHEN v.telegram_user_id IS NOT NULL THEN 0 ELSE 1 END,
                pq.joined_at ASC,
                pq.telegram_user_id ASC
            """
        ).fetchall()
    entries = [_pass_queue_entry_from_row(row) for row in rows]
    vips = [
        entry
        for entry in entries
        if entry.is_vip and entry.user_id != exclude_user_id
    ]
    standards = [entry for entry in entries if not entry.is_vip]
    if not standards:
        return now.isoformat()
    first_standard = _parse_queue_timestamp(standards[0].joined_at)
    if not vips:
        return (first_standard - timedelta(seconds=1)).isoformat()
    last_vip = _parse_queue_timestamp(vips[-1].joined_at)
    return _timestamp_between(last_vip, first_standard)


def _rotate_to_back_timestamp(path: str, telegram_user_id: int) -> str:
    """Move a queue member to the back of their tier (VIP or standard)."""
    now = datetime.now(timezone.utc)
    if is_pass_queue_vip(path, telegram_user_id):
        return _vip_join_timestamp(path, exclude_user_id=telegram_user_id)
    return now.isoformat()


def is_pass_queue_vip(path: str, telegram_user_id: int) -> bool:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM pass_queue_vips WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
    return row is not None


def add_pass_queue_vip(
    path: str,
    *,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str | None,
) -> None:
    added_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO pass_queue_vips (telegram_user_id, telegram_username, display_name, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                telegram_username = excluded.telegram_username,
                display_name = excluded.display_name
            """,
            (telegram_user_id, telegram_username, display_name, added_at),
        )
        conn.commit()
    with _connect(path) as conn:
        in_queue = conn.execute(
            "SELECT 1 FROM pass_queue WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        if in_queue:
            conn.execute(
                """
                UPDATE pass_queue
                SET joined_at = ?
                WHERE telegram_user_id = ?
                """,
                (_vip_join_timestamp(path, exclude_user_id=telegram_user_id), telegram_user_id),
            )
            conn.commit()


def remove_pass_queue_vip(path: str, telegram_user_id: int) -> bool:
    with _connect(path) as conn:
        cursor = conn.execute(
            "DELETE FROM pass_queue_vips WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        removed = cursor.rowcount > 0
        if removed:
            in_queue = conn.execute(
                "SELECT 1 FROM pass_queue WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            if in_queue:
                conn.execute(
                    """
                    UPDATE pass_queue
                    SET joined_at = ?
                    WHERE telegram_user_id = ?
                    """,
                    (datetime.now(timezone.utc).isoformat(), telegram_user_id),
                )
        conn.commit()
        return removed


def list_pass_queue_vips(path: str) -> list[PassQueueVip]:
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT telegram_user_id, telegram_username, display_name, added_at
            FROM pass_queue_vips
            ORDER BY added_at ASC, telegram_user_id ASC
            """
        ).fetchall()
    return [
        PassQueueVip(
            telegram_user_id=int(row[0]),
            telegram_username=row[1],
            display_name=row[2],
            added_at=row[3],
        )
        for row in rows
    ]


_PASS_OFFER_SELECT = """
    SELECT
        id,
        chat_id,
        notes_message_id,
        offer_message_id,
        starter_user_id,
        starter_username,
        starter_display_name,
        assigned_user_id,
        assigned_username,
        assigned_display_name,
        notes_text,
        status,
        created_at,
        last_reminder_at
    FROM pass_offers
"""


def _pass_offer_from_row(row) -> PassOffer:
    return PassOffer(
        id=int(row[0]),
        chat_id=int(row[1]),
        notes_message_id=int(row[2]),
        offer_message_id=int(row[3]) if row[3] is not None else None,
        starter_user_id=int(row[4]),
        starter_username=row[5],
        starter_display_name=row[6],
        assigned_user_id=int(row[7]),
        assigned_username=row[8],
        assigned_display_name=row[9],
        notes_text=row[10],
        status=row[11],
        created_at=row[12],
        last_reminder_at=row[13],
    )


def join_pass_queue(
    path: str,
    *,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str | None,
) -> bool:
    """Return True if newly joined, False if already in queue."""
    is_vip = is_pass_queue_vip(path, telegram_user_id)
    joined_at = (
        _vip_join_timestamp(path)
        if is_vip
        else datetime.now(timezone.utc).isoformat()
    )
    with _connect(path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM pass_queue WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE pass_queue
                SET telegram_username = ?, display_name = ?
                WHERE telegram_user_id = ?
                """,
                (telegram_username, display_name, telegram_user_id),
            )
            conn.commit()
            return False
        conn.execute(
            """
            INSERT INTO pass_queue (telegram_user_id, telegram_username, display_name, joined_at)
            VALUES (?, ?, ?, ?)
            """,
            (telegram_user_id, telegram_username, display_name, joined_at),
        )
        conn.commit()
        return True


def leave_pass_queue(path: str, telegram_user_id: int) -> bool:
    with _connect(path) as conn:
        cursor = conn.execute(
            "DELETE FROM pass_queue WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def list_pass_queue(path: str) -> list[PassQueueEntry]:
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            {_PASS_QUEUE_SELECT}
            ORDER BY
                CASE WHEN v.telegram_user_id IS NOT NULL THEN 0 ELSE 1 END,
                pq.joined_at ASC,
                pq.telegram_user_id ASC
            """
        ).fetchall()
    return [_pass_queue_entry_from_row(row) for row in rows]


def get_pass_queue_position(path: str, telegram_user_id: int) -> int:
    entries = list_pass_queue(path)
    for index, entry in enumerate(entries, start=1):
        if entry.user_id == telegram_user_id:
            return index
    return len(entries) + 1


def rotate_pass_queue_user_to_back(path: str, telegram_user_id: int) -> None:
    joined_at = _rotate_to_back_timestamp(path, telegram_user_id)
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT telegram_username, display_name
            FROM pass_queue
            WHERE telegram_user_id = ?
            """,
            (telegram_user_id,),
        ).fetchone()
        if not row:
            return
        conn.execute(
            "DELETE FROM pass_queue WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        conn.execute(
            """
            INSERT INTO pass_queue (telegram_user_id, telegram_username, display_name, joined_at)
            VALUES (?, ?, ?, ?)
            """,
            (telegram_user_id, row[0], row[1], joined_at),
        )
        conn.commit()


def pass_offer_for_notes(path: str, chat_id: int, notes_message_id: int) -> bool:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM pass_offers
            WHERE chat_id = ? AND notes_message_id = ?
            """,
            (chat_id, notes_message_id),
        ).fetchone()
    return row is not None


_PENDING_PASS_NOTE_SELECT = """
    SELECT
        id,
        chat_id,
        notes_message_id,
        starter_user_id,
        starter_username,
        starter_display_name,
        notes_text,
        created_at
    FROM pass_notes_pending
"""


def _pending_pass_note_from_row(row) -> PendingPassNote:
    return PendingPassNote(
        id=int(row[0]),
        chat_id=int(row[1]),
        notes_message_id=int(row[2]),
        starter_user_id=int(row[3]),
        starter_username=row[4],
        starter_display_name=row[5],
        notes_text=row[6],
        created_at=row[7],
    )


def upsert_pending_pass_note(
    path: str,
    *,
    chat_id: int,
    notes_message_id: int,
    starter_user_id: int,
    starter_username: str | None,
    starter_display_name: str | None,
    notes_text: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO pass_notes_pending (
                chat_id,
                notes_message_id,
                starter_user_id,
                starter_username,
                starter_display_name,
                notes_text,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, notes_message_id) DO UPDATE SET
                starter_user_id = excluded.starter_user_id,
                starter_username = excluded.starter_username,
                starter_display_name = excluded.starter_display_name,
                notes_text = excluded.notes_text,
                created_at = excluded.created_at
            """,
            (
                chat_id,
                notes_message_id,
                starter_user_id,
                starter_username,
                starter_display_name,
                notes_text.strip(),
                now,
            ),
        )
        conn.commit()


def list_pending_pass_notes(path: str) -> list[PendingPassNote]:
    with _connect(path) as conn:
        rows = conn.execute(
            f"{_PENDING_PASS_NOTE_SELECT} ORDER BY created_at ASC, id ASC"
        ).fetchall()
    return [_pending_pass_note_from_row(row) for row in rows]


def delete_pending_pass_note(path: str, chat_id: int, notes_message_id: int) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            DELETE FROM pass_notes_pending
            WHERE chat_id = ? AND notes_message_id = ?
            """,
            (chat_id, notes_message_id),
        )
        conn.commit()


def assign_pending_pass_to_user(
    path: str,
    *,
    assigned_user_id: int,
    assigned_username: str | None,
    assigned_display_name: str | None,
) -> PassOffer | None:
    """Offer oldest waiting notes to a finisher who just joined (or became free)."""
    if assigned_user_id in pending_pass_assignee_user_ids(path):
        return None
    for pending in list_pending_pass_notes(path):
        if pending.starter_user_id == assigned_user_id:
            continue
        if pass_offer_for_notes(path, pending.chat_id, pending.notes_message_id):
            delete_pending_pass_note(path, pending.chat_id, pending.notes_message_id)
            continue
        offer_id = create_pass_offer(
            path,
            chat_id=pending.chat_id,
            notes_message_id=pending.notes_message_id,
            starter_user_id=pending.starter_user_id,
            starter_username=pending.starter_username,
            starter_display_name=pending.starter_display_name,
            assigned_user_id=assigned_user_id,
            assigned_username=assigned_username,
            assigned_display_name=assigned_display_name,
            notes_text=pending.notes_text,
        )
        delete_pending_pass_note(path, pending.chat_id, pending.notes_message_id)
        return get_pass_offer(path, offer_id)
    return None


def create_pass_offer(
    path: str,
    *,
    chat_id: int,
    notes_message_id: int,
    starter_user_id: int,
    starter_username: str | None,
    starter_display_name: str | None,
    assigned_user_id: int,
    assigned_username: str | None,
    assigned_display_name: str | None,
    notes_text: str,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO pass_offers (
                chat_id,
                notes_message_id,
                starter_user_id,
                starter_username,
                starter_display_name,
                assigned_user_id,
                assigned_username,
                assigned_display_name,
                notes_text,
                status,
                created_at,
                last_reminder_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                chat_id,
                notes_message_id,
                starter_user_id,
                starter_username,
                starter_display_name,
                assigned_user_id,
                assigned_username,
                assigned_display_name,
                notes_text,
                now,
                now,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def get_pass_offer(path: str, offer_id: int) -> PassOffer | None:
    with _connect(path) as conn:
        row = conn.execute(
            f"{_PASS_OFFER_SELECT} WHERE id = ?",
            (offer_id,),
        ).fetchone()
    return _pass_offer_from_row(row) if row else None


def list_pending_pass_offers(path: str) -> list[PassOffer]:
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            {_PASS_OFFER_SELECT}
            WHERE status = 'pending'
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
    return [_pass_offer_from_row(row) for row in rows]


def pending_pass_assignee_user_ids(
    path: str,
    *,
    chat_id: int | None = None,
    exclude_offer_id: int | None = None,
) -> set[int]:
    """User ids that already have a pending take/brush pass offer."""
    clauses = ["status = 'pending'"]
    params: list[object] = []
    if chat_id is not None:
        clauses.append("chat_id = ?")
        params.append(chat_id)
    if exclude_offer_id is not None:
        clauses.append("id != ?")
        params.append(exclude_offer_id)
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT assigned_user_id
            FROM pass_offers
            WHERE {' AND '.join(clauses)}
            """,
            params,
        ).fetchall()
    return {int(row[0]) for row in rows}


def get_active_pass_offer(path: str, *, chat_id: int | None = None) -> PassOffer | None:
    """Return the oldest pending pass offer, if any."""
    clauses = ["status = 'pending'"]
    params: list[object] = []
    if chat_id is not None:
        clauses.append("chat_id = ?")
        params.append(chat_id)
    with _connect(path) as conn:
        row = conn.execute(
            f"""
            {_PASS_OFFER_SELECT}
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            params,
        ).fetchone()
    return _pass_offer_from_row(row) if row else None


def update_pass_offer(
    path: str,
    offer_id: int,
    *,
    offer_message_id: int | None = None,
    assigned_user_id: int | None = None,
    assigned_username: str | None = None,
    assigned_display_name: str | None = None,
    status: str | None = None,
    last_reminder_at: str | None = None,
    reset_reminder: bool = False,
) -> None:
    fields: list[str] = []
    params: list[object] = []
    if offer_message_id is not None:
        fields.append("offer_message_id = ?")
        params.append(offer_message_id)
    if assigned_user_id is not None:
        fields.append("assigned_user_id = ?")
        params.append(assigned_user_id)
    if assigned_username is not None:
        fields.append("assigned_username = ?")
        params.append(assigned_username)
    if assigned_display_name is not None:
        fields.append("assigned_display_name = ?")
        params.append(assigned_display_name)
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if reset_reminder:
        fields.append("last_reminder_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
    elif last_reminder_at is not None:
        fields.append("last_reminder_at = ?")
        params.append(last_reminder_at)
    if not fields:
        return
    params.append(offer_id)
    with _connect(path) as conn:
        conn.execute(
            f"UPDATE pass_offers SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        conn.commit()


def add_credo_whitelist_user(
    path: str,
    *,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str | None,
) -> None:
    added_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO credo_whitelist (telegram_user_id, telegram_username, display_name, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                telegram_username = excluded.telegram_username,
                display_name = excluded.display_name
            """,
            (telegram_user_id, telegram_username, display_name, added_at),
        )
        conn.commit()


def remove_credo_whitelist_user(path: str, telegram_user_id: int) -> bool:
    with _connect(path) as conn:
        cursor = conn.execute(
            "DELETE FROM credo_whitelist WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def list_credo_whitelist(path: str) -> list[CredoWhitelistUser]:
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT telegram_user_id, telegram_username, display_name, added_at
            FROM credo_whitelist
            ORDER BY added_at ASC
            """
        ).fetchall()
    return [
        CredoWhitelistUser(
            telegram_user_id=row[0],
            telegram_username=row[1],
            display_name=row[2],
            added_at=row[3],
        )
        for row in rows
    ]


def is_on_credo_whitelist(path: str, telegram_user_id: int) -> bool:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM credo_whitelist WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
    return row is not None


def upsert_credo_credit_card(
    path: str,
    name: str,
    photo_file_id: str,
    *,
    capacity: float = 0,
    logo_file_id: str | None = None,
    card_last4: str | None = None,
) -> None:
    cleaned = name.strip()
    if not cleaned or not photo_file_id.strip():
        raise ValueError("name and photo_file_id required")
    if capacity < 0:
        raise ValueError("capacity must be zero or positive")
    last4_clean: str | None = None
    if card_last4 is not None:
        last4_clean = card_last4.strip()
        if last4_clean and not last4_clean.isdigit():
            raise ValueError("card_last4 must be four digits")
        if last4_clean and len(last4_clean) != 4:
            raise ValueError("card_last4 must be four digits")
        if not last4_clean:
            last4_clean = None
    added_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO credo_credit_cards (
                name, photo_file_id, logo_file_id, capacity, added_at, card_last4
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                photo_file_id = excluded.photo_file_id,
                capacity = excluded.capacity,
                added_at = excluded.added_at,
                logo_file_id = COALESCE(excluded.logo_file_id, credo_credit_cards.logo_file_id),
                card_last4 = COALESCE(excluded.card_last4, credo_credit_cards.card_last4)
            """,
            (cleaned, photo_file_id, logo_file_id, capacity, added_at, last4_clean),
        )
        conn.commit()


def remove_credo_credit_card(path: str, name: str) -> bool:
    cleaned = name.strip()
    with _connect(path) as conn:
        cursor = conn.execute(
            "DELETE FROM credo_credit_cards WHERE name = ? COLLATE NOCASE",
            (cleaned,),
        )
        conn.commit()
        return cursor.rowcount > 0


def list_credo_credit_cards(path: str) -> list[CredoCreditCard]:
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT name, photo_file_id, logo_file_id, COALESCE(capacity, 0), added_at,
                   card_last4
            FROM credo_credit_cards
            ORDER BY added_at ASC, name ASC
            """
        ).fetchall()
    return [
        CredoCreditCard(
            name=row[0],
            photo_file_id=row[1],
            logo_file_id=row[2],
            capacity=float(row[3] or 0),
            added_at=row[4],
            card_last4=row[5] if len(row) > 5 else None,
        )
        for row in rows
    ]


def get_credo_credit_card(path: str, name: str) -> CredoCreditCard | None:
    cleaned = name.strip()
    if not cleaned:
        return None
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT name, photo_file_id, logo_file_id, COALESCE(capacity, 0), added_at,
                   card_last4
            FROM credo_credit_cards
            WHERE name = ? COLLATE NOCASE
            """,
            (cleaned,),
        ).fetchone()
    if row is None:
        return None
    return CredoCreditCard(
        name=row[0],
        photo_file_id=row[1],
        logo_file_id=row[2],
        capacity=float(row[3] or 0),
        added_at=row[4],
        card_last4=row[5] if len(row) > 5 else None,
    )


def get_credo_credit_card_by_last4(path: str, last4: str) -> CredoCreditCard | None:
    cleaned = last4.strip()
    if len(cleaned) != 4 or not cleaned.isdigit():
        return None
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT name, photo_file_id, logo_file_id, COALESCE(capacity, 0), added_at,
                   card_last4
            FROM credo_credit_cards
            WHERE card_last4 = ?
            ORDER BY added_at ASC, name ASC
            LIMIT 1
            """,
            (cleaned,),
        ).fetchone()
    if row is None:
        return None
    return CredoCreditCard(
        name=row[0],
        photo_file_id=row[1],
        logo_file_id=row[2],
        capacity=float(row[3] or 0),
        added_at=row[4],
        card_last4=row[5] if len(row) > 5 else None,
    )


def sum_credo_card_usage(path: str, card_name: str) -> float:
    cleaned = card_name.strip()
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM credo_card_usage
            WHERE card_name = ? COLLATE NOCASE
            """,
            (cleaned,),
        ).fetchone()
    return float(row[0] or 0)


def count_credo_card_usage_entries(path: str, card_name: str) -> int:
    cleaned = card_name.strip()
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM credo_card_usage
            WHERE card_name = ? COLLATE NOCASE
            """,
            (cleaned,),
        ).fetchone()
    return int(row[0] or 0)


def record_credo_card_usage(
    path: str,
    *,
    card_name: str,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str | None,
    amount: float,
    source_payment_id: int | None = None,
) -> int | None:
    if amount <= 0:
        raise ValueError("amount must be positive")
    created_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        if source_payment_id is not None:
            existing = conn.execute(
                """
                SELECT 1 FROM credo_card_usage WHERE source_payment_id = ?
                """,
                (source_payment_id,),
            ).fetchone()
            if existing is not None:
                return None
        cursor = conn.execute(
            """
            INSERT INTO credo_card_usage (
                card_name, telegram_user_id, telegram_username, display_name,
                amount, created_at, source_payment_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_name.strip(),
                telegram_user_id,
                telegram_username,
                display_name,
                amount,
                created_at,
                source_payment_id,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def list_credo_card_usage(path: str, card_name: str, *, limit: int = 20) -> list[CredoCardUsage]:
    cleaned = card_name.strip()
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT id, card_name, telegram_user_id, telegram_username, display_name,
                   amount, created_at
            FROM credo_card_usage
            WHERE card_name = ? COLLATE NOCASE
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (cleaned, limit),
        ).fetchall()
    return [
        CredoCardUsage(
            id=row[0],
            card_name=row[1],
            telegram_user_id=row[2],
            telegram_username=row[3],
            display_name=row[4],
            amount=float(row[5]),
            created_at=row[6],
        )
        for row in rows
    ]


def save_credo_profile(
    path: str,
    *,
    name: str,
    photo_file_id: str,
    created_by_user_id: int,
    created_by_username: str | None,
) -> int:
    created_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO credo_profiles (name, photo_file_id, created_by_user_id, created_by_username, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, photo_file_id, created_by_user_id, created_by_username, created_at),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _normalize_blacklist_username(username: str) -> str:
    return username.strip().lstrip("@").lower()


def add_chat_blacklist(
    path: str,
    *,
    chat_id: int,
    telegram_username: str,
    telegram_user_id: int | None = None,
    display_name: str | None = None,
    reason: str | None = None,
    blocked_by_user_id: int | None = None,
    blocked_by_username: str | None = None,
) -> str:
    """Add or update chat blacklist. Returns 'added', 'updated', or 'unchanged'."""
    normalized = _normalize_blacklist_username(telegram_username)
    if not normalized:
        raise ValueError("username required")
    added_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        existing = conn.execute(
            """
            SELECT telegram_user_id, reason FROM chat_blacklist
            WHERE chat_id = ? AND telegram_username = ?
            """,
            (chat_id, normalized),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO chat_blacklist (
                    chat_id, telegram_username, telegram_user_id, display_name,
                    reason, blocked_by_user_id, blocked_by_username, added_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    normalized,
                    telegram_user_id,
                    display_name,
                    reason,
                    blocked_by_user_id,
                    blocked_by_username,
                    added_at,
                ),
            )
            conn.commit()
            return "added"
        merged_user_id = telegram_user_id or existing[0]
        conn.execute(
            """
            UPDATE chat_blacklist SET
                telegram_user_id = ?,
                display_name = COALESCE(?, display_name),
                reason = ?,
                blocked_by_user_id = ?,
                blocked_by_username = ?,
                added_at = ?
            WHERE chat_id = ? AND telegram_username = ?
            """,
            (
                merged_user_id,
                display_name,
                reason,
                blocked_by_user_id,
                blocked_by_username,
                added_at,
                chat_id,
                normalized,
            ),
        )
        conn.commit()
        if reason == existing[1] and merged_user_id == existing[0]:
            return "unchanged"
        return "updated"


def get_chat_blacklist_entry(
    path: str,
    chat_id: int,
    *,
    telegram_username: str | None = None,
    telegram_user_id: int | None = None,
) -> ChatBlacklistEntry | None:
    with _connect(path) as conn:
        if telegram_user_id is not None:
            row = conn.execute(
                """
                SELECT chat_id, telegram_username, telegram_user_id, display_name,
                       reason, blocked_by_user_id, blocked_by_username, added_at
                FROM chat_blacklist
                WHERE chat_id = ? AND telegram_user_id = ?
                LIMIT 1
                """,
                (chat_id, telegram_user_id),
            ).fetchone()
        elif telegram_username:
            row = conn.execute(
                """
                SELECT chat_id, telegram_username, telegram_user_id, display_name,
                       reason, blocked_by_user_id, blocked_by_username, added_at
                FROM chat_blacklist
                WHERE chat_id = ? AND telegram_username = ?
                LIMIT 1
                """,
                (chat_id, _normalize_blacklist_username(telegram_username)),
            ).fetchone()
        else:
            return None
    if row is None:
        return None
    return ChatBlacklistEntry(
        chat_id=row[0],
        telegram_username=row[1],
        telegram_user_id=row[2],
        display_name=row[3],
        reason=row[4],
        blocked_by_user_id=row[5],
        blocked_by_username=row[6],
        added_at=row[7],
    )


def remove_chat_blacklist(
    path: str,
    *,
    chat_id: int,
    telegram_username: str | None = None,
    telegram_user_id: int | None = None,
) -> bool:
    with _connect(path) as conn:
        if telegram_user_id is not None:
            cursor = conn.execute(
                """
                DELETE FROM chat_blacklist
                WHERE chat_id = ? AND telegram_user_id = ?
                """,
                (chat_id, telegram_user_id),
            )
        elif telegram_username:
            cursor = conn.execute(
                """
                DELETE FROM chat_blacklist
                WHERE chat_id = ? AND telegram_username = ?
                """,
                (chat_id, _normalize_blacklist_username(telegram_username)),
            )
        else:
            return False
        conn.commit()
        return cursor.rowcount > 0


def list_chat_blacklist(path: str, chat_id: int) -> list[ChatBlacklistEntry]:
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT chat_id, telegram_username, telegram_user_id, display_name,
                   reason, blocked_by_user_id, blocked_by_username, added_at
            FROM chat_blacklist
            WHERE chat_id = ?
            ORDER BY added_at ASC
            """,
            (chat_id,),
        ).fetchall()
    return [
        ChatBlacklistEntry(
            chat_id=row[0],
            telegram_username=row[1],
            telegram_user_id=row[2],
            display_name=row[3],
            reason=row[4],
            blocked_by_user_id=row[5],
            blocked_by_username=row[6],
            added_at=row[7],
        )
        for row in rows
    ]


def is_chat_blacklisted(
    path: str,
    chat_id: int,
    *,
    telegram_user_id: int | None = None,
    telegram_username: str | None = None,
) -> bool:
    with _connect(path) as conn:
        if telegram_user_id is not None:
            row = conn.execute(
                """
                SELECT 1 FROM chat_blacklist
                WHERE chat_id = ? AND telegram_user_id = ?
                LIMIT 1
                """,
                (chat_id, telegram_user_id),
            ).fetchone()
            if row is not None:
                return True
        if telegram_username:
            normalized = _normalize_blacklist_username(telegram_username)
            if normalized:
                row = conn.execute(
                    """
                    SELECT 1 FROM chat_blacklist
                    WHERE chat_id = ? AND telegram_username = ?
                    LIMIT 1
                    """,
                    (chat_id, normalized),
                ).fetchone()
                if row is not None:
                    return True
    return False


def is_q1_premium_user(
    path: str,
    env_user_ids: frozenset[int],
    telegram_user_id: int,
) -> bool:
    if telegram_user_id in env_user_ids:
        return True
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM q1_premium_users WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
    return row is not None


def add_q1_premium_user(
    path: str,
    *,
    telegram_user_id: int,
    telegram_username: str | None,
    display_name: str | None,
) -> None:
    added_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO q1_premium_users (telegram_user_id, telegram_username, display_name, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                telegram_username = excluded.telegram_username,
                display_name = excluded.display_name
            """,
            (telegram_user_id, telegram_username, display_name, added_at),
        )
        conn.commit()


def remove_q1_premium_user(path: str, telegram_user_id: int) -> bool:
    with _connect(path) as conn:
        cursor = conn.execute(
            "DELETE FROM q1_premium_users WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def list_q1_premium_users(path: str) -> list[Q1PremiumUser]:
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT telegram_user_id, telegram_username, display_name, added_at
            FROM q1_premium_users
            ORDER BY added_at ASC
            """
        ).fetchall()
    return [
        Q1PremiumUser(
            telegram_user_id=row[0],
            telegram_username=row[1],
            display_name=row[2],
            added_at=row[3],
        )
        for row in rows
    ]


def get_user_handle_baseline(
    path: str,
    telegram_user_id: int,
    *,
    min_calls: int = 5,
    exclude_latest: bool = False,
) -> tuple[float, int] | None:
    if exclude_latest:
        query = """
            SELECT AVG(duration_seconds), COUNT(*)
            FROM completed_calls
            WHERE telegram_user_id = ?
              AND id != (
                SELECT id FROM completed_calls
                WHERE telegram_user_id = ?
                ORDER BY ended_at DESC, id DESC
                LIMIT 1
              )
        """
        params = (telegram_user_id, telegram_user_id)
    else:
        query = """
            SELECT AVG(duration_seconds), COUNT(*)
            FROM completed_calls
            WHERE telegram_user_id = ?
        """
        params = (telegram_user_id,)
    with _connect(path) as conn:
        row = conn.execute(query, params).fetchone()
    if row is None or row[1] is None or int(row[1]) < min_calls:
        return None
    return float(row[0]), int(row[1])


def get_user_close_rates(
    path: str,
    telegram_user_id: int,
    *,
    since: datetime | None,
    until: datetime | None,
) -> tuple[int, int, float]:
    call_clauses = ["telegram_user_id = ?"]
    pay_clauses = ["telegram_user_id = ?"]
    call_params: list[object] = [telegram_user_id]
    pay_params: list[object] = [telegram_user_id]
    if since is not None:
        call_clauses.append("ended_at >= ?")
        pay_clauses.append("created_at >= ?")
        call_params.append(since.isoformat())
        pay_params.append(since.isoformat())
    if until is not None:
        call_clauses.append("ended_at < ?")
        pay_clauses.append("created_at < ?")
        call_params.append(until.isoformat())
        pay_params.append(until.isoformat())
    call_where = " AND ".join(call_clauses)
    pay_where = " AND ".join(pay_clauses)
    with _connect(path) as conn:
        call_row = conn.execute(
            f"SELECT COUNT(*) FROM completed_calls WHERE {call_where}",
            call_params,
        ).fetchone()
        pay_row = conn.execute(
            f"SELECT COUNT(*) FROM payment_outs WHERE {pay_where}",
            pay_params,
        ).fetchone()
    calls = int(call_row[0]) if call_row else 0
    payments = int(pay_row[0]) if pay_row else 0
    rate = payments / calls if calls else 0.0
    return calls, payments, rate


def log_quiet_win(
    path: str,
    *,
    telegram_user_id: int,
    win_type: str,
    detail: str = "",
) -> None:
    created_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO quiet_win_log (telegram_user_id, win_type, detail, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (telegram_user_id, win_type, detail, created_at),
        )
        conn.commit()


def recent_quiet_win(
    path: str,
    telegram_user_id: int,
    win_type: str,
    *,
    within_hours: int = 24,
    within_minutes: int | None = None,
) -> bool:
    if within_minutes is not None:
        since = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM quiet_win_log
            WHERE telegram_user_id = ? AND win_type = ? AND created_at >= ?
            LIMIT 1
            """,
            (telegram_user_id, win_type, since.isoformat()),
        ).fetchone()
    return row is not None


def get_latest_quiet_win_detail(
    path: str,
    telegram_user_id: int,
    win_type: str,
) -> str | None:
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT detail FROM quiet_win_log
            WHERE telegram_user_id = ? AND win_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (telegram_user_id, win_type),
        ).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def _local_stats_date_iso() -> str:
    from handlers.stats_period import stats_timezone

    return datetime.now(stats_timezone()).date().isoformat()


def ready_check_sent_today(path: str, telegram_user_id: int) -> bool:
    local_date = _local_stats_date_iso()
    with _connect(path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM ready_check_sent
            WHERE telegram_user_id = ? AND local_date = ?
            """,
            (telegram_user_id, local_date),
        ).fetchone()
    return row is not None


def mark_ready_check_sent(path: str, telegram_user_id: int) -> None:
    local_date = _local_stats_date_iso()
    sent_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO ready_check_sent (telegram_user_id, local_date, sent_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_user_id, local_date) DO NOTHING
            """,
            (telegram_user_id, local_date, sent_at),
        )
        conn.commit()


def record_missed_call(
    path: str,
    *,
    extension: str,
    telegram_user_id: int,
    telegram_username: str | None = None,
    display_name: str | None = None,
    caller_name: str = "",
    caller_number: str = "",
    callid: int | None = None,
    ring_seconds: int = 0,
    source: str = "3cx",
) -> bool:
    missed_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        try:
            conn.execute(
                """
                INSERT INTO missed_calls (
                    extension, telegram_user_id, telegram_username, display_name,
                    caller_name, caller_number, callid, ring_seconds, missed_at, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    extension,
                    telegram_user_id,
                    telegram_username,
                    display_name,
                    caller_name or "",
                    caller_number or "",
                    callid,
                    max(0, ring_seconds),
                    missed_at,
                    source,
                ),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def list_missed_calls(
    path: str,
    *,
    since: datetime | None = None,
    limit: int = 50,
) -> list[MissedCall]:
    limit = max(1, min(limit, 200))
    clauses: list[str] = []
    params: list[object] = []
    if since is not None:
        clauses.append("missed_at >= ?")
        params.append(since.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with _connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, extension, telegram_user_id, telegram_username, display_name,
                   caller_name, caller_number, callid, ring_seconds, missed_at, source
            FROM missed_calls
            {where}
            ORDER BY missed_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_row_to_missed_call(row) for row in rows]


def list_missed_calls_since(
    path: str,
    *,
    since: datetime | None = None,
) -> list[MissedCall]:
    clauses: list[str] = []
    params: list[object] = []
    if since is not None:
        clauses.append("missed_at >= ?")
        params.append(since.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, extension, telegram_user_id, telegram_username, display_name,
                   caller_name, caller_number, callid, ring_seconds, missed_at, source
            FROM missed_calls
            {where}
            ORDER BY missed_at ASC, id ASC
            """,
            params,
        ).fetchall()
    return [_row_to_missed_call(row) for row in rows]


def count_missed_calls(path: str, *, since: datetime | None = None) -> int:
    clauses: list[str] = []
    params: list[object] = []
    if since is not None:
        clauses.append("missed_at >= ?")
        params.append(since.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect(path) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM missed_calls {where}",
            params,
        ).fetchone()
    return int(row[0]) if row else 0


def _row_to_missed_call(row: sqlite3.Row) -> MissedCall:
    return MissedCall(
        id=row["id"],
        extension=row["extension"],
        telegram_user_id=row["telegram_user_id"],
        telegram_username=row["telegram_username"],
        display_name=row["display_name"],
        caller_name=row["caller_name"] or "",
        caller_number=row["caller_number"] or "",
        callid=row["callid"],
        ring_seconds=int(row["ring_seconds"] or 0),
        missed_at=row["missed_at"],
        source=row["source"] or "3cx",
    )


def insert_mailer_log(
    path: str,
    *,
    session_id: str,
    event_type: str,
    telegram_user_id: int,
    telegram_username: str | None = None,
    display_name: str | None = None,
    detail: str = "",
    recipient: str | None = None,
    destination: str | None = None,
    content: str | None = None,
) -> int:
    created_at = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO mailer_logs (
                session_id, event_type, telegram_user_id, telegram_username,
                display_name, detail, recipient, destination, content, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                event_type,
                telegram_user_id,
                telegram_username,
                display_name,
                detail,
                recipient,
                destination,
                content,
                created_at,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def list_mailer_logs(
    path: str,
    *,
    limit: int = 25,
    session_id: str | None = None,
    telegram_user_id: int | None = None,
) -> list[MailerLogEntry]:
    limit = max(1, min(limit, 200))
    clauses: list[str] = []
    params: list[object] = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if telegram_user_id is not None:
        clauses.append("telegram_user_id = ?")
        params.append(telegram_user_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with _connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, session_id, event_type, telegram_user_id, telegram_username,
                   display_name, detail, recipient, destination, content, created_at
            FROM mailer_logs
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_row_to_mailer_log(row) for row in rows]


def _row_to_mailer_log(row: sqlite3.Row) -> MailerLogEntry:
    return MailerLogEntry(
        id=row["id"],
        session_id=row["session_id"],
        event_type=row["event_type"],
        telegram_user_id=row["telegram_user_id"],
        telegram_username=row["telegram_username"],
        display_name=row["display_name"],
        detail=row["detail"] or "",
        recipient=row["recipient"],
        destination=row["destination"],
        content=row["content"],
        created_at=row["created_at"],
    )


@contextmanager
def _connect(path: str):
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


_DATA_TABLES = (
    "extension_links",
    "completed_calls",
    "bot_admins",
    "payment_outs",
    "credo_whitelist",
    "credo_profiles",
    "credo_credit_cards",
    "credo_card_usage",
    "chat_blacklist",
    "bot_settings",
    "mailer_logs",
    "missed_calls",
    "q1_premium_users",
    "quiet_win_log",
    "ready_check_sent",
    "payment_nemesis",
    "pass_queue",
    "pass_queue_vips",
    "pass_offers",
    "pass_notes_pending",
)


def summarize_bot_data(path: str) -> dict[str, int]:
    """Row counts for /panic confirmation summary."""
    stats: dict[str, int] = {}
    try:
        with _connect(path) as conn:
            for table in _DATA_TABLES:
                try:
                    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                    stats[table] = int(row[0] if row else 0)
                except sqlite3.Error:
                    stats[table] = 0
    except sqlite3.Error:
        pass
    return stats


def wipe_database_file(path: str) -> None:
    """Delete the SQLite file and sidecars, then recreate an empty schema."""
    db_path = Path(path)
    for candidate in (
        db_path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}.bot.lock"),
    ):
        candidate.unlink(missing_ok=True)
    init_db(path)
