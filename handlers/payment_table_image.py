"""Render payment tables as PNG/JPEG images (Telegram dark theme + Excel row colours)."""

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
from money_format import format_amount

_SCALE = 2

_BG = "#17212b"
_HEADER_BG = "#232e3c"
_HEADER_TEXT = "#ffffff"
_BODY_TEXT = "#f5f7fa"
_GRID = "#3d4f5f"
_TOTAL_BG = "#232e3c"
_FOOTER_TEXT = "#c8d4de"
_FOOTER_BAR = "#1a2633"
_TITLE_COLOR = "#ffffff"
_LEGEND_TEXT = "#c8d4de"

_ROW_CLEARED = "#1b4332"
_ROW_PENDING = "#4a3f1a"
_ROW_NOT_CLEARED = "#4a1f1f"
_TEXT_CLEARED = "#4ade80"
_TEXT_PENDING = "#fbbf24"
_TEXT_NOT_CLEARED = "#f87171"

_COL_WIDTHS_COMPACT = (112, 116, 172, 172, 56, 118)
_COL_WIDTHS_FULL = (92, 90, 128, 128, 50, 76, 102, 110, 102)
_CLEARED_COL = 5
_ROW_H = 38
_PAD = 20
_TITLE_SIZE = 28
_BODY_SIZE = 16
_SMALL_SIZE = 14
_FOOTER_SIZE = 17
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
        _cached_font(_s(_FOOTER_SIZE), True),
    )


def _fit_text(text: str, max_chars: int) -> str:
    if not text or len(text) <= max_chars:
        return text or ""
    if max_chars <= 1:
        return text[:max_chars]
    return text[: max_chars - 1] + "…"


def _draw_text_line(
    draw,
    x: int,
    y: int,
    segments: list[tuple[str, str]],
    font,
) -> None:
    cx = x
    for text, color in segments:
        draw.text((cx, y), text, fill=color, font=font)
        cx += int(draw.textlength(text, font=font))


def _status_footer_segments(
    totals: tuple[float, int, float, int, float, int],
) -> list[tuple[str, str]]:
    pending_amount, pending_count, cleared_amount, cleared_count, nc_amount, nc_count = (
        totals
    )
    sep = ("   ·   ", _FOOTER_TEXT)
    return [
        ("Waiting: ", _TEXT_PENDING),
        (f"{format_amount(pending_amount)} ({pending_count})", _TEXT_PENDING),
        sep,
        ("Cleared: ", _TEXT_CLEARED),
        (f"{format_amount(cleared_amount)} ({cleared_count})", _TEXT_CLEARED),
        sep,
        ("Not cleared: ", _TEXT_NOT_CLEARED),
        (f"{format_amount(nc_amount)} ({nc_count})", _TEXT_NOT_CLEARED),
    ]


def render_payments_table_png(
    records: list[PaymentRecord],
    *,
    database_path: str,
    total_amount: float,
    total_count: int,
    lookup_records: list[PaymentRecord] | None = None,
    title: str,
    subtitle: str = "",
    status_totals: tuple[float, int, float, int, float, int] | None = None,
    hidden_count: int = 0,
    live: bool = False,
    full_excel: bool = True,
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
        records=records,
        full_excel=full_excel,
    )

    col_widths = _COL_WIDTHS_FULL if full_excel else _COL_WIDTHS_COMPACT
    col_w = [_s(w) for w in col_widths]
    row_h = _s(_ROW_H)
    pad = _s(_PAD)
    table_w = sum(col_w) + pad * 2

    title_block = _s(38)
    subtitle_block = _s(24) if subtitle else 0
    footer_line_h = _s(34)
    footer_lines = 0
    if status_totals:
        footer_lines += 1
    if hidden_count > 0:
        footer_lines += 1
    footer_lines += 1  # updated stamp
    footer_block = footer_line_h * footer_lines + _s(16)

    n_rows = len(shown) + 2
    table_h = pad + title_block + subtitle_block + (n_rows * row_h) + footer_block + pad

    img = Image.new("RGB", (table_w, table_h), _BG)
    draw = ImageDraw.Draw(img)

    font_title, font_body, font_header, font_sm, font_footer = _fonts()

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
            color = status_col_color if i == _CLEARED_COL and status_col_color else text_color
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

    draw_row(
        list(table_headers(full_excel=full_excel)),
        bg=_HEADER_BG,
        text_color=_HEADER_TEXT,
        header=True,
    )

    for record in shown:
        cells = payment_table_row(
            record,
            username_lookup=username_lookup,
            full_excel=full_excel,
        )
        bg, txt, status_txt = _row_style(record.cleared)
        draw_row(cells, bg=bg, text_color=txt, status_col_color=status_txt)

    draw_row(totals, bg=_TOTAL_BG, text_color=_BODY_TEXT, header=True)

    # Footer panel — bold, colour-coded summary
    footer_top = y + _s(6)
    draw.rectangle(
        (pad, footer_top, table_w - pad, table_h - pad),
        fill=_FOOTER_BAR,
    )
    fy = footer_top + _s(10)

    if status_totals:
        _draw_text_line(
            draw,
            pad + _s(8),
            fy,
            _status_footer_segments(status_totals),
            font_footer,
        )
        fy += footer_line_h

    if hidden_count > 0:
        note = f"+{hidden_count} more not shown in this image"
        draw.text(
            (pad + _s(8), fy),
            note,
            fill=_FOOTER_TEXT,
            font=font_footer,
        )
        fy += footer_line_h

    footer = format_image_footer(live=live)
    draw.text((pad + _s(8), fy), footer, fill=_TITLE_COLOR, font=font_footer)

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90, optimize=False, subsampling=0)
    return buf.getvalue()
