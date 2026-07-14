#!/usr/bin/env python3
"""Restore Jul 12 (6a0cced) working IVR + DTMF stack to local files and dial server."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "p1-telegram-bot"


def git_show(path: str, commit: str = "6a0cced") -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "show", f"{commit}:{path}"],
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def replace_fn(src: str, name: str, new_fn: str) -> str:
    i = src.find(f"def {name}")
    if i < 0:
        raise SystemExit(f"missing {name}")
    j = src.find("\ndef ", i + 1)
    if j < 0:
        raise SystemExit(f"no end for {name}")
    return src[:i] + new_fn + src[j:]


def extract_fn(src: str, name: str) -> str:
    i = src.find(f"def {name}")
    j = src.find("\ndef ", i + 1)
    if i < 0 or j < 0:
        raise SystemExit(f"cannot extract {name}")
    return src[i:j]


def main() -> int:
    old = git_show("p1-telegram-bot/vicidial_client.py")
    ivr = extract_fn(old, "_press1_ivr_dialplan")
    ami = git_show("p1-telegram-bot/AST_press1_dtmf.pl")

    (BOT / "AST_press1_dtmf.pl").write_text(ami, encoding="utf-8", newline="\n")
    cur = (BOT / "vicidial_client.py").read_text(encoding="utf-8")
    cur = replace_fn(cur, "_press1_ivr_dialplan", ivr)
    (BOT / "vicidial_client.py").write_text(cur, encoding="utf-8", newline="\n")

    # Restore audio detector to redirecting mode (as on Jul 12 working days),
    # but only fire during Read/WaitExten (digit-wait), not Playback greeting.
    audio = (BOT / "press1_audio_dtmf.py").read_text(encoding="utf-8")
    audio = audio.replace("ARM_READ_SEC = 1.5", "ARM_READ_SEC = 0.35")
    audio = audio.replace("GOERTZEL_MIN = 6.0e6", "GOERTZEL_MIN = 2.5e6")
    audio = audio.replace("REDIRECT_ON_HIT = False", "REDIRECT_ON_HIT = True")
    audio = audio.replace(
        'if app not in ("Read", "WaitExten", "Playback"):',
        'if app not in ("Read", "WaitExten"):',
    )
    # Keep v17 banner but say restored
    if "v17" in audio:
        audio = audio.replace(
            'log(\n        "audio DTMF poller v17 %s inband backup (max=%ss thr=%.1e arm=%.1fs)"\n'
            '        % (mode, MAX_IVR_SEC, GOERTZEL_MIN, ARM_READ_SEC)\n    )',
            'log(\n        "audio DTMF poller v17-restored %s digit-wait backup (max=%ss thr=%.1e arm=%.1fs)"\n'
            '        % (mode, MAX_IVR_SEC, GOERTZEL_MIN, ARM_READ_SEC)\n    )',
        )
    (BOT / "press1_audio_dtmf.py").write_text(audio, encoding="utf-8", newline="\n")

    print("Local files restored from 6a0cced + digit-wait audio")
    print("  AMI:", "Background" in ami or "xfer_allowed" in ami)
    print("  IVR has Background:", "Background(" in ivr)
    print("  IVR dtmf inband:", "inband" in ivr)

    sys.path.insert(0, str(BOT))
    from repair_server import _load_ssh_key

    _load_ssh_key()
    import vicidial_client as vd

    print("=== deploy dialplan ===")
    print(vd.ensure_press1_dialplan())
    print("=== deploy DTMF listeners ===")
    print(vd.ensure_dtmf_listener())
    print("=== verify ===")
    print(
        vd.run_remote(
            r"""
asterisk -rx 'dialplan show press1-ivr' 2>&1 | grep -E 'Background|Read|Playback|inband|auto' | head -12
systemctl is-active press1-dtmf press1-audio-dtmf
tail -3 /var/log/astguiclient/press1_audio_dtmf.log
grep -E 'xfer_allowed|background' /usr/share/astguiclient/AST_press1_dtmf.pl | head -6
""",
            40,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
