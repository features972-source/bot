"""Shared helpers to start/stop bot processes reliably on Windows."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
BOT_SCRIPT = ROOT / "bot.py"
LOG_DIR = ROOT / "logs"
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class BotInstance:
    key: str
    label: str
    env_file: str | None
    log_stem: str
    lock_name: str

    @property
    def lock_path(self) -> Path:
        return ROOT / self.lock_name


INSTANCES: dict[str, BotInstance] = {
    "q1": BotInstance(
        key="q1",
        label="Q1 Call Manager",
        env_file=None,
        log_stem="bot",
        lock_name="links.db.bot.lock",
    ),
    "q2": BotInstance(
        key="q2",
        label="Q2 Call Manager",
        env_file=".env.bot2",
        log_stem="bot2",
        lock_name="links-bot2.db.bot.lock",
    ),
    "q1australia": BotInstance(
        key="q1australia",
        label="Q1 Australia",
        env_file=".env.q1australia",
        log_stem="q1australia",
        lock_name="links-q1australia.db.bot.lock",
    ),
}


def _instance_from_cmd(cmd: str) -> str | None:
    if "--env-file" not in cmd:
        return "q1"
    for key, inst in INSTANCES.items():
        if inst.env_file and inst.env_file in cmd:
            return key
    return None


def _run_powershell(command: str) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        cwd=ROOT,
        creationflags=CREATE_NO_WINDOW,
    )
    return (result.stdout or "").strip()


def _all_bot_pid_rows() -> list[tuple[int, int, str]]:
    root_escaped = str(ROOT).replace("'", "''")
    out = _run_powershell(
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{root_escaped}*bot.py*' }} | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)|$($_.CommandLine)\" }"
    )
    rows: list[tuple[int, int, str]] = []
    for line in out.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) < 3:
            continue
        pid_s, ppid_s, cmd = parts
        if pid_s.isdigit() and ppid_s.isdigit():
            rows.append((int(pid_s), int(ppid_s), cmd))
    return rows


def instance_running(key: str) -> bool:
    return bool(bot_pids(key))


def any_bot_running(*, q1: bool) -> bool:
    """Legacy helper: True if Q1 (q1=True) or any secondary instance (q1=False)."""
    if q1:
        return instance_running("q1")
    return any(instance_running(k) for k in INSTANCES if k != "q1")


def bot_pids(key: str) -> list[int]:
    rows = _all_bot_pid_rows()
    all_pids = {pid for pid, _, _ in rows}
    root_pids: list[int] = []
    for pid, ppid, cmd in rows:
        if _instance_from_cmd(cmd) != key:
            continue
        if ppid in all_pids:
            continue
        root_pids.append(pid)
    return root_pids


def stop_instance(key: str) -> None:
    for pid in bot_pids(key):
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    INSTANCES[key].lock_path.unlink(missing_ok=True)


def stop_bot(*, q1: bool) -> None:
    if q1:
        stop_instance("q1")
    else:
        for key in INSTANCES:
            if key != "q1":
                stop_instance(key)


def ensure_stopped(key: str, timeout_s: float = 8.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        stop_instance(key)
        if not bot_pids(key):
            return
        time.sleep(1)
    remaining = bot_pids(key)
    if remaining:
        raise RuntimeError(
            f"Could not stop {INSTANCES[key].label} (pids: {remaining}). "
            "Try Stop All, then start again."
        )


def start_instance(key: str) -> None:
    from local_run import LOCAL_RUN_MESSAGE, local_run_blocked

    if local_run_blocked():
        raise RuntimeError(LOCAL_RUN_MESSAGE.replace("\n\n", " "))
    if key not in INSTANCES:
        raise ValueError(f"Unknown bot instance: {key}")
    inst = INSTANCES[key]
    if not PYTHON.is_file():
        raise FileNotFoundError(f"Python venv not found: {PYTHON}")
    if inst.env_file and not (ROOT / inst.env_file).is_file():
        raise FileNotFoundError(
            f"Missing {ROOT / inst.env_file} — copy the matching .example file and edit it."
        )
    ensure_stopped(key)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_out = LOG_DIR / f"{inst.log_stem}.log"
    log_err = LOG_DIR / f"{inst.log_stem}-error.log"
    args = [str(PYTHON), str(BOT_SCRIPT)]
    if inst.env_file:
        args.extend(["--env-file", inst.env_file])

    with open(log_out, "a", encoding="utf-8") as out, open(
        log_err, "a", encoding="utf-8"
    ) as err:
        subprocess.Popen(
            args,
            cwd=ROOT,
            stdout=out,
            stderr=err,
            creationflags=CREATE_NO_WINDOW,
        )

    time.sleep(5)
    if not instance_running(key):
        raise RuntimeError(
            f"{inst.label} failed to start. Check logs\\{inst.log_stem}-error.log"
        )


def start_bot(*, q1: bool) -> None:
    if q1:
        start_instance("q1")
    else:
        start_instance("q2")


def stop_all_bots() -> None:
    for key in INSTANCES:
        stop_instance(key)
