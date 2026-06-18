"""Shared Excel-style payment table for /payments and live payment reports."""

from __future__ import annotations

import html
from datetime import datetime, timezone

from database import PaymentRecord, list_links
from handlers.stats_period import stats_timezone
from money_format import format_amount

TABLE_HEADERS_COMPACT = (
    "#",
    "Amount",
    "Date",
    "Starter",
    "Finisher",
    "Card",
    "Cleared",
)

TABLE_HEADERS_FULL = (
    "#",
    "Amount",
    "Date",
    "Starter",
    "Finisher",
    "Card",
    "Cleared",
    "Paying Starter",
    "Paying Finisher",
    "Paying Centre",
)

TABLE_HEADERS = TABLE_HEADERS_FULL

PAYMENTS_PAGE_SIZE = 20


def table_headers(*, full_excel: bool = True) -> tuple[str, ...]:
    return TABLE_HEADERS_FULL if full_excel else TABLE_HEADERS_COMPACT


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


def sheet_user_label(
    username: str | None,
    display_name: str | None,
    user_id: int,
    *,
    username_lookup: dict[int, str] | None = None,
) -> str:
    """Match Excel export: display name first, then @username."""
    name = (display_name or "").strip()
    if name:
        return name
    if username:
        return f"@{username.lstrip('@')}"
    if username_lookup and user_id in username_lookup:
        return f"@{username_lookup[user_id]}"
    return str(user_id)


def user_at_label(
    username: str | None,
    display_name: str | None,
    user_id: int,
    *,
    username_lookup: dict[int, str] | None = None,
) -> str:
    return sheet_user_label(
        username,
        display_name,
        user_id,
        username_lookup=username_lookup,
    )


def format_status_label(cleared: bool | None) -> str:
    if cleared is None:
        return "Waiting"
    if cleared:
        return "Cleared"
    return "Not cleared"


def format_cleared_label(cleared: bool | None) -> str:
    if cleared is None:
        return "Pending"
    if cleared:
        return "Yes"
    return "No"


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


def status_summary_totals(
    *,
    pending_amount: float,
    pending_count: int,
    cleared_amount: float,
    cleared_count: int,
    not_cleared_amount: float,
    not_cleared_count: int,
) -> tuple[float, int, float, int, float, int]:
    return (
        pending_amount,
        pending_count,
        cleared_amount,
        cleared_count,
        not_cleared_amount,
        not_cleared_count,
    )


def format_image_footer(*, live: bool = False) -> str:
    from payments_excel_export import payment_sheet_footer_note

    note = payment_sheet_footer_note()
    if live:
        return f"{note} · updates automatically when payments change"
    return note


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
    full_excel: bool = True,
) -> list[str]:
    starter = ""
    if record.starter_user_id is not None:
        starter = sheet_user_label(
            record.starter_username,
            record.starter_display_name,
            record.starter_user_id,
            username_lookup=username_lookup,
        )
    row = [
        f"#{record.id}",
        format_amount(record.amount),
        format_payment_date(record.created_at),
        starter,
        sheet_user_label(
            record.finisher_username,
            record.finisher_display_name,
            record.finisher_user_id,
            username_lookup=username_lookup,
        ),
        record.card_last4 or "",
    ]
    if not full_excel:
        return row + [format_cleared_label(record.cleared)]

    from payments_excel_export import centre_payout, finisher_payout, starter_payout

    starter_pay = starter_payout(record)
    return row + [
        format_cleared_label(record.cleared),
        format_amount(starter_pay) if starter_pay else "",
        format_amount(finisher_payout(record)),
        format_amount(centre_payout(record)),
    ]


def payment_totals_table_row(
    *,
    total_amount: float,
    total_count: int,
    records: list[PaymentRecord] | None = None,
    full_excel: bool = True,
    total_label: str = "TOTAL",
) -> list[str]:
    count_label = f"{total_count} payment" + ("" if total_count == 1 else "s")
    if not full_excel:
        return [
            "",
            f"{total_label}  {format_amount(total_amount)}",
            "",
            count_label,
            "",
            "",
            "",
        ]

    from payments_excel_export import centre_payout, finisher_payout, starter_payout

    recs = records or []
    return [
        "",
        total_label,
        format_amount(total_amount),
        count_label,
        "",
        "",
        "",
        format_amount(sum(starter_payout(record) for record in recs)),
        format_amount(sum(finisher_payout(record) for record in recs)),
        format_amount(sum(centre_payout(record) for record in recs)),
    ]


def wrap_bold_table(table: str) -> str:
    """Monospace Excel-style grid, all bold."""
    return f"<b><pre>{html.escape(table)}</pre></b>"
