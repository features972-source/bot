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

PAYMENTS_PAGE_SIZE = 10
LIVE_REPORT_ROW_LIMIT = 12


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
    compact: bool = False,
) -> str:
    """Match Excel export: display name first, then @username."""
    if compact and username:
        return f"@{username.lstrip('@')}"
    name = (display_name or "").strip()
    if name:
        if compact and " " in name:
            return name.split()[0]
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

    if live:
        now_local = datetime.now(timezone.utc).astimezone(stats_timezone())
        stamp = now_local.strftime(f"{now_local.day} %b %H:%M")
        return f"Updated {stamp} · auto-updates"
    return payment_sheet_footer_note()


def format_payment_date(iso_timestamp: str, *, compact: bool = False) -> str:
    try:
        text = iso_timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return iso_timestamp
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(stats_timezone())
    if compact:
        return local.strftime("%d/%m/%y")
    return local.strftime("%d/%m/%Y")


def payment_table_row(
    record: PaymentRecord,
    *,
    username_lookup: dict[int, str],
    full_excel: bool = True,
    compact_names: bool = False,
) -> list[str]:
    starter = ""
    if record.starter_user_id is not None:
        starter = sheet_user_label(
            record.starter_username,
            record.starter_display_name,
            record.starter_user_id,
            username_lookup=username_lookup,
            compact=compact_names,
        )
    row = [
        f"#{record.id}",
        format_amount(record.amount),
        format_payment_date(record.created_at, compact=compact_names),
        starter,
        sheet_user_label(
            record.finisher_username,
            record.finisher_display_name,
            record.finisher_user_id,
            username_lookup=username_lookup,
            compact=compact_names,
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


def _align_table_rows(
    headers: tuple[str, ...],
    rows: list[list[str]],
    *,
    min_widths: tuple[int, ...] | None = None,
    max_widths: tuple[int, ...] | None = None,
    right_align: frozenset[int] | None = None,
) -> str:
    """Fixed-width monospace rows so columns line up in Telegram <pre> blocks."""
    col_count = len(headers)
    min_widths = min_widths or (0,) * col_count
    max_widths = max_widths or (999,) * col_count
    right_align = right_align or frozenset()

    def _cells(row: list[str]) -> list[str]:
        return [(row[i] if i < len(row) else "") for i in range(col_count)]

    all_rows = [_cells(list(headers)), *[_cells(row) for row in rows]]
    widths = [0] * col_count
    for row in all_rows:
        for i, cell in enumerate(row):
            clipped = cell[: max_widths[i]]
            widths[i] = max(widths[i], len(clipped))

    for i in range(col_count):
        cap = max_widths[i] if max_widths[i] < 999 else widths[i]
        widths[i] = max(min_widths[i], min(widths[i], cap))

    def _format_line(cells: list[str]) -> str:
        parts: list[str] = []
        for i, cell in enumerate(cells):
            clipped = cell[: widths[i]]
            if i in right_align:
                parts.append(clipped.rjust(widths[i]))
            else:
                parts.append(clipped.ljust(widths[i]))
        return " | ".join(parts)

    header = _format_line(all_rows[0])
    rule = "-+-".join("-" * widths[i] for i in range(col_count))
    body = [_format_line(row) for row in all_rows[1:]]
    return "\n".join([header, rule, *body])


def render_payments_table_text(
    records: list[PaymentRecord],
    *,
    database_path: str,
    total_amount: float,
    total_count: int,
    lookup_records: list[PaymentRecord] | None = None,
    totals_records: list[PaymentRecord] | None = None,
    full_excel: bool = False,
    total_label: str = "TOTAL",
) -> str:
    """Plain-text table for /payments and /alltimepayments."""
    username_lookup = build_username_lookup(
        database_path,
        lookup_records if lookup_records is not None else records,
    )
    headers = table_headers(full_excel=full_excel)
    body = [
        payment_table_row(
            record,
            username_lookup=username_lookup,
            full_excel=full_excel,
            compact_names=not full_excel,
        )
        for record in records
    ]
    totals = payment_totals_table_row(
        total_amount=total_amount,
        total_count=total_count,
        records=totals_records if totals_records is not None else records,
        full_excel=full_excel,
        total_label=total_label,
    )

    if full_excel:
        min_widths = (4, 10, 8, 14, 14, 4, 7, 10, 10, 10)
        max_widths = (5, 14, 8, 16, 16, 4, 9, 12, 12, 12)
        right_align = frozenset({1, 7, 8, 9})
    else:
        min_widths = (4, 9, 8, 12, 12, 4, 7)
        max_widths = (5, 13, 8, 15, 15, 4, 9)
        right_align = frozenset({1})

    return _align_table_rows(
        headers,
        [*body, totals],
        min_widths=min_widths,
        max_widths=max_widths,
        right_align=right_align,
    )


def format_payment_date_readable(iso_timestamp: str) -> str:
    """Short friendly date for phone screens, e.g. 18 Jun 26."""
    try:
        text = iso_timestamp.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except ValueError:
        return iso_timestamp
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(stats_timezone())
    return local.strftime("%d %b %y")


def format_status_summary_mobile(
    *,
    pending_amount: float,
    pending_count: int,
    cleared_amount: float,
    cleared_count: int,
    not_cleared_amount: float,
    not_cleared_count: int,
) -> str:
    """Plain-text status lines (legacy); prefer render_payments_status_html."""
    lines = [
        f"🟧 Waiting — {format_amount(pending_amount)} ({pending_count})",
        f"🟩 Cleared — {format_amount(cleared_amount)} ({cleared_count})",
        f"🟥 Not cleared — {format_amount(not_cleared_amount)} ({not_cleared_count})",
    ]
    return "\n".join(lines)


def render_payments_status_html(
    *,
    pending_amount: float,
    pending_count: int,
    cleared_amount: float,
    cleared_count: int,
    not_cleared_amount: float,
    not_cleared_count: int,
) -> str:
    """Status breakdown with labels for the payments footer."""
    rows = [
        ("🟧 Waiting", format_amount(pending_amount), pending_count),
        ("🟩 Cleared", format_amount(cleared_amount), cleared_count),
        ("🟥 Not cleared", format_amount(not_cleared_amount), not_cleared_count),
    ]
    lines = ["<b>By status</b>"]
    for label, amount, count in rows:
        lines.append(
            f"{label} — <b>{html.escape(amount)}</b> "
            f"({html.escape(str(count))})"
        )
    return "\n".join(lines)


def _mobile_labeled_line(label: str, value: str) -> str:
    return f"<i>{html.escape(label)}</i>  {html.escape(value)}"


def render_payments_mobile_html(
    records: list[PaymentRecord],
    *,
    database_path: str,
    total_amount: float,
    total_count: int,
    lookup_records: list[PaymentRecord] | None = None,
    status_html: str | None = None,
    total_label: str = "Total",
) -> str:
    """Card-style payment list for Telegram on narrow screens (no <pre>)."""
    username_lookup = build_username_lookup(
        database_path,
        lookup_records if lookup_records is not None else records,
    )
    blocks: list[str] = []
    for index, record in enumerate(records):
        if index > 0:
            blocks.append("")
            blocks.append("────────────")
            blocks.append("")

        if record.starter_user_id is not None:
            starter = sheet_user_label(
                record.starter_username,
                record.starter_display_name,
                record.starter_user_id,
                username_lookup=username_lookup,
                compact=True,
            )
        else:
            starter = "—"
        finisher = sheet_user_label(
            record.finisher_username,
            record.finisher_display_name,
            record.finisher_user_id,
            username_lookup=username_lookup,
            compact=True,
        )
        card = f"····{record.card_last4}" if record.card_last4 else "—"
        amount = format_amount(record.amount)
        date = format_payment_date_readable(record.created_at)
        status = cleared_table_cell(record.cleared)

        blocks.append(f"<b>{html.escape(amount)}</b>")
        blocks.append(
            f"<i>#{record.id}</i>  ·  <i>{html.escape(date)}</i>"
        )
        blocks.append(_mobile_labeled_line("Starter", starter))
        blocks.append(_mobile_labeled_line("Finisher", finisher))
        blocks.append(_mobile_labeled_line("Card", card))
        blocks.append(f"<i>Status</i>  {status}")

    count_label = f"{total_count} payment" + ("" if total_count == 1 else "s")
    blocks.append("")
    blocks.append("━━━━━━━━━━━━")
    blocks.append(
        f"<b>{html.escape(total_label)}: {html.escape(format_amount(total_amount))}</b>"
    )
    blocks.append(f"<i>{html.escape(count_label)}</i>")
    if status_html:
        blocks.append("")
        blocks.append(status_html)
    return "\n".join(blocks)
