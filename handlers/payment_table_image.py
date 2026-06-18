"""Render payment tables — dark professional spreadsheet images, mobile-readable."""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from database import PaymentRecord
from handlers.payment_table import (
    build_username_lookup,
    format_image_footer,
    payment_table_row,
    payment_totals_table_row,
    table_headers,
)

_SCALE = 2

# Dark professional palette
_BG = "#0d1117"
_SHEET_BG = "#161b22"
_SHEET_BORDER = "#30363d"
_HEADER_BG = "#21262d"
_HEADER_TEXT = "#f0f6fc"
_BODY_TEXT = "#e6edf3"
_GRID = "#30363d"
_ID_TEXT = "#79c0ff"
_TOTAL_BG = "#1c2128"
_TOTAL_TEXT = "#f0f6fc"
_STAMP_TEXT = "#8b949e"
_STATUS_CLEARED = "#3fb950"
_STATUS_PENDING = "#d29922"
_STATUS_NOT = "#f85149"

_ROW_CLEARED = "#1a3d2b"
_ROW_PENDING = "#3d3218"
_ROW_NOT_CLEARED = "#3d2228"
_ROW_CLEARED_TEXT = "#d4f5dc"
_ROW_PENDING_TEXT = "#f5e6b8"
_ROW_NOT_CLEARED_TEXT = "#f5d0d4"

_ROW_H = 42
_PAD = 16
_TITLE_SIZE = 22
_BODY_SIZE = 20
_STAMP_SIZE = 16
_CELL_PAD = 16

_ID_COL = 0
_CLEARED_COL = 6
_MIN_COL_WIDTHS_FULL = (72, 104, 92, 120, 120, 56, 72, 96, 104, 96)
_MIN_COL_WIDTHS_COMPACT = (72, 104, 92, 132, 132, 56, 72)


def live_report_title(bot_display_name: str) -> str:
    label = (bot_display_name or "").lower()
    if "q2" in label:
        return "Q2 Payments"
    if "q1" in label:
        return "Q1 Payments"
    name = (bot_display_name or "Payments").strip()
    return name if name.lower().endswith("payments") else f"{name} Payments"


def _s(value: int) -> int:
    return value * _SCALE


@lru_cache(maxsize=16)
def _cached_font(size: int, bold: bool):
    from PIL import ImageFont

    paths: list[Path] = []
    if bold:
        paths.extend(
            [
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                Path("C:/Windows/Fonts/segoeuib.ttf"),
            ]
        )
    else:
        paths.extend(
            [
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("C:/Windows/Fonts/segoeui.ttf"),
            ]
        )
    for path in paths:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _row_style(cleared: bool | None) -> tuple[str, str]:
    if cleared is None:
        return _ROW_PENDING, _ROW_PENDING_TEXT
    if cleared:
        return _ROW_CLEARED, _ROW_CLEARED_TEXT
    return _ROW_NOT_CLEARED, _ROW_NOT_CLEARED_TEXT


def _cleared_cell_color(value: str) -> str:
    label = value.strip().lower()
    if label == "yes":
        return _STATUS_CLEARED
    if label == "pending":
        return _STATUS_PENDING
    if label == "no":
        return _STATUS_NOT
    return _BODY_TEXT


def _fit_text(draw, text: str, font, max_width: int) -> str:
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    trimmed = text
    while trimmed and draw.textlength(trimmed + "…", font=font) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + "…") if trimmed else "…"


def _measure_col_widths(
    draw,
    rows: list[tuple[list[str], bool]],
    *,
    min_widths: tuple[int, ...],
) -> list[int]:
    widths = [_s(w) for w in min_widths]
    font_body = _cached_font(_s(_BODY_SIZE), False)
    font_header = _cached_font(_s(_BODY_SIZE), True)
    pad = _s(_CELL_PAD)

    for cells, is_header in rows:
        font = font_header if is_header else font_body
        for i in range(min(len(widths), len(cells))):
            cell = cells[i]
            if not cell:
                continue
            cell_font = font_header if i == _ID_COL else font
            need = int(draw.textlength(cell, font=cell_font)) + pad
            widths[i] = max(widths[i], need)
    return widths


def _fit_id_column(draw, col_w: list[int], body_rows: list[list[str]]) -> None:
    font_id = _cached_font(_s(_BODY_SIZE), True)
    pad = _s(_CELL_PAD)
    longest = "#"
    for row in body_rows:
        if row and row[0]:
            longest = row[0] if len(row[0]) > len(longest) else longest
    need = int(draw.textlength(longest, font=font_id)) + pad
    col_w[_ID_COL] = max(col_w[_ID_COL], need)


def render_payments_table_png(
    records: list[PaymentRecord],
    *,
    database_path: str,
    total_amount: float,
    total_count: int,
    lookup_records: list[PaymentRecord] | None = None,
    totals_records: list[PaymentRecord] | None = None,
    title: str = "",
    subtitle: str = "",
    status_totals: tuple[float, int, float, int, float, int] | None = None,
    live: bool = False,
    full_excel: bool = True,
    total_label: str = "TOTAL",
    page_info: str = "",
) -> bytes:
    from PIL import Image, ImageDraw

    _ = status_totals
    _ = page_info  # shown in Telegram caption, not inside the image

    username_lookup = build_username_lookup(
        database_path,
        lookup_records if lookup_records is not None else records,
    )
    shown = list(records)
    totals = payment_totals_table_row(
        total_amount=total_amount,
        total_count=total_count,
        records=totals_records if totals_records is not None else shown,
        full_excel=full_excel,
        total_label=total_label,
    )

    header_cells = list(table_headers(full_excel=full_excel))
    body_rows = [
        payment_table_row(record, username_lookup=username_lookup, full_excel=full_excel)
        for record in shown
    ]

    stamp_cells = [""] * len(header_cells)
    stamp_cells[-1] = format_image_footer(live=live)

    measure_rows: list[tuple[list[str], bool]] = [
        (header_cells, True),
        *[(row, False) for row in body_rows],
        (totals, True),
        (stamp_cells, False),
    ]

    probe = Image.new("RGB", (4, 4), _BG)
    probe_draw = ImageDraw.Draw(probe)
    min_widths = _MIN_COL_WIDTHS_FULL if full_excel else _MIN_COL_WIDTHS_COMPACT
    col_w = _measure_col_widths(probe_draw, measure_rows, min_widths=min_widths)
    _fit_id_column(probe_draw, col_w, body_rows)

    pad = _s(_PAD)
    table_inner_w = sum(col_w)
    table_w = table_inner_w + pad * 2

    has_title = bool(title)
    title_block = _s(34) if has_title else 0
    row_h = _s(_ROW_H)
    n_rows = len(shown) + 3
    table_h = pad + title_block + n_rows * row_h + pad

    img = Image.new("RGB", (table_w, table_h), _BG)
    draw = ImageDraw.Draw(img)

    font_title = _cached_font(_s(_TITLE_SIZE), True)
    font_body = _cached_font(_s(_BODY_SIZE), False)
    font_header = _cached_font(_s(_BODY_SIZE), True)
    font_stamp = _cached_font(_s(_STAMP_SIZE), False)

    x0 = pad
    y = pad
    if title:
        draw.text((x0, y), title, fill=_HEADER_TEXT, font=font_title)
        y += title_block

    sheet_top = y
    draw.rectangle(
        (x0, sheet_top, x0 + table_inner_w, sheet_top + n_rows * row_h),
        fill=_SHEET_BG,
        outline=_SHEET_BORDER,
        width=max(1, _SCALE),
    )

    col_x = [x0]
    for w in col_w[:-1]:
        col_x.append(col_x[-1] + w)

    grid_w = max(1, _SCALE)
    text_y_off = max(_s(8), (row_h - _s(_BODY_SIZE)) // 2)

    def draw_row(
        cells: list[str],
        *,
        bg: str,
        text_color: str,
        header: bool = False,
        total: bool = False,
        stamp: bool = False,
    ) -> None:
        nonlocal y
        f = font_header if header or total else font_stamp if stamp else font_body
        draw.rectangle((x0, y, x0 + table_inner_w, y + row_h), fill=bg)
        for i, w in enumerate(col_w):
            cell = cells[i] if i < len(cells) else ""
            inner_w = max(_s(10), w - _s(_CELL_PAD))
            if i == _ID_COL and cell and not header and not total:
                color = _ID_TEXT
                f_cell = font_header
            elif i == _CLEARED_COL and cell and not header and not total:
                color = _cleared_cell_color(cell)
                f_cell = font_header
            elif stamp and i == len(col_w) - 1:
                color = _STAMP_TEXT
                f_cell = font_stamp
            elif total:
                color = _TOTAL_TEXT
                f_cell = font_header
            elif header:
                color = _HEADER_TEXT
                f_cell = font_header
            else:
                color = text_color
                f_cell = f
            if i == _ID_COL and cell and not header and not total:
                label = cell
            else:
                label = _fit_text(draw, cell, f_cell, inner_w)
            cx = col_x[i] + _s(8)
            if stamp and i == len(col_w) - 1:
                text_w = int(draw.textlength(label, font=f_cell))
                cx = col_x[i] + w - text_w - _s(8)
            draw.text((cx, y + text_y_off), label, fill=color, font=f_cell)
            draw.line((col_x[i] + w, y, col_x[i] + w, y + row_h), fill=_GRID, width=grid_w)
        draw.line((x0, y + row_h, x0 + table_inner_w, y + row_h), fill=_GRID, width=grid_w)
        y += row_h

    draw_row(header_cells, bg=_HEADER_BG, text_color=_HEADER_TEXT, header=True)
    for record, cells in zip(shown, body_rows):
        bg, txt = _row_style(record.cleared)
        draw_row(cells, bg=bg, text_color=txt)
    draw_row(totals, bg=_TOTAL_BG, text_color=_TOTAL_TEXT, total=True)
    draw_row(stamp_cells, bg=_SHEET_BG, text_color=_STAMP_TEXT, stamp=True)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()
