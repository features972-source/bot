"""Number parsing and Asterisk audio conversion for Press-1 bot."""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
from pathlib import Path

MIN_PHONE_DIGITS = 9
DEFAULT_PHONE_CODE = (os.getenv("PRESS1_DEFAULT_PHONE_CODE", "44").strip() or "44")

# NZ local prefixes (leading 0): mobile 020-029, geographic, toll-free.
_NZ_LOCAL_RE = re.compile(
    r"^0("
    r"2[0-9]\d{7,8}|"  # mobile 021-029
    r"[34679]\d{7,8}|"  # geographic
    r"(800|508)\d{6,7}"  # toll-free
    r")$"
)

# Irish geographic prefixes (local format with leading 0).
_IE_GEO_PREFIXES = (
    "01", "021", "022", "023", "024", "025", "026", "027", "028", "029",
    "0402", "0404", "041", "042", "043", "044", "045", "046", "047", "049",
    "0505", "051", "052", "053", "056", "057", "058", "059", "061", "062",
    "063", "064", "065", "066", "067", "069", "071", "072", "074", "075",
    "076", "077", "090", "091", "093", "094", "095", "096", "097", "098", "099",
)


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def _strip_leading_zero(national: str) -> str:
    return national[1:] if national.startswith("0") else national


def _is_irish_local(digits: str) -> bool:
    if re.fullmatch(r"08\d{8}", digits):
        return True
    if not digits.startswith("0"):
        return False
    for prefix in sorted(_IE_GEO_PREFIXES, key=len, reverse=True):
        if digits.startswith(prefix) and len(digits) >= 9:
            return True
    return False


def _is_nz_local(digits: str) -> bool:
    return bool(_NZ_LOCAL_RE.fullmatch(digits))


def normalize_phone(phone: str) -> tuple[str, str]:
    """Return (phone_code, national_digits) for VICIdial / dialing."""
    digits = _digits(phone)
    if not digits:
        return "", ""

    for code in ("353", "44", "61", "64"):
        if digits.startswith(code):
            national = _strip_leading_zero(digits[len(code) :])
            return code, national

    # North American Numbering Plan: 1 + NXX-NXX-XXXX (area/exchange digit is 2-9).
    # Must be checked before the generic fallback so US/Canada numbers aren't given a UK code.
    if digits.startswith("1") and len(digits) == 11 and digits[1] in "23456789":
        return "1", digits[1:]

    if digits.startswith("0"):
        rest = digits[1:]
        if digits.startswith("04") and len(digits) == 10:
            return "61", rest
        # UK mobile 07xxxxxxxxx before Irish geographic 07x prefixes.
        if re.fullmatch(r"07\d{9}", digits):
            return "44", rest
        if _is_nz_local(digits):
            return "64", rest
        if _is_irish_local(digits):
            return "353", rest
        return "44", rest

    # Bare national numbers (no leading 0).
    if re.fullmatch(r"2[0-9]\d{7,8}", digits) and DEFAULT_PHONE_CODE == "64":
        return "64", digits
    if re.fullmatch(r"8\d{8}", digits):
        return "353", digits
    if re.fullmatch(r"7\d{9}", digits):
        return "44", digits

    return DEFAULT_PHONE_CODE, digits


def to_e164(phone: str) -> str:
    """Full international digits for BitCall originate."""
    code, national = normalize_phone(phone)
    if not code or not national:
        return ""
    return f"{code}{national}"


def normalize_uk(phone: str) -> tuple[str, str]:
    """Backward-compatible alias."""
    return normalize_phone(phone)


def parse_numbers(text: str) -> list[str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    nums: list[str] = []
    for ln in lines:
        phone = ln.split(",")[-1].strip() if "," in ln else ln
        if len(re.sub(r"\D", "", phone)) >= MIN_PHONE_DIGITS:
            nums.append(phone)
    return nums


def parse_csv(content: bytes) -> list[str]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    nums: list[str] = []
    if reader.fieldnames:
        phone_col = None
        for name in reader.fieldnames:
            if name and name.lower().replace(" ", "") in (
                "phonenumber",
                "phone",
                "number",
                "mobile",
                "tel",
            ):
                phone_col = name
                break
        if phone_col:
            for row in reader:
                v = row.get(phone_col, "").strip()
                if len(re.sub(r"\D", "", v)) >= MIN_PHONE_DIGITS:
                    nums.append(v)
            return nums
    return parse_numbers(text)


def convert_audio_for_asterisk(src: Path, dest_dir: Path, stem: str) -> dict[str, Path]:
    """Convert to 8 kHz telephony formats — band-limited, no dynamic processing (avoids crackle)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    wav = dest_dir / f"{stem}.wav"
    # Try soxr first; fall back for slim ffmpeg builds on Render.
    filter_chain = (
        "aresample=8000:resampler=soxr:precision=28,highpass=f=200,lowpass=f=3400",
        "aresample=8000,highpass=f=200,lowpass=f=3400",
        "aresample=8000",
    )
    last_err = ""
    for af in filter_chain:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(src),
                "-af", af,
                "-ar", "8000", "-ac", "1",
                "-sample_fmt", "s16", "-acodec", "pcm_s16le",
                str(wav),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            break
        last_err = (proc.stderr or proc.stdout or "").strip()
    else:
        raise RuntimeError(last_err or "ffmpeg WAV conversion failed")

    outputs: dict[str, Path] = {"wav": wav}
    for ext, codec_args in (
        ("sln", ["-acodec", "pcm_s16le", "-f", "s16le"]),
        ("alaw", ["-acodec", "pcm_alaw", "-f", "alaw"]),
        ("ulaw", ["-acodec", "pcm_mulaw", "-f", "mulaw"]),
    ):
        dest = dest_dir / f"{stem}.{ext}"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav), "-ar", "8000", "-ac", "1"] + codec_args + [str(dest)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            continue
        outputs[ext] = dest
    return outputs
