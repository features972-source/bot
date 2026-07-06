#!/usr/bin/env python3
"""One-shot repair: deploy DTMF listener, dialplan, and 3CX endpoints on dial server."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Repo ships a one-line OpenSSH key for emergency server repair.
_KEY_FILE = Path(__file__).with_name("RENDER_SSH_KEY_ONE_LINE.txt")


def _load_ssh_key() -> None:
    if os.getenv("VICIDIAL_SSH_KEY"):
        return
    if not _KEY_FILE.is_file():
        raise SystemExit("Set VICIDIAL_SSH_KEY or place RENDER_SSH_KEY_ONE_LINE.txt")
    raw = _KEY_FILE.read_text(encoding="utf-8").strip()
    raw = raw.replace("\\n", "\n")
    fd, path = tempfile.mkstemp(prefix="p1_ssh_", suffix=".key")
    os.close(fd)
    Path(path).write_text(raw, encoding="utf-8")
    os.environ["VICIDIAL_SSH_KEY"] = path


def main() -> None:
    _load_ssh_key()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import vicidial_client as vd

    print("Repairing press-1 dial server…")
    result = vd.repair_press1_server()
    for key, val in result.items():
        print(f"\n=== {key} ===\n{val}")


if __name__ == "__main__":
    main()
