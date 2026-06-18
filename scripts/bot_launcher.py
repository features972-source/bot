"""Desktop launcher for Q1/Q2 Call Manager bots and mailer phone link."""

from __future__ import annotations

import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot_process import (  # noqa: E402
    INSTANCES,
    PYTHON,
    ROOT as BOT_ROOT,
    bot_pids,
    start_bot,
    start_instance,
    stop_all_bots,
)

MAILER_SESSION = ROOT / "mailer-links.session"
TELETHON_LOGIN = ROOT / "scripts" / "telethon_login.py"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010


def _mailer_linked_user() -> str | None:
    if not MAILER_SESSION.is_file():
        return None
    if not PYTHON.is_file():
        return "session on disk"
    root_repr = repr(str(ROOT))
    code = f"""
import asyncio, os, sys
sys.path.insert(0, {root_repr})
os.chdir({root_repr})
from config import load_settings
from telethon import TelegramClient

async def main():
    s = load_settings()
    c = TelegramClient(s.telethon_session_path, s.telethon_api_id, s.telethon_api_hash)
    await c.connect()
    if not await c.is_user_authorized():
        print("NOT_AUTHORIZED")
    else:
        me = await c.get_me()
        print(f"{{me.first_name or ''}}|{{me.username or ''}}|{{me.phone or ''}}")
    await c.disconnect()

asyncio.run(main())
"""
    result = subprocess.run(
        [str(PYTHON), "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        creationflags=CREATE_NO_WINDOW,
        timeout=30,
    )
    line = (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
    if line == "NOT_AUTHORIZED":
        return None
    if "|" in line:
        name, user, phone = line.split("|", 2)
        label = name.strip()
        if user.strip():
            label += f" (@{user.strip()})"
        if phone.strip() and phone.strip() != "unknown":
            label += f" · {phone.strip()}"
        return label
    return "session on disk"


def _link_mailer_phone() -> None:
    if not TELETHON_LOGIN.is_file():
        raise FileNotFoundError(f"Missing {TELETHON_LOGIN}")
    if not PYTHON.is_file():
        raise FileNotFoundError(f"Python venv not found: {PYTHON}")
    subprocess.Popen(
        [str(PYTHON), str(TELETHON_LOGIN)],
        cwd=ROOT,
        creationflags=CREATE_NEW_CONSOLE,
    )


class LauncherApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Q1 Bot Launcher")
        self.root.geometry("420x420")
        self.root.resizable(False, False)

        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="Call Manager Control",
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor=tk.W)

        ttk.Label(
            frame,
            text=str(BOT_ROOT),
            font=("Segoe UI", 8),
            wraplength=380,
        ).pack(anchor=tk.W, pady=(0, 12))

        self.q1_status = ttk.Label(frame, text="Q1: checking…")
        self.q1_status.pack(anchor=tk.W, pady=2)
        self.q2_status = ttk.Label(frame, text="Q2: checking…")
        self.q2_status.pack(anchor=tk.W, pady=2)
        self.q1au_status = ttk.Label(frame, text="Q1 Australia: checking…")
        self.q1au_status.pack(anchor=tk.W, pady=2)
        self.mailer_status = ttk.Label(frame, text="Mailer: checking…", wraplength=380)
        self.mailer_status.pack(anchor=tk.W, pady=(2, 12))

        btn_row1 = ttk.Frame(frame)
        btn_row1.pack(fill=tk.X, pady=4)
        ttk.Button(btn_row1, text="Start Q1", command=self.start_q1).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4)
        )
        ttk.Button(btn_row1, text="Start Q2", command=self.start_q2).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=(4, 0)
        )

        btn_row2 = ttk.Frame(frame)
        btn_row2.pack(fill=tk.X, pady=4)
        ttk.Button(btn_row2, text="Start Q1 AU", command=self.start_q1australia).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4)
        )
        ttk.Button(btn_row2, text="Start Both", command=self.start_both).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=(4, 0)
        )

        btn_row3 = ttk.Frame(frame)
        btn_row3.pack(fill=tk.X, pady=4)
        ttk.Button(
            btn_row3,
            text="Link Mailer Phone",
            command=self.link_mailer,
        ).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))
        ttk.Button(btn_row3, text="Stop All", command=self.stop_all).pack(
            side=tk.LEFT, expand=True, fill=tk.X, padx=(4, 0)
        )

        ttk.Label(
            frame,
            text="Start Q1 = UK Call Manager + mailer only.\n"
            "Start Q1 AU = Australia instance (separate DB and group).\n"
            "Start Both = Q1 UK and Q2 together.\n"
            "If any bot shows multiple processes, use Stop All then start again.",
            font=("Segoe UI", 9),
            wraplength=380,
        ).pack(anchor=tk.W, pady=(12, 0))

        self.refresh_status()
        self.root.after(4000, self._schedule_refresh)

    def _schedule_refresh(self) -> None:
        self.refresh_status()
        self.root.after(4000, self._schedule_refresh)

    def refresh_status(self) -> None:
        for key, label_attr in (
            ("q1", "q1_status"),
            ("q2", "q2_status"),
            ("q1australia", "q1au_status"),
        ):
            inst = INSTANCES[key]
            count = len(bot_pids(key))
            text = f"{inst.label}: {'running' if count else 'stopped'}"
            if count > 1:
                text += f" — WARNING: {count} processes (use Stop All)"
            getattr(self, label_attr).config(text=text)
        try:
            linked = _mailer_linked_user()
        except Exception:
            linked = None
        if linked:
            self.mailer_status.config(text=f"Mailer access: linked · {linked}")
        elif MAILER_SESSION.is_file():
            self.mailer_status.config(
                text="Mailer access: session file present (re-link if needed)"
            )
        else:
            self.mailer_status.config(
                text="Mailer access: not linked — use Link Mailer Phone"
            )

    def _prompt_mailer_link(self) -> None:
        if not MAILER_SESSION.is_file() and messagebox.askyesno(
            "Mailer not linked",
            "Mailer phone is not linked yet.\n"
            "Open the phone login window now?",
        ):
            _link_mailer_phone()

    def start_q1(self) -> None:
        try:
            self._prompt_mailer_link()
            start_bot(q1=True)
            messagebox.showinfo("Q1 Bot", "Q1 Call Manager started (Q2 not started).")
        except Exception as exc:
            messagebox.showerror("Q1 Bot", str(exc))
        self.refresh_status()

    def start_q2(self) -> None:
        try:
            start_bot(q1=False)
            messagebox.showinfo("Q2 Bot", "Q2 Call Manager started (Q1 unchanged).")
        except Exception as exc:
            messagebox.showerror("Q2 Bot", str(exc))
        self.refresh_status()

    def start_q1australia(self) -> None:
        try:
            self._prompt_mailer_link()
            start_instance("q1australia")
            messagebox.showinfo(
                "Q1 Australia",
                "Q1 Australia started (separate database and group).",
            )
        except Exception as exc:
            messagebox.showerror("Q1 Australia", str(exc))
        self.refresh_status()

    def start_both(self) -> None:
        try:
            self._prompt_mailer_link()
            start_bot(q1=True)
            start_bot(q1=False)
            messagebox.showinfo("Both Bots", "Q1 and Q2 Call Manager started.")
        except Exception as exc:
            messagebox.showerror("Start Both", str(exc))
        self.refresh_status()

    def link_mailer(self) -> None:
        try:
            _link_mailer_phone()
            messagebox.showinfo(
                "Mailer Phone",
                "A console window opened.\n\n"
                "Enter your phone number (+country code) and the Telegram code.\n"
                "Use the same number that can access the mailer bot.",
            )
        except Exception as exc:
            messagebox.showerror("Mailer Phone", str(exc))

    def stop_all(self) -> None:
        stop_all_bots()
        messagebox.showinfo("Stop All", "All Call Manager bots stopped.")
        self.refresh_status()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if not ROOT.is_dir():
        messagebox.showerror("Launcher", f"Bot folder not found: {ROOT}")
        sys.exit(1)
    LauncherApp().run()


if __name__ == "__main__":
    main()
