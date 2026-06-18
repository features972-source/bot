"""Per-instance payment currency (each bot process sets symbol at startup)."""

from __future__ import annotations

import re

_SYMBOL = "£"
_PARSE_SYMBOLS = ("A$", "AU$", "£", "$", "€")

PAYMENT_AMOUNT = ""
PAYMENT_OUT_PATTERN: re.Pattern[str] | None = None
INLINE_PAYMENT_OUT_PATTERN: re.Pattern[str] | None = None
EXPENSE_LINE_PATTERN: re.Pattern[str] | None = None


def init_currency(symbol: str) -> None:
    global _SYMBOL
    text = symbol.strip()
    _SYMBOL = text or "£"
    _rebuild_patterns()


def currency_symbol() -> str:
    return _SYMBOL


def format_amount(amount: float) -> str:
    sym = _SYMBOL
    rounded = round(amount)
    if abs(amount - rounded) < 0.01:
        return f"{sym}{rounded:,}"
    return f"{sym}{amount:,.2f}"


def _currency_regex_fragment() -> str:
    symbols = list(_PARSE_SYMBOLS)
    if _SYMBOL not in symbols:
        symbols.insert(0, _SYMBOL)
    parts = "|".join(
        re.escape(s) for s in sorted(set(symbols), key=len, reverse=True)
    )
    return rf"(?:(?:{parts})\s*)?"


def _rebuild_patterns() -> None:
    global PAYMENT_AMOUNT, PAYMENT_OUT_PATTERN, INLINE_PAYMENT_OUT_PATTERN, EXPENSE_LINE_PATTERN
    PAYMENT_AMOUNT = (
        _currency_regex_fragment()
        + r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
        + _currency_regex_fragment()
        + r"([kKmM])?"
    )
    PAYMENT_OUT_PATTERN = re.compile(
        rf"^\s*{PAYMENT_AMOUNT}\s*out(?:\s+of|\s+too)?\s*[!?.]*\s*$",
        re.IGNORECASE,
    )
    INLINE_PAYMENT_OUT_PATTERN = re.compile(
        rf"(?:^|\s){PAYMENT_AMOUNT}\s*out(?:\s+of|\s+too)?\b",
        re.IGNORECASE,
    )
    EXPENSE_LINE_PATTERN = re.compile(
        rf"^\s*{PAYMENT_AMOUNT}\s+(\S.+?)\s*$",
        re.IGNORECASE,
    )


def _amount_from_match(match: re.Match[str]) -> float:
    amount = float(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").lower()
    if suffix == "k":
        amount *= 1000
    elif suffix == "m":
        amount *= 1_000_000
    return amount


def parse_expense_line(text: str) -> tuple[float, str, str] | None:
    """Parse messages like '£132 blast' → amount, reason, raw text."""
    if EXPENSE_LINE_PATTERN is None:
        return None
    stripped = (text or "").strip()
    if not stripped or stripped.startswith("/"):
        return None
    normalized = re.sub(r"\s+", " ", stripped)
    normalized = re.sub(r"(?<=\d),(?=\d)", "", normalized)
    match = EXPENSE_LINE_PATTERN.match(normalized)
    if match is None:
        return None
    reason = match.group(3).strip()
    if not reason:
        return None
    amount = _amount_from_match(match)
    if amount <= 0:
        return None
    return amount, reason, stripped


def parse_amount_candidates(stripped: str, normalized: str) -> list[str]:
    sym = _SYMBOL
    candidates = [
        stripped,
        normalized,
        f"{stripped} out",
        f"{stripped} out of",
        f"{normalized} out",
        f"{normalized} out of",
        f"{sym}{stripped} out",
        f"{sym}{stripped} out of",
        f"{stripped}{sym} out of",
    ]
    if sym != "$":
        candidates.extend((f"${stripped} out", f"${stripped} out of"))
    if sym.upper() != "A$":
        candidates.extend((f"A${stripped} out", f"A${stripped} out of"))
    return candidates


_rebuild_patterns()
