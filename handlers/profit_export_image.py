"""Render profit export PNG (jobs, payouts, expenses, per-user spend)."""

from __future__ import annotations

from io import BytesIO

from telegram import InputFile

from database import ExpenseSpendingEntry, list_links
from handlers.payment_table import format_image_footer, sheet_user_label
from handlers.payment_table_image import (
    _BG,
    _BODY_SIZE,
    _BODY_TEXT,
    _GRID,
    _HEADER_BG,
    _HEADER_TEXT,
    _ID_TEXT,
    _OUTPUT_INNER_WIDTH,
    _PAD,
    _ROW_H,
    _SHEET_BG,
    _SHEET_BORDER,
    _STAMP_SIZE,
    _STAMP_TEXT,
    _SUPERSAMPLE,
    _TITLE_SIZE,
    _TOTAL_BG,
    _TOTAL_TEXT,
    _cached_font,
    _fit_header_columns,
    _measure_col_widths,
    _s,
    _ss,
)
from handlers.profit_export import ProfitExportSummary
from money_format import format_amount
from payments_excel_export import (
    CENTRE_PAY_PERCENT,
    FINISHER_PAY_PERCENT,
    STARTER_PAY_PERCENT,
)

_HEADERS = ("", "Value", "Detail")
_MIN_COL_WIDTHS = (140, 100, 120)


def profit_export_input_file(data: bytes, *, filename: str = "export.png") -> InputFile:
    return InputFile(data, filename=filename)


def _export_title(bot_display_name: str) -> str:
    name = (bot_display_name or "Export").strip()
    if "export" in name.lower():
        return name
    return f"{name} Export"


def _expense_user_label(
    entry: ExpenseSpendingEntry, *, lookup: dict[int, str]
) -> str:
    return sheet_user_label(
        entry.telegram_username,
        entry.display_name,
        entry.user_id,
        username_lookup=lookup,
        compact=True,
    )


def render_profit_export_png(
    summary: ProfitExportSummary,
    *,
    database_path: str,
    bot_display_name: str,
) -> bytes:
    from PIL import Image, ImageDraw

    title = _export_title(bot_display_name)
    subtitle = summary.period_label.strip()
    if subtitle:
        subtitle = subtitle[0].upper() + subtitle[1:]

    summary_rows = [
        ["Gross jobs", format_amount(summary.gross), f"{summary.payment_count} payments"],
        [f"Starter ({STARTER_PAY_PERCENT}%)", format_amount(summary.starter_pay), ""],
        [f"Finisher ({FINISHER_PAY_PERCENT}%)", format_amount(summary.finisher_pay), ""],
        [f"Centre ({CENTRE_PAY_PERCENT}%)", format_amount(summary.centre_pay), "our share"],
        ["Total expenses", format_amount(summary.expense_total), f"{summary.expense_count} items"],
        [
            "Net profit",
            format_amount(summary.net_profit),
            "centre − expenses",
        ],
    ]

    lookup: dict[int, str] = {}
    for link in list_links(database_path):
        if link.telegram_username:
            lookup[link.telegram_user_id] = link.telegram_username.lstrip("@")
    for entry in summary.expense_by_user:
        if entry.telegram_username:
            lookup[entry.user_id] = entry.telegram_username.lstrip("@")

    expense_rows: list[list[str]] = []
    if summary.expense_by_user:
        expense_rows.append(["— Per-user expenses —", "", ""])
        for entry in summary.expense_by_user:
            expense_rows.append(
                [
                    _expense_user_label(entry, lookup=lookup),
                    format_amount(entry.total_amount),
                    f"{entry.expense_count} item{'s' if entry.expense_count != 1 else ''}",
                ]
            )
    else:
        expense_rows.append(["No expenses logged", "—", ""])

    header_cells = list(_HEADERS)
    body_rows = summary_rows + expense_rows
    totals = [
        "Centre share",
        f"{summary.centre_share_of_gross:.1f}% of gross",
        format_amount(summary.centre_pay),
    ]
    stamp_text = format_image_footer(live=False)

    n_rows = 1 + len(body_rows) + 1
    pad = _s(_PAD)
    row_h = _s(_ROW_H)
    title_block = _s(_TITLE_SIZE) + _s(10) if title else 0
    footer_h = _s(_STAMP_SIZE) + _s(10)

    col_w: list[int] = []
    for pass_num in range(2):
        measure_rows: list[tuple[list[str], bool, bool, bool]] = [
            (header_cells, True, False, False),
            *[(row, False, False, False) for row in body_rows],
            (totals, False, True, False),
        ]
        probe = Image.new("RGB", (4, 4), _BG)
        probe_draw = ImageDraw.Draw(probe)
        col_w = _measure_col_widths(probe_draw, measure_rows, min_widths=_MIN_COL_WIDTHS)
        _fit_header_columns(probe_draw, col_w, header_cells)
        if sum(col_w) <= _OUTPUT_INNER_WIDTH:
            break

    table_inner = sum(col_w)
    table_w = table_inner + pad * 2
    table_h = pad + title_block + n_rows * row_h + footer_h

    pad_hi = _ss(pad)
    table_inner_hi = _ss(table_inner)
    table_w_hi = table_inner_hi + pad_hi * 2
    title_block_hi = _ss(title_block)
    row_h_hi = _ss(row_h)
    footer_h_hi = _ss(footer_h)
    table_h_hi = pad_hi + title_block_hi + n_rows * row_h_hi + footer_h_hi
    col_w_hi = [_ss(w) for w in col_w]

    img = Image.new("RGB", (table_w_hi, table_h_hi), _BG)
    draw = ImageDraw.Draw(img)
    font_title = _cached_font(_ss(_s(_TITLE_SIZE)), True)
    font_sub = _cached_font(_ss(_s(_STAMP_SIZE + 2)), False)
    font_stamp = _cached_font(_ss(_s(_STAMP_SIZE)), False)

    x0 = pad_hi
    y = pad_hi
    if title:
        draw.text((x0, y), title, fill=_HEADER_TEXT, font=font_title)
        y += _ss(_s(_TITLE_SIZE + 4))
    if subtitle:
        draw.text((x0, y), subtitle, fill=_STAMP_TEXT, font=font_sub)
        y += _ss(_s(_STAMP_SIZE + 8))

    sheet_top = y
    draw.rectangle(
        (x0, sheet_top, x0 + table_inner_hi, sheet_top + n_rows * row_h_hi),
        fill=_SHEET_BG,
        outline=_SHEET_BORDER,
        width=max(1, _SUPERSAMPLE),
    )

    col_x = [x0]
    for w in col_w_hi[:-1]:
        col_x.append(col_x[-1] + w)

    grid_w = max(1, _SUPERSAMPLE)
    text_y_off = max(_ss(_s(8)), (row_h_hi - _ss(_s(_BODY_SIZE))) // 2)

    def draw_row(
        cells: list[str],
        *,
        bg: str,
        text_color: str,
        header: bool = False,
        total: bool = False,
        section: bool = False,
    ) -> None:
        nonlocal y
        draw.rectangle((x0, y, x0 + table_inner_hi, y + row_h_hi), fill=bg)
        for i, w in enumerate(col_w_hi):
            cell = cells[i] if i < len(cells) else ""
            bold = header or total or section
            f_cell = _cached_font(_ss(_s(_BODY_SIZE)), bold)
            if total:
                color = _TOTAL_TEXT
            elif header:
                color = _HEADER_TEXT
            elif section:
                color = _ID_TEXT
            else:
                color = text_color
            cx = col_x[i] + _ss(_s(8))
            draw.text((cx, y + text_y_off), cell, fill=color, font=f_cell)
            draw.line((col_x[i] + w, y, col_x[i] + w, y + row_h_hi), fill=_GRID, width=grid_w)
        draw.line((x0, y + row_h_hi, x0 + table_inner_hi, y + row_h_hi), fill=_GRID, width=grid_w)
        y += row_h_hi

    draw_row(header_cells, bg=_HEADER_BG, text_color=_HEADER_TEXT, header=True)
    for cells in body_rows:
        section = cells[0].startswith("—")
        draw_row(
            cells,
            bg=_SHEET_BG,
            text_color=_BODY_TEXT,
            section=section,
        )
    draw_row(totals, bg=_TOTAL_BG, text_color=_TOTAL_TEXT, total=True)

    footer_y = y + _ss(_s(6))
    footer_w = int(draw.textlength(stamp_text, font=font_stamp))
    footer_x = x0 + max(_ss(_s(8)), table_inner_hi - footer_w - _ss(_s(8)))
    draw.text((footer_x, footer_y), stamp_text, fill=_STAMP_TEXT, font=font_stamp)

    img = img.resize((table_w, table_h), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG", compress_level=1, optimize=False)
    return buf.getvalue()
