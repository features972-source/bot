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

DOB_PATTERN = re.compile(
    r"\b\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}\b"
)

UK_POSTCODE = re.compile(
    r"(?i)\b(?:"
    r"[A-Z]{1,2}\d{1,2}[A-Z]?\s+\d[A-Z]{2}|"
    r"[A-Z]{2}\d{2}[A-Z]{3}|"
    r"[A-Z]{1,2}\d{1,2}[A-Z]?\d[A-Z]{2}"
    r")\b"
)

FINANCIAL_KEYWORD = re.compile(
    r"(?i)\b(?:"
    r"barclay(?:card|s)?|hsbc|natwest|lloyds|santander|halifax|monzo|"
    r"revolut|starling|nationwide|tsb|metro|capital\s*one|"
    r"visa|mastercard|amex|credit|debit|bank(?:ing|er)?|"
    r"apay|apple\s*pay|gpay|google\s*pay|has\s+apay|"
    r"online(?:\s+banking)?|no\s+online\s+banking|"
    r"coin|stiff|sort\s*code|account|savers?|balance|"
    r"work\s+address|overdraft|loan|mortgage|paypal"
    r")\b"
)

AMOUNT_PATTERN = re.compile(
    r"(?i)(?:"
    r"(?:around|about|approx|balance|savers?\s+with|has|with)\s+)?"
    r"\d[\d,.\s]*(?:k|m)?(?:\s*[-–]\s*\d[\d,.\s]*(?:k|m)?)?"
)

YES_NO_LINE = re.compile(r"(?i)^(?:apay|online|coin|banking|credit|debit)\s*[-:]\s*(?:yes|no)\b")

INFO_LINE = re.compile(
    r"(?i)(?:"
    r"\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}|"
    r"barclay|hsbc|bank|credit|debit|apay|online|coin|balance|saver|"
    r"stiff|visa|mastercard|\d{3,}|\d+k\b|yes|no"
    r")"
)

CHAT_LINE = re.compile(
    r"(?i)^(?:"
    r"lol|lmao|haha|ok(?:ay)?|yes|no|thanks|thank you|cheers|nice|cool|"
    r"hey|hi|hello|gm|good morning|good night|see you|brb|"
    r"👍|🤣|😂|🙏"
    r")[\s!.]*$"
)

NOT_NAME_PHRASE = re.compile(
    r"(?i)^(see you|thank|thanks|hey there|good morning|good night|how are)\b"
)

PERSON_NAME_LINE = re.compile(
    r"^[A-Za-z][A-Za-z'\-]+(?:\s+[A-Za-z][A-Za-z'\-\.]+)+$"
)


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _structured_notes(lines: list[str]) -> bool:
    if NOTES_MARKER.match(lines[0]):
        return True
    label_hits = sum(1 for line in lines if FIELD_LABEL.match(line))
    if label_hits >= 1 and len(lines) >= 2:
        return True
    return label_hits >= 2


def _person_name_first(line: str) -> bool:
    stripped = line.strip()
    if CHAT_LINE.match(stripped) or NOT_NAME_PHRASE.match(stripped):
        return False
    if PERSON_NAME_LINE.match(stripped):
        return True
    words = stripped.split()
    return (
        len(words) >= 2
        and len(stripped) <= 48
        and all(re.match(r"^[A-Za-z][A-Za-z'\-]+$", word) for word in words)
    )


def _info_line_count(lines: list[str]) -> int:
    count = 0
    for line in lines:
        if INFO_LINE.search(line) or FINANCIAL_KEYWORD.search(line):
            count += 1
        elif YES_NO_LINE.match(line.strip()):
            count += 1
        elif DOB_PATTERN.search(line):
            count += 1
    return count


def _has_content_signal(text: str) -> bool:
    return bool(
        DOB_PATTERN.search(text)
        or UK_POSTCODE.search(text)
        or FINANCIAL_KEYWORD.search(text)
        or AMOUNT_PATTERN.search(text)
    )


def _looks_like_casual_chat(lines: list[str]) -> bool:
    if not lines:
        return True
    if _has_content_signal("\n".join(lines)):
        return False
    informal = 0
    for line in lines:
        if CHAT_LINE.match(line.strip()):
            informal += 1
        elif len(line.strip()) < 18 and not re.search(r"\d", line):
            informal += 1
    return informal >= len(lines)


def _queue_waiting_notes(lines: list[str], cleaned: str) -> bool:
    """When someone is waiting in queue, treat obvious multi-line blocks as notes."""
    if len(lines) < 2 or len(cleaned) < 12:
        return False
    if _looks_like_casual_chat(lines):
        return False
    if len(lines) >= 2:
        return True
    return False


def _freeform_notes(text: str, lines: list[str]) -> bool:
    line_count = len(lines)
    if line_count < 2:
        return False

    info_lines = _info_line_count(lines)
    has_signal = _has_content_signal(text)
    name_first = _person_name_first(lines[0])

    if line_count >= 2 and has_signal:
        return True
    if line_count >= 2 and info_lines >= 1:
        return True
    if line_count >= 3 and _looks_like_casual_chat(lines):
        return False
    if line_count >= 3 and name_first:
        return True
    if line_count >= 3 and info_lines >= 1:
        return True
    if line_count >= 2 and name_first and info_lines >= 1:
        return True
    if line_count >= 4:
        return not _looks_like_casual_chat(lines)
    if line_count >= 5:
        return True

    return False


def looks_like_notes(text: str | None, *, queue_waiting: bool = False) -> bool:
    if not text:
        return False
    cleaned = text.strip()
    if len(cleaned) < 12 or OUT_PATTERN.search(cleaned):
        return False
    if cleaned.startswith("/"):
        return False

    lines = _non_empty_lines(cleaned)
    if not lines:
        return False

    if queue_waiting and _queue_waiting_notes(lines, cleaned):
        return True

    if _structured_notes(lines):
        return True
    return _freeform_notes(cleaned, lines)
