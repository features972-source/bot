"""Integration tests for multi-step flows (expense wizard, table refresh)."""

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
os.environ.setdefault("BOT_TOKEN", "0000000000:TEST_TOKEN_NOT_REAL")
os.environ.setdefault("CLOUD_DEPLOYED", "true")
os.environ.setdefault("BOT_INSTANCE_ID", "q2")
os.environ.setdefault("BOT_DISPLAY_NAME", "Q2 Call Manager")
os.environ.setdefault("ADMIN_CHAT_ID", "8780653370")

_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB.close()
os.environ["DATABASE_PATH"] = _DB.name

from config import load_settings  # noqa: E402
from database import (  # noqa: E402
    init_db,
    link_extension,
    set_expense_logging_chat_id,
    set_expense_report_chat_id,
)
from handlers import expense_reports, expenses  # noqa: E402
from money_format import init_currency  # noqa: E402


class ExpenseFlowTests(unittest.IsolatedAsyncioTestCase):
    CHAT_ID = -1003928995399
    USER_ID = 8780653370

    @classmethod
    def setUpClass(cls):
        init_currency("£")
        cls.settings = load_settings()
        init_db(cls.settings.database_path)
        set_expense_logging_chat_id(cls.settings.database_path, cls.CHAT_ID)
        set_expense_report_chat_id(cls.settings.database_path, cls.CHAT_ID)
        link_extension(
            cls.settings.database_path,
            extension="999",
            telegram_user_id=8780653370,
            telegram_username="frankgside",
            display_name="Frank",
        )

    def _context(self):
        bot = AsyncMock()
        bot.username = "Q2CallManagerBot"
        bot.delete_message = AsyncMock()
        bot.send_photo = AsyncMock(
            return_value=SimpleNamespace(message_id=5001)
        )
        bot.send_message = AsyncMock(
            return_value=SimpleNamespace(message_id=5002)
        )
        ctx = MagicMock()
        ctx.bot = bot
        ctx.bot_data = {
            "settings": self.settings,
            "instance_id": "q2",
            "pending_expenses": {},
        }
        ctx.user_data = {}
        ctx.application = MagicMock()
        ctx.application.bot_data = ctx.bot_data
        return ctx, bot

    def _update(self, text: str, message_id: int):
        user = SimpleNamespace(
            id=self.USER_ID,
            username="frankgside",
            first_name="Frank",
            last_name="",
            is_bot=False,
        )
        chat = SimpleNamespace(id=self.CHAT_ID, type="supergroup")
        message = MagicMock()
        message.message_id = message_id
        message.text = text
        message.chat = chat
        message.from_user = user
        message.reply_to_message = None
        message.entities = []
        message.reply_text = AsyncMock(
            return_value=SimpleNamespace(message_id=message_id + 1)
        )
        update = MagicMock()
        update.effective_user = user
        update.effective_chat = chat
        update.effective_message = message
        update.message = message
        update.args = []
        return update

    async def test_expense_wizard_posts_table(self):
        ctx, bot = self._context()
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 128

        with patch(
            "handlers.admin_access.require_admin",
            new=AsyncMock(return_value=True),
        ), patch(
            "handlers.expense_reports.asyncio.to_thread",
            new=AsyncMock(return_value=png),
        ):
            await expenses.expense_command(self._update("/expense", 9100), ctx)
            self.assertTrue(
                await expenses.try_complete_pending_expense(
                    self._update("@frankgside", 9102), ctx
                )
            )
            self.assertTrue(
                await expenses.try_complete_pending_expense(
                    self._update("2232", 9104), ctx
                )
            )
            self.assertTrue(
                await expenses.try_complete_pending_expense(
                    self._update("Blast", 9106), ctx
                )
            )

        bot.send_photo.assert_awaited()
        args, kwargs = bot.send_photo.await_args
        self.assertEqual(kwargs.get("chat_id") or args[0], self.CHAT_ID)
        bot.delete_message.assert_awaited()

    async def test_refresh_uses_table_chat_fallback(self):
        ctx, bot = self._context()
        png = b"\x89PNG\r\n\x1a\n" + b"1" * 128
        with patch(
            "handlers.expense_reports.asyncio.to_thread",
            new=AsyncMock(return_value=png),
        ):
            await expense_reports.refresh_expense_report(
                bot, self.settings, chat_id=self.CHAT_ID
            )
        bot.send_photo.assert_awaited()


if __name__ == "__main__":
    unittest.main()
