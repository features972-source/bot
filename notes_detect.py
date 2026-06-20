"""Detect starter notes messages for pass-queue handoff."""

from __future__ import annotations

import re

NOTES_MARKER = re.compile(r"(?i)^(?:📝\s*)?notes?\b")

FIELD_LABEL = re.compile(
    r"(?i)^(?:"
    r"name|full name|dob|date of birth|card|card number|sort|sort code|"
    r"address|phone|mobile|email|postcode|post code|account|bank|"
    r"ni|national insurance|mother(?:'?s)? maiden|mmn|"
    r"limit|amount|balance|otp|pin|expiry|cvv|"
    r"customer|client|lead"
    r")\s*[:.\-]"
)

OUT_PATTERN = re.compile(r"\b\d[\d,.\s]*(?:k|m)?\s+out\b", re.IGNORECASE)

DOB_PATTERN = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")

UK_POSTCODE = re.compile(
    r"(?i)\b(?:"
    r"[A-Z]{1,2}\d{1,2}[A-Z]?\s+\d[A-Z]{2}|"  # BN1 3WF
    r"[A-Z]{2}\d{2}[A-Z]{3}|"  # BK19CSS (compact)
    r"[A-Z]{1,2}\d{1,2}[A-Z]?\d[A-Z]{2}"  # BN13WF (no space)
    r")\b"
)

FINANCIAL_KEYWORD = re.compile(
    r"(?i)\b(?:"
    r"barclay(?:card|s)?|apay|apple\s*pay|has\s+apay|online(?:\s+banking)?|"
    r"no\s+online\s+banking|credit|debit|coin|banking|bank|stiff|"
    r"sort\s*code|account|work\s+address"
    r")\b"
)

AMOUNT_K_PATTERN = re.compile(
    r"(?i)(?:around\s+)?\d[\d,.\s]*(?:k|m)(?:\s*[-–]\s*\d[\d,.\s]*(?:k|m))?"
)

NAME_LIKE_LINE = re.compile(
    r"^[A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]+)+$"
)


def _structured_notes(lines: list[str]) -> bool:
    if NOTES_MARKER.match(lines[0]):
        return True
    label_hits = sum(1 for line in lines if FIELD_LABEL.match(line))
    if label_hits >= 2:
        return True
    return len(lines) >= 4 and label_hits >= 1


def _freeform_notes(text: str, lines: list[str]) -> bool:
    if len(lines) < 3:
        return False

    has_dob = bool(DOB_PATTERN.search(text))
    has_postcode = bool(UK_POSTCODE.search(text))
    has_financial = bool(FINANCIAL_KEYWORD.search(text))
    has_amount_k = bool(AMOUNT_K_PATTERN.search(text))
    has_name_line = bool(NAME_LIKE_LINE.match(lines[0]))

    if has_dob and has_financial:
        return True
    if has_postcode and has_financial:
        return True
    if has_postcode and has_amount_k:
        return True
    if has_amount_k and has_financial and len(lines) >= 4:
        return True
    if has_name_line and has_dob and len(lines) >= 3:
        return True
    if has_name_line and has_postcode and len(lines) >= 3:
        return True

    signal_count = sum(
        (
            has_dob,
            has_postcode,
            has_financial,
            has_amount_k,
        )
    )
    return signal_count >= 2 and len(lines) >= 4


def looks_like_notes(text: str | None) -> bool:
    if not text:
        return False
    cleaned = text.strip()
    if len(cleaned) < 15 or OUT_PATTERN.search(cleaned):
        return False

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if len(lines) < 2:
        return False

    if _structured_notes(lines):
        return True
    return _freeform_notes(cleaned, lines)
