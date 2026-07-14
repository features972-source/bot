#!/usr/bin/env python3
"""Press-1 audio backup — MixMonitor RX from IVR answer.

RFC2833 + dialplan Read() are primary. This watcher is the inband fallback during
Read/Playback only; ignores silent RX, early arm delay, and legs past MAX_IVR_SEC.
"""
from __future__ import annotations

import math
import struct
import subprocess
import time
from pathlib import Path

MONITOR_DIR = Path("/var/spool/asterisk/monitor")
LOG = Path("/var/log/astguiclient/press1_audio_dtmf.log")
SR = 8000
# Analyse recent buffer for classic DTMF-1 bursts.
TAIL_SEC = 1.5
MIN_FILE_SEC = 0.35  # ~0.35s of RX before we look
ARM_READ_SEC = 0.25
MIN_SIZE = 1200  # ~75ms of sln
MAX_IVR_SEC = 40  # real press-1 is early; long legs = echo false positives
GOERTZEL_MIN = 2.5e6  # higher = fewer false positives from speech/echo
# NEVER auto-redirect from audio Goertzel — it false-fires on speech/echo
# (e.g. Jul 14 11:15 owner test -> straight to agent at dur=7s).
# Real press-1 = RFC2833 AMI + dialplan Read() only.
REDIRECT_ON_HIT = True


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%a %b %d %H:%M:%S %Y')} {msg}\n")
    except Exception:
        pass


def sh(cmd: str) -> str:
    return subprocess.getoutput(cmd)


def goertzel(buf: list[int], freq: float, sr: int = SR) -> float:
    n = len(buf)
    if n < 80:
        return 0.0
    k = int(0.5 + n * freq / sr)
    coeff = 2 * math.cos(2 * math.pi * k / n)
    s0 = s1 = s2 = 0.0
    for x in buf:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    return (s1 * s1 + s2 * s2 - coeff * s1 * s2) / n


def read_sln(path: Path) -> list[int]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    n = len(raw) // 2
    if n < 200:
        return []
    return list(struct.unpack("<" + "h" * n, raw[: n * 2]))


def window_dtmf1(buf: list[int]) -> bool:
    """Classic 697+1209 DTMF-1 only (no degraded mode — that auto-fired)."""
    energy = sum(abs(x) for x in buf) / len(buf)
    if energy < 100:
        return False
    lows = {f: goertzel(buf, f) for f in (697, 770, 852, 941)}
    highs = {f: goertzel(buf, f) for f in (1209, 1336, 1477, 1633)}
    p697, p1209 = lows[697], highs[1209]
    low_sum = sum(lows.values()) + 1.0
    high_sum = sum(highs.values()) + 1.0
    return (
        p697 == max(lows.values())
        and p1209 == max(highs.values())
        and p697 > GOERTZEL_MIN
        and p1209 > GOERTZEL_MIN
        and p697 > lows[770] * 1.5
        and p1209 > highs[1336] * 1.5
        and p697 / low_sum > 0.45
        and p1209 / high_sum > 0.45
    )


def find_press1(samples: list[int]) -> bool:
    """Short classic burst with gaps (not continuous tone)."""
    if len(samples) < int(SR * MIN_FILE_SEC):
        return False
    tail = samples[-int(SR * min(TAIL_SEC, len(samples) / SR)) :]
    win, hop = 400, 80
    if len(tail) < win:
        return window_dtmf1(tail) if len(tail) >= 200 else False
    flags: list[bool] = []
    for i in range(0, len(tail) - win + 1, hop):
        flags.append(window_dtmf1(tail[i : i + win]))
    if not flags:
        return False
    # Continuous tone across most of the buffer = echo/noise, not a keypress
    if sum(flags) >= max(3, int(len(flags) * 0.65)):
        return False
    i = 0
    while i < len(flags):
        if not flags[i]:
            i += 1
            continue
        j = i
        while j < len(flags) and flags[j]:
            j += 1
        burst = j - i
        if 3 <= burst <= 22:
            left = i == 0 or not flags[i - 1]
            right = j >= len(flags) or not flags[j]
            # Prefer a real gap before the burst when we have history
            if i >= 1 and left and right:
                return True
            if i == 0 and right and burst <= 15 and sum(flags) <= burst + 1:
                # Press at very start of digit-wait recording
                return True
        i = j
    return False


def live_read_channels() -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for line in sh("/usr/sbin/asterisk -rx 'core show channels concise' 2>/dev/null").splitlines():
        if not line.lower().startswith("pjsip/bitcall-"):
            continue
        if "press1-ivr" not in line.lower():
            continue
        parts = line.split("!")
        app = parts[5] if len(parts) > 5 else ""
        if app not in ("Read", "WaitExten", "Playback"):
            continue
        chan, uid = parts[0], parts[-1].strip()
        dur = 0
        for tok in parts[-4:]:
            if tok.isdigit():
                dur = int(tok)
        if dur > MAX_IVR_SEC:
            continue
        if app == "Playback" and dur < 1:
            continue
        if chan and uid:
            out.append((chan, uid, dur))
    return out


def paths_for(uid: str) -> list[Path]:
    safe = uid.replace("/", "")
    unders = safe.replace(".", "_")
    return [
        MONITOR_DIR / f"p1audio-{safe}-in.sln",
        MONITOR_DIR / f"p1audio-{unders}-in.sln",
        MONITOR_DIR / f"p1digit-{safe}-in.sln",
        MONITOR_DIR / f"p1digit-{unders}-in.sln",
    ]


def main() -> None:
    MONITOR_DIR.mkdir(parents=True, exist_ok=True)
    fired: set[str] = set()
    armed_at: dict[str, float] = {}
    mode = "redirect" if REDIRECT_ON_HIT else "observe-only"
    log(
        "audio DTMF poller v17-restored %s digit-wait backup (max=%ss thr=%.1e arm=%.1fs)"
        % (mode, MAX_IVR_SEC, GOERTZEL_MIN, ARM_READ_SEC)
    )
    while True:
        try:
            live_now = live_read_channels()
            live_set = {c for c, _, _ in live_now}
            for chan in list(armed_at):
                if chan not in live_set:
                    armed_at.pop(chan, None)
            fired &= live_set
            now = time.monotonic()
            for chan, uid, dur in live_now:
                if chan in fired:
                    continue
                if chan not in armed_at:
                    armed_at[chan] = now
                    continue
                if (now - armed_at[chan]) < ARM_READ_SEC:
                    continue
                hit_path = None
                for p in paths_for(uid):
                    if not p.exists() or p.stat().st_size < MIN_SIZE:
                        continue
                    try:
                        samples = read_sln(p)
                        # Silent RX (RFC2833-only phones) — never invent a press
                        if not samples or sum(abs(x) for x in samples) < 500:
                            continue
                        if find_press1(samples):
                            hit_path = p
                            break
                    except Exception as e:
                        log(f"analyze fail {p}: {e}")
                if not hit_path:
                    continue
                fired.add(chan)
                if not REDIRECT_ON_HIT:
                    log(
                        f"AUDIO DTMF1/observe {chan} dur={dur} file={hit_path} "
                        "(no redirect — AMI/Read are authoritative)"
                    )
                    continue
                safe = chan.replace("'", "")
                redirect = sh(
                    f"/usr/sbin/asterisk -rx 'channel redirect {safe} press1-ivr,1,1' 2>&1"
                )
                log(f"AUDIO DTMF1/classic {chan} dur={dur} file={hit_path} -> {redirect}")
            time.sleep(0.06)
        except Exception as e:
            log(f"loop error: {e}")
            time.sleep(1)


if __name__ == "__main__":
    main()
