"""Smoke-test every bot command handler with mocked Telegram updates."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ALLOW_LOCAL_RUN", "true")
os.environ.setdefault("BOT_TOKEN", "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
os.environ.setdefault("CLOUD_DEPLOYED", "true")
os.environ.setdefault("BOT_INSTANCE_ID", "q2")
os.environ.setdefault("BOT_DISPLAY_NAME", "Q2 Call Manager")
os.environ.setdefault("ADMIN_CHAT_ID", "8780653370")

_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB.close()
os.environ["DATABASE_PATH"] = _DB.name

from config import load_settings  # noqa: E402
from database import init_db, set_notify_chat_id  # noqa: E402
from handlers.bot_commands import build_bot_handlers  # noqa: E402
from handlers.credo import build_add_card_handlers, build_credo_handlers  # noqa: E402
from handlers.mailer import build_mailer_handlers  # noqa: E402
from money_format import init_currency  # noqa: E402


def _make_update(
    text: str,
    *,
    user_id: int = 8780653370,
    chat_id: int = -1003928995399,
    chat_type: str = "supergroup",
    args: list[str] | None = None,
):
    user = SimpleNamespace(
        id=user_id,
        username="testadmin",
        first_name="Test",
        last_name="Admin",
        is_bot=False,
    )
    chat = SimpleNamespace(id=chat_id, type=chat_type)
    message = MagicMock()
    message.message_id = 9001
    message.text = text
    message.chat = chat
    message.from_user = user
    message.reply_to_message = None
    message.entities = []
    message.reply_text = AsyncMock()
    message.reply_photo = AsyncMock()
    message.edit_message_text = AsyncMock()

    update = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = message
    update.message = message
    update.callback_query = None
    update.args = args or []

    return update


def _make_context(settings):
    bot = AsyncMock()
    bot.username = "Q2CallManagerBot"
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.delete_message = AsyncMock()

    context = MagicMock()
    context.bot = bot
    context.bot_data = {
        "settings": settings,
        "instance_id": "q2",
        "notify_chat_id": -1003928995399,
    }
    context.user_data = {}
    context.args = []
    context.application = MagicMock()
    context.application.bot_data = context.bot_data
    return context


def _collect_command_handlers():
    handlers = []
    for builder in (
        build_bot_handlers,
        build_credo_handlers,
        build_add_card_handlers,
        build_mailer_handlers,
    ):
        for handler in builder():
            if handler.__class__.__name__ == "CommandHandler":
                handlers.append((handler.commands, handler.callback))
            elif handler.__class__.__name__ == "ConversationHandler":
                for ep in handler.entry_points:
                    if ep.__class__.__name__ == "CommandHandler":
                        handlers.append((ep.commands, ep.callback))
    # dedupe by command name
    seen: set[str] = set()
    unique = []
    for commands, callback in handlers:
        for cmd in commands:
            if cmd in seen:
                continue
            seen.add(cmd)
            unique.append((cmd, callback))
    return sorted(unique, key=lambda row: row[0])


COMMAND_ARGS: dict[str, list[str]] = {
    "link": ["101"],
    "unlink": ["101"],
    "setpayment": ["1", "100"],
    "removepayment": ["1"],
    "blacklist": ["@someone", "test"],
    "unblacklist": ["@someone"],
    "addadmin": [],
    "removeadmin": [],
    "nemesis": ["@someone"],
    "clearalldata": ["2026-06-17"],
    "removeexpense": ["1"],
}


class CommandSmokeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        init_currency("£")
        cls.settings = load_settings()
        init_db(cls.settings.database_path)
        set_notify_chat_id(cls.settings.database_path, -1003928995399)

    async def _run_command(self, command: str, callback, *, args: list[str] | None = None):
        update = _make_update(f"/{command}", args=args)
        context = _make_context(self.settings)
        context.args = args or []
        with patch(
            "handlers.admin_access.is_bot_admin",
            return_value=True,
        ), patch(
            "handlers.admin_access.require_admin",
            new=AsyncMock(return_value=True),
        ), patch(
            "handlers.credo.is_credo_allowed",
            return_value=True,
        ), patch(
            "handlers.expense_reports.refresh_expense_report",
            new=AsyncMock(),
        ), patch(
            "handlers.expense_reports.schedule_expense_report_refresh",
        ), patch(
            "handlers.payment_reports.schedule_payment_report_refresh",
        ), patch(
            "handlers.profit_export_image.render_profit_export_png",
            return_value=b"\x89PNG\r\n\x1a\n" + b"0" * 64,
        ), patch(
            "handlers.admin_access.sync_bot_command_menu",
            new=AsyncMock(),
        ):
            try:
                result = callback(update, context)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                raise AssertionError(f"/{command} raised {type(exc).__name__}: {exc}") from exc

    async def test_all_commands_smoke(self):
        failures: list[str] = []
        commands = _collect_command_handlers()
        self.assertGreater(len(commands), 30, "expected many commands")
        for command, callback in commands:
            args = COMMAND_ARGS.get(command, [])
            try:
                await self._run_command(command, callback, args=args)
            except AssertionError as exc:
                failures.append(str(exc))
        if failures:
            self.fail("Command failures:\n" + "\n".join(failures))


class UtilitySmokeTests(unittest.TestCase):
    def test_money_parsers(self):
        from money_format import parse_expense_amount, parse_expense_line

        self.assertEqual(parse_expense_amount("132")[0], 132.0)
        self.assertEqual(parse_expense_line("£132 blast")[1], "blast")

    def test_expense_table_image(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed locally")

        from datetime import datetime, timezone

        from database import record_expense
        from handlers.expense_reports import build_expense_report_image

        init_db(os.environ["DATABASE_PATH"])
        record_expense(
            os.environ["DATABASE_PATH"],
            telegram_user_id=1,
            telegram_username="tester",
            display_name="Tester",
            amount=50.0,
            raw_text="£50 test",
            reason="Test",
            chat_id=-1003928995399,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        img = build_expense_report_image(load_settings())
        self.assertIsNotNone(img)
        self.assertGreater(len(img), 100)


if __name__ == "__main__":
    unittest.main()
