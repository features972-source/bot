"""Stress-test every bot command — varied args, rapid repeats, concurrent bursts."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ALLOW_LOCAL_RUN", "true")
os.environ.setdefault("BOT_TOKEN", "0000000000:TEST_TOKEN_NOT_REAL")
os.environ.setdefault("CLOUD_DEPLOYED", "true")
os.environ.setdefault("BOT_INSTANCE_ID", "q2")
os.environ.setdefault("BOT_DISPLAY_NAME", "Q2 Call Manager")
os.environ.setdefault("ADMIN_CHAT_ID", "8780653370")

from database import (  # noqa: E402
    create_pass_offer,
    init_db,
    join_pass_queue,
    link_extension,
    record_expense,
    record_payment_out,
    set_notify_chat_id,
    upsert_pending_pass_note,
)
from tests.test_command_smoke import (  # noqa: E402
    _collect_command_handlers,
    _make_context,
    _make_update,
    load_settings,
)
from money_format import init_currency  # noqa: E402

CHAT_GROUP = -1003928995399
CHAT_PRIVATE = 8780653370
ADMIN_ID = 8780653370
FINISHER_BASE = 8800000

# Multiple arg variants per command (empty, typical, edge-case).
COMMAND_SCENARIOS: dict[str, list[list[str]]] = {
    "stats": [[], ["today"], ["7"], ["30"], ["all"], ["bogus"]],
    "missedcalls": [[], ["today"], ["7"], ["all"], ["xyz"]],
    "leaderboard": [[], ["today"], ["7"], ["all"]],
    "outstats": [[], ["today"], ["7"]],
    "outleaderboard": [[], ["all"]],
    "alltimepayments": [[]],
    "alltime": [[]],
    "payments": [[]],
    "sent": [[]],
    "export": [[], ["today"], ["7"], ["all"], ["bad"]],
    "out": [[], ["5182"], ["4.5k"], ["notanumber"]],
    "link": [[], ["101"], ["999"]],
    "unlink": [[], ["101"], ["999"]],
    "setpayment": [[], ["1"], ["1", "100"], ["999", "5000"], ["x", "y"]],
    "removepayment": [[], ["1"], ["99999"]],
    "nemesis": [[], ["@rival"], ["999999999"]],
    "blacklist": [[], ["@baduser"], ["@baduser", "spam reason"]],
    "unblacklist": [[], ["@baduser"]],
    "clearalldata": [[], ["2026-01-01"], ["not-a-date"]],
    "removeexpense": [[], ["1"], ["99999"]],
    "addadmin": [[]],
    "removeadmin": [[]],
    "addcredouser": [[], ["@cuser"]],
    "removecredouser": [[], ["@cuser"]],
    "removecredo": [[], ["1"], ["999"]],
    "setcredolimit": [[], ["Tesco", "5000"], ["Lloyds #1", "5k"]],
    "addpremium": [[], [str(ADMIN_ID)]],
    "removepremium": [[], [str(ADMIN_ID)]],
    "mail": [[], ["test subject"]],
    "panel": [[]],
    "ready": [[]],
    "panic": [[]],
    "clearpayments": [[]],
    "todaypayments": [[]],
    "syncpayments": [[]],
    "paidside": [[]],
    "cleared": [[]],
    "setcleared": [[]],
    "notcleared": [[]],
    "setnotcleared": [[]],
    "setnotify": [[]],
    "setnotifyexpenses": [[]],
    "setnotifypayments": [[]],
    "setexpenses": [[]],
    "expense": [[]],
    "cancel": [[]],
    "finished": [[]],
    "cc": [[]],
    "credos": [[]],
    "credo": [[]],
    "creditcard": [[]],
    "activeccs": [[]],
    "usingcc": [[]],
    "addcredo": [[]],
}

# Commands hammered repeatedly (simulates busy group during pass queue load).
RAPID_REPEAT_COMMANDS = (
    "help",
    "payments",
    "out",
    "start",
    "myid",
    "cancel",
    "finished",
)
RAPID_REPEAT_COUNT = 15


@asynccontextmanager
async def _command_patches(*, admin: bool = True, credo: bool = True):
    with (
        patch("handlers.admin_access.is_bot_admin", return_value=admin),
        patch(
            "handlers.admin_access.require_admin",
            new=AsyncMock(return_value=admin),
        ),
        patch("handlers.credo.is_credo_allowed", return_value=credo),
        patch(
            "handlers.expense_reports.refresh_expense_report",
            new=AsyncMock(),
        ),
        patch("handlers.expense_reports.schedule_expense_report_refresh"),
        patch("handlers.payment_reports.schedule_payment_report_refresh"),
        patch(
            "handlers.profit_export_image.render_profit_export_png",
            return_value=b"\x89PNG\r\n\x1a\n" + b"0" * 64,
        ),
        patch(
            "handlers.admin_access.sync_bot_command_menu",
            new=AsyncMock(),
        ),
        patch(
            "handlers.ready_check.send_ready_check",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "handlers.admin_panel.list_all_active_calls",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "handlers.payments.render_payments_table_png",
            return_value=b"\x89PNG\r\n\x1a\n" + b"0" * 128,
        ),
        patch(
            "payments_excel_export.excel_sync_with_timer",
            new=AsyncMock(return_value=None),
        ),
    ):
        yield


async def _invoke(
    settings,
    command: str,
    callback,
    *,
    args: list[str] | None = None,
    user_id: int = ADMIN_ID,
    chat_id: int = CHAT_GROUP,
    chat_type: str = "supergroup",
    admin: bool = True,
    credo: bool = True,
) -> None:
    update = _make_update(
        f"/{command}",
        args=args,
        user_id=user_id,
        chat_id=chat_id,
        chat_type=chat_type,
    )
    context = _make_context(settings)
    context.args = args or []
    async with _command_patches(admin=admin, credo=credo):
        result = callback(update, context)
        if asyncio.iscoroutine(result):
            await result


def _seed_busy_state(db_path: str) -> None:
    """Seed DB like a busy shift: payments, expenses, pass queue load."""
    now = datetime.now(timezone.utc).isoformat()
    for i in range(8):
        record_payment_out(
            db_path,
            telegram_user_id=ADMIN_ID,
            telegram_username="testadmin",
            display_name="Test Admin",
            amount=1000.0 + i * 100,
            raw_text=f"{5000 + i} out",
            chat_id=CHAT_GROUP,
            telegram_message_id=1000 + i,
            created_at=now,
        )
    for i in range(5):
        record_expense(
            db_path,
            telegram_user_id=ADMIN_ID,
            telegram_username="testadmin",
            display_name="Test Admin",
            amount=50.0 + i,
            raw_text=f"£{50 + i} test",
            reason=f"Expense {i}",
            chat_id=CHAT_GROUP,
            created_at=now,
        )
    for i in range(5):
        join_pass_queue(
            db_path,
            telegram_user_id=FINISHER_BASE + i,
            telegram_username=f"fin{i}",
            display_name=f"Finisher {i}",
        )
    for i in range(3):
        create_pass_offer(
            db_path,
            chat_id=CHAT_GROUP,
            notes_message_id=2000 + i,
            starter_user_id=9000 + i,
            starter_username=f"starter{i}",
            starter_display_name=f"Starter {i}",
            assigned_user_id=FINISHER_BASE + i,
            assigned_username=f"fin{i}",
            assigned_display_name=f"Finisher {i}",
            notes_text=f"Customer {i}\n01/01/1990\nbarclays\ncurrent {5000 + i}",
        )
    for i in range(3, 5):
        upsert_pending_pass_note(
            db_path,
            chat_id=CHAT_GROUP,
            notes_message_id=3000 + i,
            starter_user_id=9000 + i,
            starter_username=f"starter{i}",
            starter_display_name=f"Starter {i}",
            notes_text=f"Waiting {i}\n01/01/1990\nbarclays\ncurrent {6000 + i}",
        )


class CommandStressTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        init_currency("£")
        cls.settings = load_settings()
        init_db(cls.settings.database_path)
        set_notify_chat_id(cls.settings.database_path, CHAT_GROUP)
        link_extension(
            cls.settings.database_path,
            extension="101",
            telegram_user_id=ADMIN_ID,
            telegram_username="testadmin",
            display_name="Test Admin",
        )
        cls.commands = dict(_collect_command_handlers())

    async def test_every_command_all_scenarios(self):
        failures: list[str] = []
        runs = 0
        for command, callback in sorted(self.commands.items()):
            scenarios = COMMAND_SCENARIOS.get(command, [[]])
            for args in scenarios:
                runs += 1
                try:
                    await _invoke(self.settings, command, callback, args=args)
                except Exception as exc:
                    failures.append(
                        f"/{command} args={args!r} -> {type(exc).__name__}: {exc}"
                    )
        self.assertGreater(runs, len(self.commands), "expected multiple scenarios")
        if failures:
            self.fail(
                f"{len(failures)} scenario failure(s):\n" + "\n".join(failures[:40])
            )

    async def test_rapid_repeat_hot_commands(self):
        failures: list[str] = []
        for command in RAPID_REPEAT_COMMANDS:
            callback = self.commands.get(command)
            if callback is None:
                continue
            for n in range(RAPID_REPEAT_COUNT):
                try:
                    await _invoke(self.settings, command, callback)
                except Exception as exc:
                    failures.append(
                        f"/{command} repeat {n + 1} -> {type(exc).__name__}: {exc}"
                    )
        if failures:
            self.fail(
                f"{len(failures)} rapid-repeat failure(s):\n" + "\n".join(failures)
            )

    async def test_full_burst_all_commands_sequential(self):
        """Run every command 3× back-to-back (simulates command spam in group)."""
        failures: list[str] = []
        for _round in range(3):
            for command, callback in sorted(self.commands.items()):
                args = (COMMAND_SCENARIOS.get(command) or [[]])[0]
                try:
                    await _invoke(self.settings, command, callback, args=args)
                except Exception as exc:
                    failures.append(
                        f"round {_round + 1} /{command} -> {type(exc).__name__}: {exc}"
                    )
        if failures:
            self.fail(
                f"{len(failures)} burst failure(s):\n" + "\n".join(failures[:30])
            )

    async def test_concurrent_command_mix(self):
        """Fire 40 commands concurrently (mixed handlers)."""
        tasks = []
        names = sorted(self.commands.keys())
        for i in range(40):
            command = names[i % len(names)]
            callback = self.commands[command]
            scenarios = COMMAND_SCENARIOS.get(command, [[]])
            args = scenarios[i % len(scenarios)] if scenarios else []
            tasks.append(
                _invoke(self.settings, command, callback, args=args)
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failures = [
            f"task {i}: {r}"
            for i, r in enumerate(results)
            if isinstance(r, Exception)
        ]
        if failures:
            self.fail(
                f"{len(failures)} concurrent failure(s):\n" + "\n".join(failures)
            )

    async def test_non_admin_paths_do_not_crash(self):
        """Regular users hitting admin commands should deny gracefully, not explode."""
        admin_only = (
            "stats",
            "missedcalls",
            "link",
            "unlink",
            "links",
            "users",
            "setnotify",
            "setnotifyexpenses",
            "setnotifypayments",
            "setexpenses",
            "clearpayments",
            "clearalldata",
            "panic",
            "syncpayments",
            "paidside",
            "addadmin",
            "removeadmin",
            "addvip",
            "removevip",
            "clearnotes",
            "blacklist",
            "unblacklist",
            "addcredouser",
            "removecredouser",
            "removecredo",
            "addpremium",
            "removepremium",
            "maillogs",
            "panel",
            "export",
        )
        failures: list[str] = []
        user_id = 7777777
        for command in admin_only:
            callback = self.commands.get(command)
            if callback is None:
                continue
            args = (COMMAND_SCENARIOS.get(command) or [[]])[0]
            try:
                await _invoke(
                    self.settings,
                    command,
                    callback,
                    args=args,
                    user_id=user_id,
                    admin=False,
                    credo=False,
                )
            except Exception as exc:
                failures.append(f"/{command} -> {type(exc).__name__}: {exc}")
        if failures:
            self.fail(
                f"{len(failures)} non-admin crash(es):\n" + "\n".join(failures)
            )

    async def test_private_chat_sensitive_commands(self):
        """Commands that behave differently in DM vs group."""
        private_commands = ("mail", "maildone", "ready", "start", "help", "myid")
        failures: list[str] = []
        for command in private_commands:
            callback = self.commands.get(command)
            if callback is None:
                continue
            try:
                await _invoke(
                    self.settings,
                    command,
                    callback,
                    chat_id=CHAT_PRIVATE,
                    chat_type="private",
                )
            except Exception as exc:
                failures.append(f"/{command} private -> {type(exc).__name__}: {exc}")
        if failures:
            self.fail(
                f"{len(failures)} private-chat failure(s):\n" + "\n".join(failures)
            )

    async def test_commands_under_loaded_database(self):
        """All commands while DB has payments, expenses, and pass queue backlog."""
        _seed_busy_state(self.settings.database_path)
        failures: list[str] = []
        for command, callback in sorted(self.commands.items()):
            scenarios = COMMAND_SCENARIOS.get(command, [[]])
            args = scenarios[0] if scenarios else []
            try:
                await _invoke(self.settings, command, callback, args=args)
            except Exception as exc:
                failures.append(f"/{command} loaded -> {type(exc).__name__}: {exc}")
        if failures:
            self.fail(
                f"{len(failures)} loaded-DB failure(s):\n" + "\n".join(failures)
            )

    async def test_pass_queue_commands_while_finishers_busy(self):
        self.skipTest("Pass queue disabled")


if __name__ == "__main__":
    unittest.main()
