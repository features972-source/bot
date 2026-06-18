"""Single-instance file lock (one process per database)."""

from __future__ import annotations

import atexit
import os
import sys


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok and code.value == STILL_ACTIVE)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_single_instance_lock(database_path: str):
    """Hold an exclusive lock for the lifetime of this process."""
    lock_path = f"{database_path}.bot.lock"
    if os.path.exists(lock_path):
        try:
            with open(lock_path, encoding="utf-8") as existing:
                old_pid = int(existing.read().strip() or "0")
            if _pid_running(old_pid):
                print(
                    "Another 3cx-telegram-bot instance is already running "
                    f"(pid {old_pid}). Stop it first."
                )
                sys.exit(1)
        except (OSError, ValueError):
            pass
        try:
            os.remove(lock_path)
        except OSError:
            pass

    lock_file = open(lock_path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        print(
            "Another 3cx-telegram-bot instance is already running.\n"
            "Stop the other process first (only one instance should run)."
        )
        sys.exit(1)

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()

    def _release() -> None:
        try:
            lock_file.close()
        except OSError:
            pass
        try:
            os.remove(lock_path)
        except OSError:
            pass

    atexit.register(_release)
    return lock_file
