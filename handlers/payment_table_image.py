"""Render payment tables as PNG/JPEG images (Telegram dark theme + Excel row colours)."""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from database import PaymentRecord
from handlers.payment_table import (
    TABLE_HEADERS,
    build_username_lookup,
    format_image_footer,
    format_status_label,
    payment_table_row,
    payment_totals_table_row,
)

_SCALE = 2

_BG = "#17212b"
_HEADER_BG = "#232e3c"
_HEADER_TEXT = "#ffffff"
_BODY_TEXT = "#f5f7fa"
_GRID = "#3d4f5f"
_TOTAL_BG = "#232e3c"
_FOOTER_TEXT = "#b0bec9"
_TITLE_COLOR = "#ffffff"
_LEGEND_TEXT = "#c8d4de"

_ROW_CLEARED = "#1b4332"
_ROW_PENDING = "#4a3f1a"
_ROW_NOT_CLEARED = "#4a1f1f"
_TEXT_CLEARED = "#86efac"
_TEXT_PENDING = "#fde047"
_TEXT_NOT_CLEARED = "#fca5a5"

_COL_WIDTHS = (112, 116, 172, 172, 56, 118)
_ROW_H = 38
_PAD = 20
_TITLE_SIZE = 28
_BODY_SIZE = 16
_SMALL_SIZE = 14
_LEGEND = "Green row = Cleared   ·   Amber row = Waiting   ·   Red row = Not cleared"
_MAX_ROWS = 40

_FONT_DIR_CANDIDATES = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("C:/Windows/Fonts/segoeui.ttf"),
    Path("C:/Windows/Fonts/segoeuib.ttf"),
)


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


def _row_style(cleared: bool | None) -> tuple[str, str, str]:
    if cleared is None:
        return _ROW_PENDING, _BODY_TEXT, _TEXT_PENDING
    if cleared:
        return _ROW_CLEARED, _BODY_TEXT, _TEXT_CLEARED
    return _ROW_NOT_CLEARED, _BODY_TEXT, _TEXT_NOT_CLEARED


@lru_cache(maxsize=8)
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
    paths.extend(list(_FONT_DIR_CANDIDATES))
    for path in paths:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _fonts():
    return (
        _cached_font(_s(_TITLE_SIZE), True),
        _cached_font(_s(_BODY_SIZE), False),
        _cached_font(_s(_BODY_SIZE), True),
        _cached_font(_s(_SMALL_SIZE), False),
    )


def _fit_text(text: str, max_chars: int) -> str:
    if not text or len(text) <= max_chars:
        return text or ""
    if max_chars <= 1:
        return text[:max_chars]
    return text[: max_chars - 1] + "…"


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
    live: bool = False,
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

    title_block = _s(38)
    subtitle_block = _s(24) if subtitle else 0
    legend_block = _s(22)
    footer_lines = 1 + (1 if status_summary else 0) + (1 if hidden_count > 0 else 0)
    footer_block = _s(26) * footer_lines + _s(10)

    n_rows = len(shown) + 2
    table_h = (
        pad
        + title_block
        + subtitle_block
        + legend_block
        + (n_rows * row_h)
        + footer_block
        + pad
    )

    img = Image.new("RGB", (table_w, table_h), _BG)
    draw = ImageDraw.Draw(img)

    font_title, font_body, font_header, font_sm = _fonts()

    y = pad
    draw.text((pad, y), title, fill=_TITLE_COLOR, font=font_title)
    y += title_block
    if subtitle:
        draw.text((pad, y), subtitle, fill=_FOOTER_TEXT, font=font_sm)
        y += subtitle_block
    draw.text((pad, y), _LEGEND, fill=_LEGEND_TEXT, font=font_sm)
    y += legend_block

    x0 = pad
    col_x = [x0]
    for w in col_w[:-1]:
        col_x.append(col_x[-1] + w)

    grid_w = max(_s(1), _SCALE)
    char_limits = [max(4, w // _s(9)) for w in col_w]

    def draw_row(
        cells: list[str],
        *,
        bg: str,
        text_color: str,
        status_col_color: str | None = None,
        header: bool = False,
    ) -> None:
        nonlocal y
        f = font_header if header else font_body
        draw.rectangle((x0, y, x0 + sum(col_w), y + row_h), fill=bg)
        for i, (cell, w) in enumerate(zip(cells, col_w)):
            cx = col_x[i] + _s(10)
            cy = y + _s(10)
            color = status_col_color if i == 5 and status_col_color else text_color
            label = _fit_text(cell, char_limits[i])
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
        cells[5] = format_status_label(record.cleared)
        bg, txt, status_txt = _row_style(record.cleared)
        draw_row(cells, bg=bg, text_color=txt, status_col_color=status_txt)

    draw_row(totals, bg=_TOTAL_BG, text_color=_BODY_TEXT, header=True)

    if status_summary:
        draw.text((pad, y + _s(8)), status_summary, fill=_FOOTER_TEXT, font=font_sm)
        y += _s(26)

    if hidden_count > 0:
        note = f"{hidden_count} more payment{'s' if hidden_count != 1 else ''} not shown in this image"
        draw.text((pad, y + _s(8)), note, fill=_FOOTER_TEXT, font=font_sm)
        y += _s(26)

    footer = format_image_footer(live=live)
    draw.text((pad, y + _s(8)), footer, fill=_FOOTER_TEXT, font=font_sm)

    buf = BytesIO()
    # JPEG encodes much faster and uploads quicker; sharp enough at 2× scale.
    img.save(buf, format="JPEG", quality=90, optimize=False, subsampling=0)
    return buf.getvalue()
