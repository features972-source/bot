"""Shared expense table formatting for live expense reports."""

from __future__ import annotations

from database import ExpenseRecord, list_links
from handlers.payment_table import format_payment_date, sheet_user_label
from money_format import format_amount

EXPENSE_HEADERS = ("#", "Amount", "Date", "User", "Where")
LIVE_EXPENSE_ROW_LIMIT = 12


def build_expense_username_lookup(
    database_path: str,
    records: list[ExpenseRecord],
) -> dict[int, str]:
    lookup: dict[int, str] = {}
    for link in list_links(database_path):
        if link.telegram_username:
            lookup[link.telegram_user_id] = link.telegram_username.lstrip("@")
    for record in records:
        if record.telegram_username:
            lookup[record.telegram_user_id] = record.telegram_username.lstrip("@")
    return lookup


def expense_table_row(
    record: ExpenseRecord,
    *,
    username_lookup: dict[int, str],
    compact_names: bool = False,
) -> list[str]:
    return [
        f"#{record.id}",
        format_amount(record.amount),
        format_payment_date(record.created_at, compact=compact_names),
        sheet_user_label(
            record.telegram_username,
            record.display_name,
            record.telegram_user_id,
            username_lookup=username_lookup,
            compact=compact_names,
        ),
        record.reason,
    ]


def expense_totals_row(*, total_amount: float, total_count: int) -> list[str]:
    return [
        "TOTAL",
        format_amount(total_amount),
        "",
        f"{total_count} expense{'s' if total_count != 1 else ''}",
        "",
    ]


def format_expense_subtitle(period_label: str) -> str:
    text = period_label.strip()
    if text:
        text = text[0].upper() + text[1:]
    return f"{text} · new week every Sunday"


def format_expense_report_text(
    records: list[ExpenseRecord],
    *,
    database_path: str,
    total_amount: float,
    total_count: int,
    title: str,
    period_label: str,
) -> str:
    import html

    lookup = build_expense_username_lookup(database_path, records)
    lines = [
        f"<b>{html.escape(title)}</b>",
        f"<i>{html.escape(format_expense_subtitle(period_label))}</i>",
        "",
    ]
    for record in records:
        row = expense_table_row(record, username_lookup=lookup)
        lines.append(
            f"{html.escape(row[1])} · {html.escape(row[3])} → "
            f"{html.escape(row[4])} ({html.escape(row[0])})"
        )
    lines.extend(
        [
            "",
            f"<b>Total:</b> {html.escape(format_amount(total_amount))} "
            f"({total_count} expense{'s' if total_count != 1 else ''})",
        ]
    )
    return "\n".join(lines)
