"""Render payment tables — dark professional spreadsheet images, mobile-readable."""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from telegram import InputFile

from database import PaymentRecord
from handlers.payment_table import (
    build_username_lookup,
    format_image_footer,
    payment_table_row,
    payment_totals_table_row,
    table_headers,
)

_SCALE = 1
_OUTPUT_INNER_WIDTH = 640
_SUPERSAMPLE = 2  # render 2× then downscale → sharper text at same output size

# Tuned for Telegram inline preview (narrow image, readable text at chat width)
_ROW_H = 30
_PAD = 10
_TITLE_SIZE = 18
_BODY_SIZE = 17
_STAMP_SIZE = 12
_CELL_PAD = 8
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

_ID_COL = 0
_CARD_COL = 5
_CLEARED_COL = 6
_MIN_COL_WIDTHS_FULL = (52, 88, 72, 88, 88, 52, 60, 72, 80, 72)
_MIN_COL_WIDTHS_COMPACT = (52, 88, 72, 88, 88, 52, 60)


def payment_table_input_file(png: bytes, *, filename: str = "payments.png") -> InputFile:
    """Fresh InputFile for each Telegram upload (BytesIO must not be reused)."""
    bio = BytesIO(png)
    bio.seek(0)
    return InputFile(bio, filename=filename)


def live_report_title(bot_display_name: str) -> str:
    label = (bot_display_name or "").lower()
    if "q2" in label:
        return "Q2 Payments"
    if "q1" in label:
        return "Q1 Payments"
    name = (bot_display_name or "Payments").strip()
    return name if name.lower().endswith("payments") else f"{name} Payments"


def _s(value: int) -> int:
    return max(1, int(round(value * _SCALE * _RENDER_SCALE)))


_RENDER_SCALE = 1.0


def _ss(value: int | float) -> int:
    """Supersampled pixel size for internal render (downscaled before PNG export)."""
    return max(1, int(round(float(value) * _SUPERSAMPLE)))


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


def _cell_draw_font(
    col_index: int,
    *,
    header: bool,
    total: bool,
    stamp: bool,
    is_id_value: bool,
    is_cleared_value: bool,
):
    if stamp:
        return _cached_font(_s(_STAMP_SIZE), False)
    if header or total or is_id_value or is_cleared_value:
        return _cached_font(_s(_BODY_SIZE), True)
    return _cached_font(_s(_BODY_SIZE), False)


def _measure_col_widths(
    draw,
    rows: list[tuple[list[str], bool, bool, bool]],
    *,
    min_widths: tuple[int, ...],
) -> list[int]:
    widths = [_s(w) for w in min_widths]
    pad = _s(_CELL_PAD)

    for cells, header, total, stamp in rows:
        for i in range(min(len(widths), len(cells))):
            cell = cells[i]
            if not cell:
                continue
            is_id = i == _ID_COL and not header and not total
            is_cleared = i == _CLEARED_COL and not header and not total
            font = _cell_draw_font(
                i,
                header=header,
                total=total,
                stamp=stamp,
                is_id_value=is_id,
                is_cleared_value=is_cleared,
            )
            need = int(draw.textlength(cell, font=font)) + pad
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


def _fit_header_columns(draw, col_w: list[int], header_cells: list[str]) -> None:
    font = _cached_font(_s(_BODY_SIZE), True)
    pad = _s(_CELL_PAD) + _s(4)
    for i, label in enumerate(header_cells):
        if i >= len(col_w) or not label:
            continue
        need = int(draw.textlength(label, font=font)) + pad
        col_w[i] = max(col_w[i], need)


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

    global _RENDER_SCALE
    compact_names = not full_excel

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

    col_w: list[int] = []
    header_cells: list[str] = []
    body_rows: list[list[str]] = []
    stamp_text = format_image_footer(live=live)

    for pass_num in range(2):
        _RENDER_SCALE = (
            1.0
            if pass_num == 0
            else min(1.0, _OUTPUT_INNER_WIDTH / max(sum(col_w), 1))
        )

        header_cells = list(table_headers(full_excel=full_excel))
        body_rows = [
            payment_table_row(
                record,
                username_lookup=username_lookup,
                full_excel=full_excel,
                compact_names=compact_names,
            )
            for record in shown
        ]
        measure_rows: list[tuple[list[str], bool, bool, bool]] = [
            (header_cells, True, False, False),
            *[(row, False, False, False) for row in body_rows],
            (totals, False, True, False),
        ]

        probe = Image.new("RGB", (4, 4), _BG)
        probe_draw = ImageDraw.Draw(probe)
        min_widths = _MIN_COL_WIDTHS_FULL if full_excel else _MIN_COL_WIDTHS_COMPACT
        col_w = _measure_col_widths(probe_draw, measure_rows, min_widths=min_widths)
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

    # Supersampled draw buffer (2×) → downscale to table_w×table_h for crisp PNG text
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

    def _hi_font(
        col_index: int,
        *,
        header: bool,
        total: bool,
        is_id_value: bool,
        is_cleared_value: bool,
    ):
        if header or total or is_id_value or is_cleared_value:
            return _cached_font(_ss(_s(_BODY_SIZE)), True)
        return _cached_font(_ss(_s(_BODY_SIZE)), False)

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
            is_cleared = i == _CLEARED_COL and cell and not header and not total
            f_cell = _hi_font(
                i,
                header=header,
                total=total,
                is_id_value=is_id,
                is_cleared_value=is_cleared,
            )
            if is_id:
                color = _ID_TEXT
            elif is_cleared:
                color = _cleared_cell_color(cell)
            elif total:
                color = _TOTAL_TEXT
            elif header:
                color = _HEADER_TEXT
            else:
                color = text_color
            label = cell
            cx = col_x[i] + _ss(_s(8))
            draw.text((cx, y + text_y_off), label, fill=color, font=f_cell)
            draw.line((col_x[i] + w, y, col_x[i] + w, y + row_h_hi), fill=_GRID, width=grid_w)
        draw.line((x0, y + row_h_hi, x0 + table_inner_hi, y + row_h_hi), fill=_GRID, width=grid_w)
        y += row_h_hi

    draw_row(header_cells, bg=_HEADER_BG, text_color=_HEADER_TEXT, header=True)
    for record, cells in zip(shown, body_rows):
        bg, txt = _row_style(record.cleared)
        draw_row(cells, bg=bg, text_color=txt)
    draw_row(totals, bg=_TOTAL_BG, text_color=_TOTAL_TEXT, total=True)

    footer_y = y + _ss(_s(6))
    footer_w = int(draw.textlength(stamp_text, font=font_stamp))
    footer_x = x0 + max(_ss(_s(8)), table_inner_hi - footer_w - _ss(_s(8)))
    draw.text((footer_x, footer_y), stamp_text, fill=_STAMP_TEXT, font=font_stamp)

    img = img.resize((table_w, table_h), Image.Resampling.LANCZOS)

    buf = BytesIO()
    img.save(buf, format="PNG", compress_level=1, optimize=False)
    return buf.getvalue()
