#!/usr/bin/env python3
"""Restore the Jul 13 working press-1 dialplan (auto DTMF + Read with sound)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "p1-telegram-bot"
COMMIT = "5ed8520"  # last known working: auto + Read(sound&beep)


def git_show(path: str, commit: str = COMMIT) -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "show", f"{commit}:{path}"],
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def extract_fn(src: str, name: str) -> str:
    i = src.find(f"def {name}")
    j = src.find("\ndef ", i + 1)
    if i < 0 or j < 0:
        raise SystemExit(f"cannot extract {name}")
    return src[i:j]


def replace_fn(src: str, name: str, new_fn: str) -> str:
    i = src.find(f"def {name}")
    j = src.find("\ndef ", i + 1)
    if i < 0 or j < 0:
        raise SystemExit(f"missing {name}")
    return src[:i] + new_fn + src[j:]


def main() -> int:
    old = git_show("p1-telegram-bot/vicidial_client.py")
    ivr = extract_fn(old, "_press1_ivr_dialplan")
    if "PJSIP_DTMF_MODE()=auto" not in ivr or "P1SOUND}}&beep" not in ivr:
        raise SystemExit("unexpected IVR from commit — abort")

    # AMI from 6a0cced/5ed8520 era (allows Background+Read, DTMFBegin redirect)
    ami = git_show("p1-telegram-bot/AST_press1_dtmf.pl", "6a0cced")
    (BOT / "AST_press1_dtmf.pl").write_text(ami, encoding="utf-8", newline="\n")

    cur = (BOT / "vicidial_client.py").read_text(encoding="utf-8")
    cur = replace_fn(cur, "_press1_ivr_dialplan", ivr)
    (BOT / "vicidial_client.py").write_text(cur, encoding="utf-8", newline="\n")

    # Audio: redirect during Read (matches working days), include Playback ok
    audio = (BOT / "press1_audio_dtmf.py").read_text(encoding="utf-8")
    audio = audio.replace("REDIRECT_ON_HIT = False", "REDIRECT_ON_HIT = True")
    if 'if app not in ("Read", "WaitExten"):' in audio:
        audio = audio.replace(
            'if app not in ("Read", "WaitExten"):',
            'if app not in ("Read", "WaitExten", "Playback"):',
        )
    audio = audio.replace("ARM_READ_SEC = 1.5", "ARM_READ_SEC = 0.25")
    audio = audio.replace("GOERTZEL_MIN = 6.0e6", "GOERTZEL_MIN = 2.5e6")
    (BOT / "press1_audio_dtmf.py").write_text(audio, encoding="utf-8", newline="\n")

    print("Restored IVR from", COMMIT, "(auto DTMF + Read sound&beep)")
    print("modes:", [ln.strip() for ln in ivr.splitlines() if "PJSIP_DTMF" in ln or "Read(P1DIGIT" in ln])

    sys.path.insert(0, str(BOT))
    from repair_server import _load_ssh_key

    _load_ssh_key()
    import vicidial_client as vd

    print("=== dialplan ===")
    print(vd.ensure_press1_dialplan())
    print("=== dtmf ===")
    print(vd.ensure_dtmf_listener())
    # Ensure beep exists so Read(sound&beep) doesn't glitch
    print(
        vd.run_remote(
            r"""
# ensure stock beep exists for Read(...&beep)
for d in /var/lib/asterisk/sounds/en /usr/share/asterisk/sounds/en /var/lib/asterisk/sounds; do
  [ -f "$d/beep.ulaw" ] || [ -f "$d/beep.gsm" ] || [ -f "$d/beep.wav" ] && echo HAVE_BEEP_IN_$d && break
done
# soft link if missing in language path
if [ ! -f /var/lib/asterisk/sounds/en/beep.ulaw ] && [ -f /var/lib/asterisk/sounds/beep.ulaw ]; then
  ln -sf /var/lib/asterisk/sounds/beep.ulaw /var/lib/asterisk/sounds/en/beep.ulaw
  echo LINKED_BEEP
fi
asterisk -rx 'dialplan show press1-ivr' 2>&1 | grep -E 'DTMF|Read|Background|auto|inband' | head -10
asterisk -rx 'pjsip show endpoint bitcall' 2>/dev/null | grep -i dtmf_mode
systemctl is-active press1-dtmf press1-audio-dtmf
tail -2 /var/log/astguiclient/press1_audio_dtmf.log
""",
            40,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
