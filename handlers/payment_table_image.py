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

# Render at 2× for sharp text on phone screens (Telegram compresses photos).
_SCALE = 2

# Telegram dark theme
_BG = "#17212b"
_HEADER_BG = "#232e3c"
_HEADER_TEXT = "#ffffff"
_BODY_TEXT = "#f0f4f8"
_GRID = "#3d4f5f"
_TOTAL_BG = "#232e3c"
_FOOTER_TEXT = "#9db0c0"
_TITLE_COLOR = "#ffffff"

_ROW_CLEARED = "#1b4332"
_ROW_PENDING = "#4a3f1a"
_ROW_NOT_CLEARED = "#4a1f1f"
_TEXT_CLEARED = "#6ee7a0"
_TEXT_PENDING = "#fde047"
_TEXT_NOT_CLEARED = "#fca5a5"

_COL_WIDTHS = (108, 112, 168, 168, 58, 88)
_ROW_H = 36
_PAD = 18
_TITLE_SIZE = 26
_BODY_SIZE = 15
_SMALL_SIZE = 13
_MAX_ROWS = 40


def live_report_title(bot_display_name: str) -> str:
    """Top-left label for /setnotifypayments live image."""
    label = (bot_display_name or "").lower()
    if "q2" in label:
        return "Q2 Payments"
    if "q1" in label:
        return "Q1 Payments"
    name = (bot_display_name or "Payments").strip()
    return name if name.lower().endswith("payments") else f"{name} Payments"


def _s(value: int) -> int:
    return value * _SCALE


def _row_style(cleared: bool | None) -> tuple[str, str, str]:
    if cleared is None:
        return _ROW_PENDING, _BODY_TEXT, _TEXT_PENDING
    if cleared:
        return _ROW_CLEARED, _BODY_TEXT, _TEXT_CLEARED
    return _ROW_NOT_CLEARED, _BODY_TEXT, _TEXT_NOT_CLEARED


def _load_font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    regular = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/consola.ttf"),
        Path("C:/Windows/Fonts/segoeui.ttf"),
    ]
    bold_paths = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf"),
        Path("C:/Windows/Fonts/consolab.ttf"),
    ]
    paths = bold_paths + regular if bold else regular
    scaled = _s(size)
    for path in paths:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), scaled)
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

    col_w = [_s(w) for w in _COL_WIDTHS]
    row_h = _s(_ROW_H)
    pad = _s(_PAD)
    table_w = sum(col_w) + pad * 2

    title_block = _s(34)
    subtitle_block = _s(22) if subtitle else 0
    footer_lines = 1 + (1 if status_summary else 0) + (1 if hidden_count > 0 else 0)
    footer_block = _s(24) * footer_lines + _s(8)

    n_rows = len(shown) + 2
    table_h = pad + title_block + subtitle_block + (n_rows * row_h) + footer_block + pad

    img = Image.new("RGB", (table_w, table_h), _BG)
    draw = ImageDraw.Draw(img)

    font_title = _load_font(_TITLE_SIZE, bold=True)
    font_body = _load_font(_BODY_SIZE)
    font_header = _load_font(_BODY_SIZE, bold=True)
    font_sm = _load_font(_SMALL_SIZE)

    y = pad
    draw.text((pad, y), title, fill=_TITLE_COLOR, font=font_title)
    y += title_block
    if subtitle:
        draw.text((pad, y), subtitle, fill=_FOOTER_TEXT, font=font_sm)
        y += subtitle_block

    x0 = pad
    col_x = [x0]
    for w in col_w[:-1]:
        col_x.append(col_x[-1] + w)

    grid_w = max(_s(1), _SCALE)

    def draw_row(
        cells: list[str],
        *,
        bg: str,
        text_color: str,
        cleared_col_color: str | None = None,
        header: bool = False,
    ) -> None:
        nonlocal y
        f = font_header if header else font_body
        draw.rectangle((x0, y, x0 + sum(col_w), y + row_h), fill=bg)
        for i, (cell, w) in enumerate(zip(cells, col_w)):
            cx = col_x[i] + _s(10)
            cy = y + _s(9)
            color = text_color
            if i == 5 and cleared_col_color:
                color = cleared_col_color
            label = _truncate(cell, f, w - _s(14), draw)
            draw.text((cx, cy), label, fill=color, font=f)
            if i < len(col_w) - 1:
                gx = col_x[i] + w
                draw.line((gx, y, gx, y + row_h), fill=_GRID, width=grid_w)
        draw.line(
            (x0, y + row_h, x0 + sum(col_w), y + row_h),
            fill=_GRID,
            width=grid_w,
        )
        y += row_h

    draw_row(list(TABLE_HEADERS), bg=_HEADER_BG, text_color=_HEADER_TEXT, header=True)

    for record in shown:
        cells = payment_table_row(record, username_lookup=username_lookup)
        cells[5] = _cleared_plain(record.cleared)
        bg, txt, clr_txt = _row_style(record.cleared)
        draw_row(cells, bg=bg, text_color=txt, cleared_col_color=clr_txt)

    draw_row(totals, bg=_TOTAL_BG, text_color=_BODY_TEXT, header=True)

    if status_summary:
        draw.text((pad, y + _s(6)), status_summary, fill=_FOOTER_TEXT, font=font_sm)
        y += _s(24)

    if hidden_count > 0:
        note = f"+{hidden_count} more not shown"
        draw.text((pad, y + _s(6)), note, fill=_FOOTER_TEXT, font=font_sm)
        y += _s(24)

    footer = f"{format_payment_sheet_updated_note()} · live"
    draw.text((pad, y + _s(6)), footer, fill=_FOOTER_TEXT, font=font_sm)

    buf = BytesIO()
    img.save(buf, format="PNG", compress_level=3)
    return buf.getvalue()
