"""Render expense tables as dark spreadsheet PNGs (same style as payments)."""

from __future__ import annotations

from io import BytesIO

from telegram import InputFile

from database import ExpenseRecord
from handlers.expense_table import (
    EXPENSE_HEADERS,
    build_expense_username_lookup,
    expense_table_row,
    expense_totals_row,
)
from handlers.payment_table import format_image_footer

from handlers.payment_table_image import (
    _BG,
    _BODY_SIZE,
    _BODY_TEXT,
    _GRID,
    _HEADER_BG,
    _HEADER_TEXT,
    _ID_COL,
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
    _fit_id_column,
    _measure_col_widths,
    _s,
    _ss,
)

_RENDER_SCALE = 1.0

_MIN_COL_WIDTHS = (52, 88, 72, 88, 120)
_REASON_COL = 4


def expense_table_input_file(data: bytes, *, filename: str = "expenses.png") -> InputFile:
    return InputFile(data, filename=filename)


def expense_report_title(bot_display_name: str) -> str:
    label = (bot_display_name or "").lower()
    if "q2" in label:
        return "Q2 Expenses"
    if "q1" in label:
        return "Q1 Expenses"
    name = (bot_display_name or "Expenses").strip()
    return name if name.lower().endswith("expenses") else f"{name} Expenses"


def render_expenses_table_png(
    records: list[ExpenseRecord],
    *,
    database_path: str,
    total_amount: float,
    total_count: int,
    title: str = "",
    subtitle: str = "",
    live: bool = False,
) -> bytes:
    from PIL import Image, ImageDraw

    _ = subtitle

    username_lookup = build_expense_username_lookup(database_path, records)
    shown = list(records)
    totals = expense_totals_row(total_amount=total_amount, total_count=total_count)

    col_w: list[int] = []
    header_cells = list(EXPENSE_HEADERS)
    body_rows = [
        expense_table_row(record, username_lookup=username_lookup, compact_names=True)
        for record in shown
    ]
    stamp_text = format_image_footer(live=live)

    global _RENDER_SCALE
    _RENDER_SCALE = 1.0

    for pass_num in range(2):
        _RENDER_SCALE = (
            1.0
            if pass_num == 0
            else min(1.0, _OUTPUT_INNER_WIDTH / max(sum(col_w), 1))
        )
        body_rows = [
            expense_table_row(record, username_lookup=username_lookup, compact_names=True)
            for record in shown
        ]
        measure_rows: list[tuple[list[str], bool, bool, bool]] = [
            (header_cells, True, False, False),
            *[(row, False, False, False) for row in body_rows],
            (totals, False, True, False),
        ]
        probe = Image.new("RGB", (4, 4), _BG)
        probe_draw = ImageDraw.Draw(probe)
        col_w = _measure_col_widths(probe_draw, measure_rows, min_widths=_MIN_COL_WIDTHS)
        _fit_id_column(probe_draw, col_w, body_rows)
        _fit_header_columns(probe_draw, col_w, header_cells)
        if sum(col_w) <= _OUTPUT_INNER_WIDTH:
            break

    pad = _s(_PAD)
    table_inner_w = sum(col_w)
    table_w = table_inner_w + pad * 2
    has_title = bool(title)
    title_block = _s(34) if has_title else 0
    row_h = _s(_ROW_H)
    footer_h = _s(30)
    n_rows = len(shown) + 2
    table_h = pad + title_block + n_rows * row_h + footer_h + pad

    pad_hi = _ss(pad)
    table_inner_hi = _ss(table_inner_w)
    table_w_hi = table_inner_hi + pad_hi * 2
    title_block_hi = _ss(title_block)
    row_h_hi = _ss(row_h)
    footer_h_hi = _ss(footer_h)
    table_h_hi = pad_hi + title_block_hi + n_rows * row_h_hi + footer_h_hi
    col_w_hi = [_ss(w) for w in col_w]

    img = Image.new("RGB", (table_w_hi, table_h_hi), _BG)
    draw = ImageDraw.Draw(img)
    font_title = _cached_font(_ss(_s(_TITLE_SIZE)), True)
    font_stamp = _cached_font(_ss(_s(_STAMP_SIZE)), False)

    x0 = pad_hi
    y = pad_hi
    if title:
        draw.text((x0, y), title, fill=_HEADER_TEXT, font=font_title)
        y += title_block_hi

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
    ) -> None:
        nonlocal y
        draw.rectangle((x0, y, x0 + table_inner_hi, y + row_h_hi), fill=bg)
        for i, w in enumerate(col_w_hi):
            cell = cells[i] if i < len(cells) else ""
            is_id = i == _ID_COL and cell and not header and not total
            bold = header or total or is_id or i == _REASON_COL
            f_cell = _cached_font(_ss(_s(_BODY_SIZE)), bold)
            if is_id:
                color = _ID_TEXT
            elif total:
                color = _TOTAL_TEXT
            elif header:
                color = _HEADER_TEXT
            else:
                color = text_color
            cx = col_x[i] + _ss(_s(8))
            draw.text((cx, y + text_y_off), cell, fill=color, font=f_cell)
            draw.line((col_x[i] + w, y, col_x[i] + w, y + row_h_hi), fill=_GRID, width=grid_w)
        draw.line((x0, y + row_h_hi, x0 + table_inner_hi, y + row_h_hi), fill=_GRID, width=grid_w)
        y += row_h_hi

    draw_row(header_cells, bg=_HEADER_BG, text_color=_HEADER_TEXT, header=True)
    for cells in body_rows:
        draw_row(cells, bg=_SHEET_BG, text_color=_BODY_TEXT)
    draw_row(totals, bg=_TOTAL_BG, text_color=_TOTAL_TEXT, total=True)

    footer_y = y + _ss(_s(6))
    footer_w = int(draw.textlength(stamp_text, font=font_stamp))
    footer_x = x0 + max(_ss(_s(8)), table_inner_hi - footer_w - _ss(_s(8)))
    draw.text((footer_x, footer_y), stamp_text, fill=_STAMP_TEXT, font=font_stamp)

    img = img.resize((table_w, table_h), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG", compress_level=1, optimize=False)
    return buf.getvalue()
