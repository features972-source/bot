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
    dest_dir.mkdir(parents=True, exist_ok=True)
    base_cmd = ["ffmpeg", "-y", "-i", str(src), "-ar", "8000", "-ac", "1"]
    formats = {
        "wav": ["-acodec", "pcm_s16le"],
        "alaw": ["-acodec", "pcm_alaw", "-f", "alaw"],
        "ulaw": ["-acodec", "pcm_mulaw", "-f", "mulaw"],
    }
    outputs: dict[str, Path] = {}
    for ext, codec_args in formats.items():
        dest = dest_dir / f"{stem}.{ext}"
        proc = subprocess.run(
            base_cmd + codec_args + [str(dest)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            if ext == "wav":
                raise RuntimeError(proc.stderr.strip() or "ffmpeg failed")
            continue
        outputs[ext] = dest
    if "wav" not in outputs:
        raise RuntimeError("WAV conversion failed")
    return outputs
