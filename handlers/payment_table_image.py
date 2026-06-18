"""Render payment tables as a single Excel-style spreadsheet image."""

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
# Excel export fills (openpyxl fgColor values)
_ROW_CLEARED = "#C6EFCE"
_ROW_PENDING = "#FFEB9C"
_ROW_NOT_CLEARED = "#FFC7CE"
_BG = "#FFFFFF"
_SHEET_BG = "#FFFFFF"
_HEADER_BG = "#D9D9D9"
_HEADER_TEXT = "#000000"
_BODY_TEXT = "#000000"
_GRID = "#BFBFBF"
_ID_TEXT = "#1d4ed8"
_TOTAL_BG = "#F2F2F2"
_STAMP_TEXT = "#595959"

_ROW_H = 30
_PAD = 12
_TITLE_SIZE = 20
_BODY_SIZE = 15
_STAMP_SIZE = 13
_CELL_PAD = 14

_ID_COL = 0
_MIN_COL_WIDTHS = (44, 88, 76, 100, 100, 48, 64, 92, 100, 92)


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


@lru_cache(maxsize=12)
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
        return _ROW_PENDING, _BODY_TEXT
    if cleared:
        return _ROW_CLEARED, _BODY_TEXT
    return _ROW_NOT_CLEARED, _BODY_TEXT


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
            need = int(draw.textlength(cell, font=font)) + pad
            widths[i] = max(widths[i], need)
    return widths


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
    if page_info:
        stamp_cells[2] = page_info
    stamp_cells[-1] = format_image_footer(live=live)

    measure_rows: list[tuple[list[str], bool]] = [
        (header_cells, True),
        *[(row, False) for row in body_rows],
        (totals, True),
        (stamp_cells, False),
    ]

    probe = Image.new("RGB", (4, 4), _BG)
    probe_draw = ImageDraw.Draw(probe)
    col_w = _measure_col_widths(probe_draw, measure_rows, min_widths=_MIN_COL_WIDTHS)

    pad = _s(_PAD)
    table_inner_w = sum(col_w)
    table_w = table_inner_w + pad * 2

    has_title = bool(title)
    title_block = _s(28) if has_title else 0
    row_h = _s(_ROW_H)
    n_rows = len(shown) + 3  # header + totals + stamp
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

    col_x = [x0]
    for w in col_w[:-1]:
        col_x.append(col_x[-1] + w)

    grid_w = max(1, _SCALE)
    text_y_off = max(_s(6), (row_h - _s(_BODY_SIZE)) // 2)

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
            inner_w = max(_s(8), w - _s(_CELL_PAD))
            if i == _ID_COL and cell and not header and not total:
                color = _ID_TEXT
                f_cell = font_header
            elif stamp and i == len(col_w) - 1:
                color = _STAMP_TEXT
                f_cell = font_stamp
            else:
                color = text_color
                f_cell = f
            label = _fit_text(draw, cell, f_cell, inner_w)
            cx = col_x[i] + _s(6)
            if stamp and i == len(col_w) - 1:
                text_w = int(draw.textlength(label, font=f_cell))
                cx = col_x[i] + w - text_w - _s(6)
            draw.text((cx, y + text_y_off), label, fill=color, font=f_cell)
            draw.line((col_x[i] + w, y, col_x[i] + w, y + row_h), fill=_GRID, width=grid_w)
        draw.line((x0, y + row_h, x0 + table_inner_w, y + row_h), fill=_GRID, width=grid_w)
        y += row_h

    draw_row(header_cells, bg=_HEADER_BG, text_color=_HEADER_TEXT, header=True)
    for record, cells in zip(shown, body_rows):
        bg, txt = _row_style(record.cleared)
        draw_row(cells, bg=bg, text_color=txt)
    draw_row(totals, bg=_TOTAL_BG, text_color=_HEADER_TEXT, total=True)
    draw_row(stamp_cells, bg=_SHEET_BG, text_color=_STAMP_TEXT, stamp=True)

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=94, optimize=False, subsampling=0)
    return buf.getvalue()
