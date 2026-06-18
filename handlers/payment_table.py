"""Shared Excel-style payment table for /payments and live payment reports."""

from __future__ import annotations

import html
from datetime import datetime, timezone

from database import PaymentRecord, list_links
from handlers.stats_period import stats_timezone
from money_format import format_amount

TABLE_HEADERS = (
    "Amount",
    "Date",
    "Starter",
    "Finisher",
    "Card",
    "Status",
)


def build_username_lookup(
    database_path: str,
    records: list[PaymentRecord],
) -> dict[int, str]:
    """Map telegram user id → username from links and payment rows."""
    lookup: dict[int, str] = {}
    for link in list_links(database_path):
        if link.telegram_username:
            lookup[link.telegram_user_id] = link.telegram_username.lstrip("@")
    for record in records:
        if record.finisher_username:
            lookup[record.finisher_user_id] = record.finisher_username.lstrip("@")
        if record.starter_user_id is not None and record.starter_username:
            lookup[record.starter_user_id] = record.starter_username.lstrip("@")
    return lookup


def user_at_label(
    username: str | None,
    display_name: str | None,
    user_id: int,
    *,
    username_lookup: dict[int, str] | None = None,
) -> str:
    if username:
        return f"@{username.lstrip('@')}"
    if username_lookup and user_id in username_lookup:
        return f"@{username_lookup[user_id]}"
    if display_name:
        return display_name
    return str(user_id)


def format_status_label(cleared: bool | None) -> str:
    """Plain status text for tables and images."""
    if cleared is None:
        return "Waiting"
    if cleared:
        return "Cleared"
    return "Not cleared"


def cleared_table_cell(cleared: bool | None) -> str:
    if cleared is None:
        return "🟧 Waiting"
    if cleared:
        return "🟩 Cleared"
    return "🟥 Not cleared"


def format_image_subtitle(period_label: str) -> str:
    text = period_label.strip()
    if text:
        text = text[0].upper() + text[1:]
    return f"{text} · new week every Sunday"


def format_status_summary(
    *,
    pending_amount: float,
    pending_count: int,
    cleared_amount: float,
    cleared_count: int,
    not_cleared_amount: float,
    not_cleared_count: int,
) -> str:
    return (
        f"Waiting: {format_amount(pending_amount)} ({pending_count})   "
        f"Cleared: {format_amount(cleared_amount)} ({cleared_count})   "
        f"Not cleared: {format_amount(not_cleared_amount)} ({not_cleared_count})"
    )


def format_image_footer(*, live: bool = False) -> str:
    from payments_excel_export import format_payment_sheet_updated_note

    stamp = format_payment_sheet_updated_note()
    if live:
        return f"{stamp} · updates automatically when payments change"
    return stamp


def format_payment_date(iso_timestamp: str) -> str:
    try:
        text = iso_timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return iso_timestamp
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(stats_timezone()).strftime("%d/%m/%Y")


def payment_table_row(
    record: PaymentRecord,
    *,
    username_lookup: dict[int, str],
) -> list[str]:
    starter = ""
    if record.starter_user_id is not None:
        starter = user_at_label(
            record.starter_username,
            record.starter_display_name,
            record.starter_user_id,
            username_lookup=username_lookup,
        )
    return [
        format_amount(record.amount),
        format_payment_date(record.created_at),
        starter,
        user_at_label(
            record.finisher_username,
            record.finisher_display_name,
            record.finisher_user_id,
            username_lookup=username_lookup,
        ),
        record.card_last4 or "—",
        format_status_label(record.cleared),
    ]


def payment_totals_table_row(
    *,
    total_amount: float,
    total_count: int,
) -> list[str]:
    count_label = f"{total_count} payment" + ("" if total_count == 1 else "s")
    return [
        "WEEK TOTAL",
        format_amount(total_amount),
        count_label,
        "",
        "",
        "",
    ]


def wrap_bold_table(table: str) -> str:
    """Monospace Excel-style grid, all bold."""
    return f"<b><pre>{html.escape(table)}</pre></b>"
