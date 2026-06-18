"""Render payment tables as PNG images (Telegram dark theme + Excel row colours)."""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

from database import PaymentRecord
from handlers.payment_table import (
    TABLE_HEADERS,
    build_username_lookup,
    payment_table_row,
    payment_totals_table_row,
)
from payments_excel_export import format_payment_sheet_updated_note

logger = logging.getLogger(__name__)

# Telegram dark chat / code-block tones
_BG = "#182533"
_HEADER_BG = "#1f2c38"
_HEADER_TEXT = "#6ab2f2"
_BODY_TEXT = "#e4ecf2"
_GRID = "#2a3944"
_TOTAL_BG = "#1f2c38"
_FOOTER_TEXT = "#8b9bab"

_ROW_CLEARED = "#1a3d2a"
_ROW_PENDING = "#3d3420"
_ROW_NOT_CLEARED = "#3d2222"
_TEXT_CLEARED = "#86efac"
_TEXT_PENDING = "#fcd34d"
_TEXT_NOT_CLEARED = "#fca5a5"

_COL_WIDTHS = (92, 96, 148, 148, 52, 78)
_ROW_H = 30
_PAD = 14
_TITLE_H = 36
_FOOTER_H = 28
_MAX_ROWS = 40


def _row_style(cleared: bool | None) -> tuple[str, str, str]:
    if cleared is None:
        return _ROW_PENDING, _BODY_TEXT, _TEXT_PENDING
    if cleared:
        return _ROW_CLEARED, _BODY_TEXT, _TEXT_CLEARED
    return _ROW_NOT_CLEARED, _BODY_TEXT, _TEXT_NOT_CLEARED


def _load_font(size: int):
    from PIL import ImageFont

    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/consola.ttf"),
        Path("C:/Windows/Fonts/segoeui.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _truncate(text: str, font, max_w: int, draw) -> str:
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_w:
        return text
    trimmed = text
    while len(trimmed) > 1 and draw.textlength(trimmed + "…", font=font) > max_w:
        trimmed = trimmed[:-1]
    return trimmed + "…" if trimmed else "…"


def _cleared_plain(cleared: bool | None) -> str:
    if cleared is None:
        return "Pending"
    return "Yes" if cleared else "No"


def render_payments_table_png(
    records: list[PaymentRecord],
    *,
    database_path: str,
    total_amount: float,
    total_count: int,
    lookup_records: list[PaymentRecord] | None = None,
    title: str,
    subtitle: str = "",
    status_summary: str = "",
    hidden_count: int = 0,
) -> bytes:
    from PIL import Image, ImageDraw

    username_lookup = build_username_lookup(
        database_path,
        lookup_records if lookup_records is not None else records,
    )
    shown = records[:_MAX_ROWS]
    totals = payment_totals_table_row(
        total_amount=total_amount,
        total_count=total_count,
    )

    table_w = sum(_COL_WIDTHS) + _PAD * 2
    n_rows = len(shown) + 2  # header + total
    table_h = _TITLE_H + (n_rows * _ROW_H) + _FOOTER_H + _PAD * 2
    if hidden_count > 0:
        table_h += 22
    if status_summary:
        table_h += 22

    img = Image.new("RGB", (table_w, table_h), _BG)
    draw = ImageDraw.Draw(img)
    font = _load_font(13)
    font_bold = _load_font(13)
    font_sm = _load_font(11)

    y = _PAD
    draw.text((_PAD, y), title, fill=_HEADER_TEXT, font=font_bold)
    y += 18
    if subtitle:
        draw.text((_PAD, y), subtitle, fill=_FOOTER_TEXT, font=font_sm)
        y += 16
    y += 6

    x0 = _PAD
    col_x = [x0]
    for w in _COL_WIDTHS[:-1]:
        col_x.append(col_x[-1] + w)

    def draw_row(
        cells: list[str],
        *,
        bg: str,
        text_color: str,
        cleared_col_color: str | None = None,
        bold: bool = False,
    ) -> None:
        nonlocal y
        f = font_bold if bold else font
        draw.rectangle(
            (x0, y, x0 + sum(_COL_WIDTHS), y + _ROW_H),
            fill=bg,
        )
        for i, (cell, w) in enumerate(zip(cells, _COL_WIDTHS)):
            cx = col_x[i] + 6
            cy = y + 7
            color = text_color
            if i == 5 and cleared_col_color:
                color = cleared_col_color
            label = _truncate(cell, f, w - 10, draw)
            draw.text((cx, cy), label, fill=color, font=f)
            if i < len(_COL_WIDTHS) - 1:
                gx = col_x[i] + w
                draw.line((gx, y, gx, y + _ROW_H), fill=_GRID, width=1)
        draw.line(
            (x0, y + _ROW_H, x0 + sum(_COL_WIDTHS), y + _ROW_H),
            fill=_GRID,
            width=1,
        )
        y += _ROW_H

    draw_row(list(TABLE_HEADERS), bg=_HEADER_BG, text_color=_HEADER_TEXT, bold=True)

    for record in shown:
        cells = payment_table_row(record, username_lookup=username_lookup)
        cells[5] = _cleared_plain(record.cleared)
        bg, txt, clr_txt = _row_style(record.cleared)
        draw_row(cells, bg=bg, text_color=txt, cleared_col_color=clr_txt)

    draw_row(totals, bg=_TOTAL_BG, text_color=_BODY_TEXT, bold=True)

    if status_summary:
        draw.text((_PAD, y + 4), status_summary, fill=_FOOTER_TEXT, font=font_sm)
        y += 22

    if hidden_count > 0:
        note = f"+{hidden_count} more not shown"
        draw.text((_PAD, y + 4), note, fill=_FOOTER_TEXT, font=font_sm)
        y += 22

    footer = format_payment_sheet_updated_note()
    draw.text((_PAD, y + 4), footer, fill=_FOOTER_TEXT, font=font_sm)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
