"""Number parsing and Asterisk audio conversion for Press-1 bot."""

from __future__ import annotations

import csv
import io
import re
import subprocess
from pathlib import Path

MIN_PHONE_DIGITS = 9


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


def normalize_uk(phone: str) -> tuple[str, str]:
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("44"):
        national = digits[2:]
        if national.startswith("0"):
            national = national[1:]
        return "44", national
    if digits.startswith("0"):
        return "44", digits[1:]
    return "44", digits


def convert_audio_for_asterisk(src: Path, dest_dir: Path, stem: str) -> dict[str, Path]:
    """Convert to 8 kHz telephony formats with clip-safe processing (no loudnorm)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    wav = dest_dir / f"{stem}.wav"
    # Quiet MP3s: fixed +8 dB after phone-band filter; no dynamics/limiter (avoids crackle).
    af = "aresample=8000:resampler=soxr:precision=28,highpass=f=200,lowpass=f=3400,volume=8dB"
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(src),
            "-af", af,
            "-ar", "8000", "-ac", "1",
            "-sample_fmt", "s16", "-acodec", "pcm_s16le",
            "-dither_method", "none",
            str(wav),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffmpeg WAV conversion failed")

    outputs: dict[str, Path] = {"wav": wav}
    for ext, codec_args in (
        ("alaw", ["-acodec", "pcm_alaw", "-f", "alaw"]),
        ("ulaw", ["-acodec", "pcm_mulaw", "-f", "mulaw"]),
        ("sln", ["-f", "s16le"]),
    ):
        dest = dest_dir / f"{stem}.{ext}"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav), "-ar", "8000", "-ac", "1"] + codec_args + [str(dest)],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            outputs[ext] = dest
    return outputs
