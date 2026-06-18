"""Render payment tables as mobile-friendly card pages or Excel-style group tables."""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from database import PaymentRecord
from handlers.payment_table import (
    build_username_lookup,
    format_cleared_label,
    format_image_footer,
    format_payment_date,
    format_status_label,
    payment_totals_table_row,
    sheet_user_label,
    table_headers,
)
from money_format import format_amount

_SCALE = 2
# Full phone width — Telegram scales images to chat width; wide source = readable text.
_MOBILE_W = 1080
_MAX_PAGE_H = 1500
_MAX_TABLE_H = 9800

_BG = "#0b0f14"
_SHEET_BG = "#121820"
_HEADER_BG = "#2d3748"
_HEADER_TEXT = "#f8fafc"
_BODY_TEXT = "#e8edf4"
_GRID = "#3d4a5c"
_ID_TEXT = "#60a5fa"
_ROW_CLEARED = "#14352a"
_ROW_PENDING = "#3d3520"
_ROW_NOT_CLEARED = "#3d2228"
_TOTAL_BG = "#1e2836"
_STAMP_TEXT = "#94a3b8"
_STATUS_CLEARED = "#34d399"
_STATUS_PENDING = "#fbbf24"
_STATUS_NOT = "#f87171"

_CARD_H = 88
_CARD_GAP = 8
_TITLE_SIZE = 26
_SUBTITLE_SIZE = 16
_BODY_SIZE = 20
_SMALL_SIZE = 17
_STAMP_SIZE = 15
_PAD = 20


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


def _canvas_w() -> int:
    return _s(_MOBILE_W)


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


def _row_style(cleared: bool | None) -> tuple[str, str, str]:
    if cleared is None:
        return _ROW_PENDING, _BODY_TEXT, _STATUS_PENDING
    if cleared:
        return _ROW_CLEARED, _BODY_TEXT, _STATUS_CLEARED
    return _ROW_NOT_CLEARED, _BODY_TEXT, _STATUS_NOT


def _status_label(record: PaymentRecord, *, full_excel: bool) -> str:
    if full_excel:
        return format_cleared_label(record.cleared)
    return format_status_label(record.cleared)


def _people_line(record: PaymentRecord, *, username_lookup: dict[int, str]) -> str:
    finisher = sheet_user_label(
        record.finisher_username,
        record.finisher_display_name,
        record.finisher_user_id,
        username_lookup=username_lookup,
    )
    if record.starter_user_id is None:
        return finisher
    starter = sheet_user_label(
        record.starter_username,
        record.starter_display_name,
        record.starter_user_id,
        username_lookup=username_lookup,
    )
    if record.starter_user_id == record.finisher_user_id:
        return f"{starter} · starter & finisher"
    return f"{starter} → {finisher}"


def _save_jpeg(img) -> bytes:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=93, optimize=False, subsampling=0)
    return buf.getvalue()


def _page_capacity(*, include_totals: bool) -> int:
    pad = _PAD * 2
    header = 72
    footer = 88 if include_totals else 36
    usable = _MAX_PAGE_H - pad - header - footer
    return max(1, usable // (_CARD_H + _CARD_GAP))


def render_payments_mobile_pages(
    records: list[PaymentRecord],
    *,
    database_path: str,
    total_amount: float,
    total_count: int,
    lookup_records: list[PaymentRecord] | None = None,
    title: str,
    subtitle: str = "",
    live: bool = False,
    full_excel: bool = False,
    total_label: str = "TOTAL",
) -> list[bytes]:
    """Full-width card layout for /payments and /alltimepayments (mobile-readable)."""
    from PIL import Image, ImageDraw

    username_lookup = build_username_lookup(
        database_path,
        lookup_records if lookup_records is not None else records,
    )
    shown = list(records)
    if not shown:
        return []

    per_page = _page_capacity(include_totals=False)
    page_chunks: list[list[PaymentRecord]] = []
    for i in range(0, len(shown), per_page):
        page_chunks.append(shown[i : i + per_page])

    total_pages = len(page_chunks)
    pages: list[bytes] = []
    width = _canvas_w()
    pad = _s(_PAD)
    card_h = _s(_CARD_H)
    card_gap = _s(_CARD_GAP)
    inner_w = width - pad * 2

    font_title = _cached_font(_s(_TITLE_SIZE), True)
    font_sub = _cached_font(_s(_SUBTITLE_SIZE), False)
    font_body = _cached_font(_s(_BODY_SIZE), False)
    font_bold = _cached_font(_s(_BODY_SIZE), True)
    font_sm = _cached_font(_s(_SMALL_SIZE), False)
    font_stamp = _cached_font(_s(_STAMP_SIZE), False)

    totals_text = payment_totals_table_row(
        total_amount=total_amount,
        total_count=total_count,
        records=records,
        full_excel=full_excel,
        total_label=total_label,
    )
    total_line = (
        f"{total_label}  {totals_text[2]}  ·  {totals_text[3]}"
        if len(totals_text) > 3
        else f"{total_label}  {format_amount(total_amount)}  ·  {total_count} payments"
    )

    for page_idx, chunk in enumerate(page_chunks):
        is_last = page_idx == total_pages - 1
        header_h = _s(72)
        footer_h = _s(88 if is_last else 36)
        body_h = len(chunk) * card_h + max(0, len(chunk) - 1) * card_gap
        height = pad + header_h + body_h + footer_h + pad

        img = Image.new("RGB", (width, height), _BG)
        draw = ImageDraw.Draw(img)
        y = pad

        page_title = title.upper() if title else "PAYMENTS"
        if total_pages > 1:
            page_title = f"{page_title}  ({page_idx + 1}/{total_pages})"
        draw.text((pad, y), page_title, fill=_HEADER_TEXT, font=font_title)
        y += _s(34)

        if subtitle:
            draw.text((pad, y), subtitle, fill=_STAMP_TEXT, font=font_sub)
            y += _s(24)
        else:
            y += _s(6)

        y += _s(8)

        for record in chunk:
            bg, txt, status_color = _row_style(record.cleared)
            status = _status_label(record, full_excel=full_excel)
            amount = format_amount(record.amount)
            date = format_payment_date(record.created_at)
            people = _people_line(record, username_lookup=username_lookup)
            pid = f"#{record.id}"

            draw.rounded_rectangle(
                (pad, y, pad + inner_w, y + card_h),
                radius=_s(10),
                fill=bg,
                outline=_GRID,
                width=max(1, _SCALE),
            )
            accent_w = _s(5)
            draw.rounded_rectangle(
                (pad, y, pad + accent_w, y + card_h),
                radius=_s(3),
                fill=status_color,
            )

            line1_y = y + _s(12)
            draw.text((pad + _s(16), line1_y), pid, fill=_ID_TEXT, font=font_bold)
            id_w = int(draw.textlength(pid, font=font_bold))
            draw.text(
                (pad + _s(16) + id_w + _s(14), line1_y),
                amount,
                fill=_HEADER_TEXT,
                font=font_bold,
            )
            status_w = int(draw.textlength(status, font=font_bold))
            draw.text(
                (pad + inner_w - _s(12) - status_w, line1_y),
                status,
                fill=status_color,
                font=font_bold,
            )

            line2_y = y + _s(38)
            meta = date
            if record.card_last4:
                meta = f"{date}  ·  ····{record.card_last4}"
            draw.text((pad + _s(16), line2_y), meta, fill=_STAMP_TEXT, font=font_sm)

            line3_y = y + _s(58)
            draw.text((pad + _s(16), line3_y), people, fill=txt, font=font_body)

            y += card_h + card_gap

        if is_last:
            draw.rounded_rectangle(
                (pad, y, pad + inner_w, y + _s(56)),
                radius=_s(8),
                fill=_TOTAL_BG,
                outline=_GRID,
                width=max(1, _SCALE),
            )
            draw.text(
                (pad + _s(16), y + _s(16)),
                total_line,
                fill=_HEADER_TEXT,
                font=font_bold,
            )
            y += _s(64)
            stamp = format_image_footer(live=live)
            stamp_w = int(draw.textlength(stamp, font=font_stamp))
            draw.text(
                (pad + inner_w - stamp_w, y),
                stamp,
                fill=_STAMP_TEXT,
                font=font_stamp,
            )
        else:
            cont = "continued on next page →"
            cont_w = int(draw.textlength(cont, font=font_stamp))
            draw.text(
                (pad + inner_w - cont_w, y),
                cont,
                fill=_STAMP_TEXT,
                font=font_stamp,
            )

        pages.append(_save_jpeg(img))

    return pages


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
    total_label: str = "TOTAL",
    mobile: bool = False,
) -> bytes | list[bytes]:
    if mobile or not full_excel:
        pages = render_payments_mobile_pages(
            records,
            database_path=database_path,
            total_amount=total_amount,
            total_count=total_count,
            lookup_records=lookup_records,
            title=title,
            subtitle=subtitle,
            live=live,
            full_excel=full_excel,
            total_label=total_label,
        )
        return pages[0] if len(pages) == 1 else pages

    from PIL import Image, ImageDraw

    from handlers.payment_table import payment_table_row

    _ = status_totals

    username_lookup = build_username_lookup(
        database_path,
        lookup_records if lookup_records is not None else records,
    )
    shown = list(records)
    totals = payment_totals_table_row(
        total_amount=total_amount,
        total_count=total_count,
        records=records,
        full_excel=True,
        total_label=total_label,
    )

    header_cells = list(table_headers(full_excel=True))
    body_rows = [
        payment_table_row(record, username_lookup=username_lookup, full_excel=True)
        for record in shown
    ]

    stamp_cells = [""] * len(header_cells)
    stamp_cells[-1] = format_image_footer(live=live)

    min_widths = (56, 100, 92, 130, 130, 56, 76, 108, 116, 108)
    probe = Image.new("RGB", (4, 4), _BG)
    probe_draw = ImageDraw.Draw(probe)
    col_w = _measure_col_widths(probe_draw, header_cells, body_rows, totals, min_widths)

    table_inner_w = sum(col_w)
    min_table_w = _canvas_w() - _s(_PAD) * 2
    if table_inner_w < min_table_w:
        extra = min_table_w - table_inner_w
        for idx in (3, 4, 8, 9):
            col_w[idx] += extra // 4

    pad = _s(_PAD)
    table_w = max(_canvas_w(), table_inner_w + pad * 2)
    x0 = (table_w - table_inner_w) // 2

    has_title = bool(title)
    title_block = _s(36) if has_title else 0
    row_h = _s(40)
    n_rows = len(shown) + 3
    table_h = pad + title_block + n_rows * row_h + pad

    if table_h > _MAX_TABLE_H:
        return render_payments_mobile_pages(
            records,
            database_path=database_path,
            total_amount=total_amount,
            total_count=total_count,
            lookup_records=lookup_records,
            title=title,
            subtitle=subtitle,
            live=live,
            full_excel=True,
            total_label=total_label,
        )

    img = Image.new("RGB", (table_w, table_h), _BG)
    draw = ImageDraw.Draw(img)

    font_title = _cached_font(_s(_TITLE_SIZE), True)
    font_body = _cached_font(_s(18), False)
    font_header = _cached_font(_s(18), True)
    font_sm = _cached_font(_s(_SUBTITLE_SIZE), False)
    font_stamp = _cached_font(_s(_STAMP_SIZE), False)

    y = pad
    if title:
        draw.text((x0, y), title, fill=_HEADER_TEXT, font=font_title)
        if subtitle:
            draw.text((x0, y + _s(30)), subtitle, fill=_STAMP_TEXT, font=font_sm)
        y += title_block

    col_x = [x0]
    for w in col_w[:-1]:
        col_x.append(col_x[-1] + w)

    text_y_off = _s(10)
    grid_w = max(1, _SCALE)

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
            inner_w = max(_s(10), w - _s(24))
            if i == 0 and cell and not header and not total:
                color = _ID_TEXT
                f_cell = font_header
            elif stamp and i == len(col_w) - 1:
                color = _STAMP_TEXT
                f_cell = font_stamp
            else:
                color = text_color
                f_cell = f
            label = _fit_text(draw, cell, f_cell, inner_w)
            cx = col_x[i] + _s(10)
            if stamp and i == len(col_w) - 1:
                text_w = int(draw.textlength(label, font=f_cell))
                cx = col_x[i] + w - text_w - _s(10)
            draw.text((cx, y + text_y_off), label, fill=color, font=f_cell)
            draw.line((col_x[i] + w, y, col_x[i] + w, y + row_h), fill=_GRID, width=grid_w)
        draw.line((x0, y + row_h, x0 + table_inner_w, y + row_h), fill=_GRID, width=grid_w)
        y += row_h

    draw_row(header_cells, bg=_HEADER_BG, text_color=_HEADER_TEXT, header=True)
    for record, cells in zip(shown, body_rows):
        bg, txt, _ = _row_style(record.cleared)
        draw_row(cells, bg=bg, text_color=txt)
    draw_row(totals, bg=_TOTAL_BG, text_color=_HEADER_TEXT, total=True)
    draw_row(stamp_cells, bg=_SHEET_BG, text_color=_STAMP_TEXT, stamp=True)

    return _save_jpeg(img)


def _fit_text(draw, text: str, font, max_width: int) -> str:
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    trimmed = text
    while trimmed and draw.textlength(trimmed + "…", font=font) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + "…") if trimmed else "…"


def _measure_col_widths(draw, header, body_rows, totals, min_widths) -> list[int]:
    widths = [_s(w) for w in min_widths]
    font_body = _cached_font(_s(18), False)
    font_header = _cached_font(_s(18), True)
    pad = _s(24)
    rows = [(header, True), *[(r, False) for r in body_rows], (totals, True)]
    for cells, is_header in rows:
        font = font_header if is_header else font_body
        for i, cell in enumerate(cells):
            if i >= len(widths) or not cell:
                continue
            widths[i] = max(widths[i], int(draw.textlength(cell, font=font)) + pad)
    return widths
