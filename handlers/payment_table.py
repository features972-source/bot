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
    "Cleared",
)
TABLE_WIDTHS = (12, 12, 18, 18, 6, 9)
_COLUMN_SEP = " │ "


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


def cleared_table_cell(cleared: bool | None) -> str:
    if cleared is None:
        return "🟧 Pending"
    if cleared:
        return "🟩 Yes"
    return "🟥 No"


def format_payment_date(iso_timestamp: str) -> str:
    try:
        text = iso_timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return iso_timestamp
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(stats_timezone()).strftime("%d/%m/%Y")


def _fit_cell(value: str, width: int) -> str:
    text = value or ""
    if len(text) <= width:
        return text.ljust(width)
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def format_table_row(cells: list[str]) -> str:
    padded = [
        _fit_cell(cells[i] if i < len(cells) else "", TABLE_WIDTHS[i])
        for i in range(len(TABLE_WIDTHS))
    ]
    return _COLUMN_SEP.join(padded).rstrip()


def format_table_divider() -> str:
    segments = ["─" * width for width in TABLE_WIDTHS]
    return "─┼─".join(segments)


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
        record.card_last4 or "",
        cleared_table_cell(record.cleared),
    ]


def payment_totals_table_row(
    *,
    total_amount: float,
    total_count: int,
) -> list[str]:
    count_label = f"{total_count} payment" + ("" if total_count == 1 else "s")
    return [
        "TOTAL",
        format_amount(total_amount),
        count_label,
        "",
        "",
        "",
    ]


def format_payments_table(
    records: list[PaymentRecord],
    *,
    totals_row: list[str],
    database_path: str,
    lookup_records: list[PaymentRecord] | None = None,
    hidden_count: int = 0,
    hidden_suffix: str = "live list has full detail",
) -> str:
    username_lookup = build_username_lookup(
        database_path,
        lookup_records if lookup_records is not None else records,
    )
    lines = [
        format_table_row(list(TABLE_HEADERS)),
        format_table_divider(),
    ]
    lines.extend(
        format_table_row(payment_table_row(record, username_lookup=username_lookup))
        for record in records
    )
    lines.append(format_table_divider())
    lines.append(format_table_row(totals_row))
    if hidden_count > 0:
        lines.append(
            f"… +{hidden_count} more payment"
            f"{'' if hidden_count == 1 else 's'} ({hidden_suffix})"
        )
    return "\n".join(lines)


def wrap_bold_table(table: str) -> str:
    """Monospace Excel-style grid, all bold."""
    return f"<b><pre>{html.escape(table)}</pre></b>"
