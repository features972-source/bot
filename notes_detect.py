"""Detect starter notes messages for pass-queue handoff."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

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

BALANCE_AMOUNT = r"£?\s*\d[\d,.\s]*(?:k|m)?"
BALANCE_KEYWORD = r"(?:current|savings?|balance|bala)"

BALANCE_IN_NOTES = re.compile(
    r"(?i)(?:"
    rf"{BALANCE_KEYWORD}\s*[:.\-]?\s*{BALANCE_AMOUNT}"
    r"|(?:lt|last\s+transaction)\s*[:.\-]?\s*£?\s*\d[\d,.\s]*(?:k|m)?"
    r"|savers?\s+with\s+£?\s*\d[\d,.\s]*(?:k|m)?"
    rf"|{BALANCE_AMOUNT}\s+{BALANCE_KEYWORD}\b"
    r"|£\s*\d[\d,.\s]*(?:\.\d{1,2})?(?:\s*(?:k|m))?\b"
    r")"
)

STANDALONE_MONEY_LINE = re.compile(
    r"(?i)^(?:"
    r"£\s*\d[\d,.\s]*(?:\.\d{1,2})?(?:\s*(?:k|m))?\s*"
    r"|\d{3,}[\d,.\s]*(?:\.\d{1,2})?(?:\s*(?:k|m))?\s*"
    r")$"
)

BALANCE_ONLY_LINE = re.compile(
    r"(?i)^(?:"
    rf"{BALANCE_KEYWORD}\s*[:.\-]?\s*{BALANCE_AMOUNT}\s*"
    rf"|{BALANCE_AMOUNT}\s+{BALANCE_KEYWORD}\b\s*"
    r"|(?:lt|last\s+transaction)\s*[:.\-]?\s*£?\s*\d[\d,.\s]*(?:k|m)?\s*"
    r"|savers?\s+with\s+£?\s*\d[\d,.\s]*(?:k|m)?\s*"
    r"|£\s*\d[\d,.\s]*(?:\.\d{1,2})?(?:\s*(?:k|m))?\s*"
    r")$"
)


def notes_has_balance(text: str | None) -> bool:
    if not text:
        return False
    cleaned = text.strip()
    if BALANCE_IN_NOTES.search(cleaned):
        return True
    return any(STANDALONE_MONEY_LINE.match(line) for line in _non_empty_lines(cleaned))


def notes_balance_only(text: str | None) -> bool:
    """True when the message is only balance lines with no full customer notes."""
    if not text or not notes_has_balance(text):
        return False
    lines = _non_empty_lines(text.strip())
    if not lines:
        return False
    return all(BALANCE_ONLY_LINE.match(line) for line in lines)


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
    """Someone is waiting — multi-line paste or single-line notes with balance."""
    if len(cleaned) < 10:
        return False
    if len(lines) == 1:
        return notes_has_balance(cleaned) and bool(_has_content_signal(cleaned))
    return not _looks_like_casual_chat(lines)


def _freeform_notes(text: str, lines: list[str]) -> bool:
    line_count = len(lines)
    if line_count < 2:
        return False

    info_lines = _info_line_count(lines)
    has_signal = _has_content_signal(text)
    name_first = _person_name_first(lines[0])

    if has_signal:
        return True
    if info_lines >= 1:
        return True
    if line_count >= 3 and name_first:
        return True
    if line_count >= 4 and not _looks_like_casual_chat(lines):
        return True
    return False


def looks_like_notes(text: str | None, *, queue_waiting: bool = False) -> bool:
    if not text:
        return False
    cleaned = text.strip()
    if len(cleaned) < 10 or OUT_PATTERN.search(cleaned):
        return False
    if cleaned.startswith("/"):
        return False

    lines = _non_empty_lines(cleaned)
    if not lines:
        return False

    if queue_waiting and _queue_waiting_notes(lines, cleaned):
        return True

    if len(lines) == 1 and notes_has_balance(cleaned) and _has_content_signal(cleaned):
        return True

    if _structured_notes(lines):
        return True
    return _freeform_notes(cleaned, lines)


BANK_NAME = re.compile(
    r"(?i)\b("
    r"barclay(?:card|s)?|hsbc|natwest|lloyds|santander|halifax|monzo|"
    r"revolut|starling|nationwide|tsb|metro|capital\s*one"
    r")\b"
)

BALANCE_SNIPPET = re.compile(
    r"(?i)(?:"
    rf"{BALANCE_KEYWORD}\s*[:.\-]?\s*{BALANCE_AMOUNT}"
    rf"|{BALANCE_AMOUNT}\s+{BALANCE_KEYWORD}\b"
    r"|£\s*\d[\d,.\s]*(?:\.\d{1,2})?(?:\s*(?:k|m))?\b"
    r"|savers?\s+with\s+£?\s*\d[\d,.\s]*(?:k|m)?"
    r")"
)

ONLINE_STATUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"no\s+online\s+banking", re.I), "No online banking"),
    (re.compile(r"no\s+online", re.I), "No online"),
    (re.compile(r"no\s+app", re.I), "No app"),
    (re.compile(r"online(?:\s+banking)?\s*[-:]\s*no", re.I), "No online banking"),
    (re.compile(r"banking\s*[-:]\s*no", re.I), "No online banking"),
    (re.compile(r"online(?:\s+banking)?\s*[-:]\s*yes", re.I), "Online banking"),
    (re.compile(r"has\s+online(?:\s+banking)?", re.I), "Has online banking"),
)


@dataclass
class NotesPassSummary:
    balance: str | None = None
    dob: str | None = None
    bank: str | None = None
    online: str | None = None


def _extract_balance_summary(text: str) -> str | None:
    seen: set[str] = set()
    parts: list[str] = []
    for line in _non_empty_lines(text):
        stripped = line.strip()
        if STANDALONE_MONEY_LINE.match(stripped):
            key = stripped.lower()
            if key not in seen:
                seen.add(key)
                parts.append(stripped)
            continue
        for match in BALANCE_SNIPPET.finditer(stripped):
            snippet = match.group(0).strip()
            key = snippet.lower()
            if key not in seen:
                seen.add(key)
                parts.append(snippet)
    return " · ".join(parts) if parts else None


def _extract_dob(text: str) -> str | None:
    match = DOB_PATTERN.search(text)
    return match.group(0) if match else None


def _extract_bank(text: str) -> str | None:
    for line in _non_empty_lines(text):
        if re.fullmatch(r"(?i)bk", line.strip()):
            return "Bk"
    match = BANK_NAME.search(text)
    if not match:
        return None
    bank = match.group(1)
    if bank.lower().startswith("barclay"):
        return "Barclaycard" if "card" in bank.lower() else "Barclays"
    return bank.title()


def _extract_online_status(text: str) -> str | None:
    for pattern, label in ONLINE_STATUS_PATTERNS:
        if pattern.search(text):
            return label
    return None


def extract_notes_pass_summary(text: str | None) -> NotesPassSummary:
    if not text:
        return NotesPassSummary()
    cleaned = text.strip()
    return NotesPassSummary(
        balance=_extract_balance_summary(cleaned),
        dob=_extract_dob(cleaned),
        bank=_extract_bank(cleaned),
        online=_extract_online_status(cleaned),
    )


def format_notes_summary_html(text: str | None) -> str:
    summary = extract_notes_pass_summary(text)
    lines: list[str] = []
    if summary.balance:
        lines.append(f"<b>Balance:</b> {html.escape(summary.balance)}")
    if summary.dob:
        lines.append(f"<b>DOB:</b> {html.escape(summary.dob)}")
    if summary.bank:
        lines.append(f"<b>Bank:</b> {html.escape(summary.bank)}")
    if summary.online:
        lines.append(f"<b>Online:</b> {html.escape(summary.online)}")
    return "\n".join(lines)
