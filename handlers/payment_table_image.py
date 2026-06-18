"""Render payment tables as PNG/JPEG — futuristic neon dashboard style."""

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
_MAX_CANVAS_HEIGHT_PX = 9800

# ── Futuristic palette ──────────────────────────────────────────────────────
_BG_TOP = "#030712"
_BG_BOTTOM = "#0a0f2e"
_CARD_BORDER = "#1e3a8a"
_CARD_FILL = "#070d1f"
_HEADER_TOP = "#0e7490"
_HEADER_BOTTOM = "#4338ca"
_ACCENT_PURPLE = "#a855f7"
_TITLE = "#22d3ee"
_SUBTITLE = "#64748b"
_BODY = "#e2e8f0"
_AMOUNT = "#67e8f9"
_HEADER_TEXT = "#f0f9ff"
_GRID = "#1e293b"
_GRID_GLOW = "#0ea5e9"
_TOTAL_TOP = "#1e1b4b"
_TOTAL_BOTTOM = "#312e81"
_TOTAL_TEXT = "#c4b5fd"
_FOOTER_BG = "#04060f"
_FOOTER_LINE = "#06b6d4"
_FOOTER_TEXT = "#94a3b8"
_LIVE_BADGE_BG = "#052e16"
_LIVE_BADGE_TEXT = "#4ade80"
_LIVE_BADGE_BORDER = "#22c55e"

_ROW_CLEARED = "#041f18"
_ROW_PENDING = "#1a1508"
_ROW_NOT_CLEARED = "#1f0808"
_ROW_ALT = "#0a1020"
_ROW_BASE = "#070d1a"
_ACCENT_CLEARED = "#10b981"
_ACCENT_PENDING = "#fbbf24"
_ACCENT_NOT = "#f43f5e"
_TEXT_CLEARED = "#34d399"
_TEXT_PENDING = "#fcd34d"
_TEXT_NOT_CLEARED = "#fb7185"

_MIN_COL_WIDTHS_COMPACT = (44, 100, 96, 120, 120, 52, 96)
_MIN_COL_WIDTHS_FULL = (44, 96, 88, 120, 120, 52, 72, 92, 100, 92)
_ID_COL = 0
_AMOUNT_COL = 1
_CLEARED_COL = 6
_ID_TEXT = "#c084fc"
_ROW_H = 38
_PAD = 22
_HERO_H = 56
_TITLE_SIZE = 30
_BODY_SIZE = 15
_SMALL_SIZE = 13
_FOOTER_SIZE = 16
_CELL_H_PAD = 22
_CORNER_R = 14

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


def _hex_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _lerp_color(a: str, b: str, t: float) -> str:
    ar, ag, ab = _hex_rgb(a)
    br, bg, bb = _hex_rgb(b)
    t = max(0.0, min(1.0, t))
    r = int(ar + (br - ar) * t)
    g = int(ag + (bg - ag) * t)
    bl = int(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


def _fill_vertical_gradient(
    draw,
    box: tuple[int, int, int, int],
    top: str,
    bottom: str,
) -> None:
    x0, y0, x1, y1 = box
    height = max(y1 - y0, 1)
    for i in range(height):
        t = i / max(height - 1, 1)
        draw.line((x0, y0 + i, x1, y0 + i), fill=_lerp_color(top, bottom, t))


def _row_style(
    cleared: bool | None,
    *,
    alt: bool,
) -> tuple[str, str, str, str]:
    base = _ROW_ALT if alt else _ROW_BASE
    if cleared is None:
        return _ROW_PENDING, _BODY, _TEXT_PENDING, _ACCENT_PENDING
    if cleared:
        return _ROW_CLEARED, _BODY, _TEXT_CLEARED, _ACCENT_CLEARED
    return _ROW_NOT_CLEARED, _BODY, _TEXT_NOT_CLEARED, _ACCENT_NOT


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


def _fit_text_width(draw, text: str, font, max_width: int) -> str:
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
    ncols = len(min_widths)
    widths = [_s(w) for w in min_widths]
    font_body = _cached_font(_s(_BODY_SIZE), False)
    font_header = _cached_font(_s(_BODY_SIZE), True)
    pad = _s(_CELL_H_PAD)

    for cells, is_header in rows:
        font = font_header if is_header else font_body
        for i in range(min(ncols, len(cells))):
            cell = cells[i]
            if not cell:
                continue
            need = int(draw.textlength(cell, font=font)) + pad
            widths[i] = max(widths[i], need)
    return widths


def _pick_row_height(num_data_rows: int, *, has_subtitle: bool, footer_lines: int) -> int:
    hero = _s(_HERO_H)
    subtitle_block = _s(22) if has_subtitle else 0
    footer_block = _s(36) * footer_lines + _s(20)
    fixed = _s(_PAD) * 2 + hero + subtitle_block + footer_block + _s(12)
    table_rows = num_data_rows + 2
    for row_h in (_s(_ROW_H), _s(34), _s(30), _s(26)):
        if fixed + table_rows * row_h <= _MAX_CANVAS_HEIGHT_PX:
            return row_h
    available = _MAX_CANVAS_HEIGHT_PX - fixed
    return max(_s(22), available // max(table_rows, 1))


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
    sep = ("  │  ", "#334155")
    return [
        ("◆ WAIT ", _TEXT_PENDING),
        (f"{format_amount(pending_amount)} ({pending_count})", _TEXT_PENDING),
        sep,
        ("◆ CLR ", _TEXT_CLEARED),
        (f"{format_amount(cleared_amount)} ({cleared_count})", _TEXT_CLEARED),
        sep,
        ("◆ OUT ", _TEXT_NOT_CLEARED),
        (f"{format_amount(nc_amount)} ({nc_count})", _TEXT_NOT_CLEARED),
    ]


def _draw_hero(
    draw,
    *,
    x: int,
    y: int,
    width: int,
    title: str,
    subtitle: str,
    live: bool,
    font_title,
    font_sm,
) -> int:
    """Title block with neon accent — returns height used."""
    h = _s(_HERO_H)
    draw.text((x, y + _s(4)), "◈", fill=_ACCENT_PENDING, font=font_title)
    title_x = x + _s(28)
    draw.text((title_x, y + _s(2)), title.upper(), fill=_TITLE, font=font_title)

    if live:
        badge = " ● LIVE "
        badge_w = int(draw.textlength(badge, font=font_sm)) + _s(16)
        badge_x = x + width - badge_w
        badge_y = y + _s(6)
        draw.rounded_rectangle(
            (badge_x, badge_y, badge_x + badge_w, badge_y + _s(22)),
            radius=_s(8),
            fill=_LIVE_BADGE_BG,
            outline=_LIVE_BADGE_BORDER,
            width=max(1, _SCALE),
        )
        draw.text(
            (badge_x + _s(8), badge_y + _s(3)),
            badge.strip(),
            fill=_LIVE_BADGE_TEXT,
            font=font_sm,
        )

    if subtitle:
        draw.text(
            (title_x, y + _s(32)),
            subtitle,
            fill=_SUBTITLE,
            font=font_sm,
        )

    line_y = y + h - _s(4)
    draw.line((x, line_y, x + width, line_y), fill=_GRID_GLOW, width=_SCALE)
    draw.line((x, line_y + _s(1), x + width // 3, line_y + _s(1)), fill=_ACCENT_PURPLE, width=1)
    return h


def _draw_card_frame(draw, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    r = _s(_CORNER_R)
    draw.rounded_rectangle(box, radius=r, fill=_CARD_FILL, outline=_CARD_BORDER, width=_SCALE)
    # Top highlight edge
    draw.line((x0 + r, y0 + 1, x1 - r, y0 + 1), fill=_GRID_GLOW, width=max(1, _SCALE))


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
    live: bool = False,
    full_excel: bool = True,
    total_label: str = "WEEK TOTAL",
) -> bytes:
    from PIL import Image, ImageDraw

    username_lookup = build_username_lookup(
        database_path,
        lookup_records if lookup_records is not None else records,
    )
    shown = list(records)
    totals = payment_totals_table_row(
        total_amount=total_amount,
        total_count=total_count,
        records=records,
        full_excel=full_excel,
        total_label=total_label,
    )

    header_cells = list(table_headers(full_excel=full_excel))
    body_rows: list[list[str]] = []
    for record in shown:
        body_rows.append(
            payment_table_row(
                record,
                username_lookup=username_lookup,
                full_excel=full_excel,
            )
        )

    measure_rows: list[tuple[list[str], bool]] = [
        (header_cells, True),
        *[(row, False) for row in body_rows],
        (totals, True),
    ]
    min_widths = _MIN_COL_WIDTHS_FULL if full_excel else _MIN_COL_WIDTHS_COMPACT

    probe = Image.new("RGB", (4, 4), _BG_TOP)
    probe_draw = ImageDraw.Draw(probe)
    col_w = _measure_col_widths(probe_draw, measure_rows, min_widths=min_widths)

    pad = _s(_PAD)
    table_inner_w = sum(col_w)
    table_w = table_inner_w + pad * 2

    footer_lines = 1
    if status_totals:
        footer_lines += 1
    footer_line_h = _s(36)
    footer_block = footer_line_h * footer_lines + _s(20)

    hero_h = _s(_HERO_H)
    subtitle_extra = _s(4) if subtitle else 0
    row_h = _pick_row_height(
        len(shown),
        has_subtitle=bool(subtitle),
        footer_lines=footer_lines,
    )

    n_rows = len(shown) + 2
    table_body_h = n_rows * row_h
    table_h = pad + hero_h + subtitle_extra + table_body_h + footer_block + pad

    img = Image.new("RGB", (table_w, table_h), _BG_TOP)
    draw = ImageDraw.Draw(img)
    _fill_vertical_gradient(draw, (0, 0, table_w, table_h), _BG_TOP, _BG_BOTTOM)

    font_title, font_body, font_header, font_sm, font_footer = _fonts()

    y = pad
    y += _draw_hero(
        draw,
        x=pad,
        y=y,
        width=table_inner_w,
        title=title,
        subtitle=subtitle,
        live=live,
        font_title=font_title,
        font_sm=font_sm,
    )
    y += subtitle_extra

    card_top = y
    card_box = (pad, card_top, pad + table_inner_w, card_top + table_body_h)
    _draw_card_frame(draw, card_box)

    x0 = pad
    col_x = [x0]
    for w in col_w[:-1]:
        col_x.append(col_x[-1] + w)

    grid_w = max(_s(1), _SCALE)
    text_y_offset = max(_s(6), (row_h - _s(_BODY_SIZE)) // 2)
    accent_w = _s(4)

    def draw_header_row(cells: list[str]) -> None:
        nonlocal y
        _fill_vertical_gradient(
            draw,
            (x0, y, x0 + table_inner_w, y + row_h),
            _HEADER_TOP,
            _HEADER_BOTTOM,
        )
        draw.line((x0, y, x0 + table_inner_w, y), fill=_GRID_GLOW, width=grid_w)
        for i, w in enumerate(col_w):
            cell = cells[i] if i < len(cells) else ""
            cx = col_x[i] + _s(10)
            cy = y + text_y_offset
            inner_w = max(_s(8), w - _s(_CELL_H_PAD))
            label = _fit_text_width(draw, cell, font_header, inner_w)
            draw.text((cx, cy), label.upper(), fill=_HEADER_TEXT, font=font_header)
            if i < len(col_w) - 1:
                gx = col_x[i] + w
                draw.line((gx, y + _s(4), gx, y + row_h - _s(4)), fill=_GRID, width=grid_w)
        draw.line((x0, y + row_h, x0 + table_inner_w, y + row_h), fill=_GRID_GLOW, width=grid_w)
        y += row_h

    def draw_body_row(
        cells: list[str],
        *,
        bg: str,
        text_color: str,
        status_col_color: str | None,
        accent: str,
    ) -> None:
        nonlocal y
        draw.rectangle((x0, y, x0 + table_inner_w, y + row_h), fill=bg)
        draw.rectangle((x0, y, x0 + accent_w, y + row_h), fill=accent)
        for i, w in enumerate(col_w):
            cell = cells[i] if i < len(cells) else ""
            cx = col_x[i] + _s(10)
            cy = y + text_y_offset
            if i == _CLEARED_COL and status_col_color:
                color = status_col_color
            elif i == _AMOUNT_COL:
                color = _AMOUNT
            elif i == _ID_COL:
                color = _ID_TEXT
            else:
                color = text_color
            inner_w = max(_s(8), w - _s(_CELL_H_PAD))
            f = font_header if i in (_AMOUNT_COL, _ID_COL) else font_body
            label = _fit_text_width(draw, cell, f, inner_w)
            draw.text((cx, cy), label, fill=color, font=f)
            if i < len(col_w) - 1:
                gx = col_x[i] + w
                draw.line((gx, y + _s(3), gx, y + row_h - _s(3)), fill=_GRID, width=1)
        draw.line((x0, y + row_h, x0 + table_inner_w, y + row_h), fill=_GRID, width=1)
        y += row_h

    def draw_total_row(cells: list[str]) -> None:
        nonlocal y
        _fill_vertical_gradient(
            draw,
            (x0, y, x0 + table_inner_w, y + row_h),
            _TOTAL_TOP,
            _TOTAL_BOTTOM,
        )
        draw.line((x0, y, x0 + table_inner_w, y), fill=_ACCENT_PURPLE, width=grid_w)
        for i, w in enumerate(col_w):
            cell = cells[i] if i < len(cells) else ""
            cx = col_x[i] + _s(10)
            cy = y + text_y_offset
            color = _AMOUNT if i == _AMOUNT_COL else _TOTAL_TEXT
            f = font_header
            inner_w = max(_s(8), w - _s(_CELL_H_PAD))
            label = _fit_text_width(draw, cell, f, inner_w)
            draw.text((cx, cy), label, fill=color, font=f)
            if i < len(col_w) - 1:
                gx = col_x[i] + w
                draw.line((gx, y + _s(4), gx, y + row_h - _s(4)), fill="#4c1d95", width=1)
        draw.line((x0, y + row_h, x0 + table_inner_w, y + row_h), fill=_ACCENT_PURPLE, width=grid_w)
        y += row_h

    draw_header_row(header_cells)

    for idx, (record, cells) in enumerate(zip(shown, body_rows)):
        bg, txt, status_txt, accent = _row_style(record.cleared, alt=idx % 2 == 1)
        draw_body_row(cells, bg=bg, text_color=txt, status_col_color=status_txt, accent=accent)

    draw_total_row(totals)

    footer_top = y + _s(8)
    footer_box = (pad, footer_top, table_w - pad, table_h - pad)
    draw.rounded_rectangle(footer_box, radius=_s(10), fill=_FOOTER_BG, outline=_FOOTER_LINE, width=1)
    draw.line(
        (pad + _s(10), footer_top + 1, table_w - pad - _s(10), footer_top + 1),
        fill=_FOOTER_LINE,
        width=max(1, _SCALE),
    )
    fy = footer_top + _s(12)

    if status_totals:
        _draw_text_line(
            draw,
            pad + _s(14),
            fy,
            _status_footer_segments(status_totals),
            font_footer,
        )
        fy += footer_line_h

    footer = format_image_footer(live=live)
    draw.text((pad + _s(14), fy), f"◇ {footer}", fill=_FOOTER_TEXT, font=font_footer)

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=92, optimize=False, subsampling=0)
    return buf.getvalue()
