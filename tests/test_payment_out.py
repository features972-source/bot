"""Tests for payment out detection and starter resolution."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ALLOW_LOCAL_RUN", "true")
os.environ.setdefault("BOT_TOKEN", "0000000000:TEST")
os.environ.setdefault("CLOUD_DEPLOYED", "true")
os.environ.setdefault("BOT_INSTANCE_ID", "q2")

from database import (  # noqa: E402
    create_pass_offer,
    init_db,
    set_notify_chat_id,
)
from handlers.payments import (  # noqa: E402
    _resolve_starter,
    find_payment_out_in_text,
    payment_out_message,
)
from money_format import init_currency  # noqa: E402


class PaymentOutParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_currency("£")

    def test_standard_out(self):
        self.assertEqual(find_payment_out_in_text("2322 out"), (2322.0, "2322 out"))

    def test_reversed_out(self):
        self.assertEqual(find_payment_out_in_text("out 2322"), (2322.0, "out 2322"))

    def test_comma_amount(self):
        self.assertEqual(find_payment_out_in_text("2,322 out")[0], 2322.0)


class PaymentOutStarterTests(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(
            tempfile.gettempdir(), f"pay_out_test_{uuid.uuid4().hex}.db"
        )
        init_db(self.db_path)
        self.chat_id = -100

    def test_resolve_starter_from_pass_offer_message(self):
        from config import load_settings

        os.environ["DATABASE_PATH"] = self.db_path
        settings = load_settings()

        create_pass_offer(
            self.db_path,
            chat_id=self.chat_id,
            notes_message_id=500,
            starter_user_id=222,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=111,
            assigned_username="fin",
            assigned_display_name="Fin",
            notes_text="notes",
        )
        offer = __import__("database").get_pass_offer(self.db_path, 1)
        assert offer is not None
        __import__("database").update_pass_offer(
            self.db_path, offer.id, offer_message_id=601
        )

        bot_user = SimpleNamespace(id=999, is_bot=True, username="bot")
        offer_message = SimpleNamespace(
            message_id=601,
            chat=SimpleNamespace(id=self.chat_id),
            chat_id=self.chat_id,
            from_user=bot_user,
            reply_to_message=None,
            text="Take this pass",
            caption=None,
        )

        starter = _resolve_starter(
            settings=settings,
            bot_data={},
            reply_to=offer_message,
        )
        self.assertIsNotNone(starter)
        assert starter is not None
        self.assertEqual(starter[0], 222)


class PaymentOutHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_out_message_detects_caption(self):
        from config import load_settings

        db_path = os.path.join(
            tempfile.gettempdir(), f"pay_out_hdl_{uuid.uuid4().hex}.db"
        )
        init_db(db_path)
        set_notify_chat_id(db_path, -100)
        os.environ["DATABASE_PATH"] = db_path
        init_currency("£")
        settings = load_settings()

        starter = SimpleNamespace(
            id=222,
            username="starter",
            first_name="Starter",
            last_name="",
            is_bot=False,
        )
        notes = SimpleNamespace(
            message_id=500,
            chat=SimpleNamespace(id=-100),
            chat_id=-100,
            from_user=starter,
            reply_to_message=None,
            text="Customer notes",
            caption=None,
        )
        finisher = SimpleNamespace(
            id=111,
            username="fin",
            first_name="Fin",
            last_name="",
            is_bot=False,
        )
        message = MagicMock()
        message.message_id = 502
        message.text = None
        message.caption = "2322 out"
        message.chat = SimpleNamespace(id=-100, type="supergroup")
        message.from_user = finisher
        message.reply_to_message = notes
        message.reply_text = AsyncMock()

        update = MagicMock()
        update.effective_user = finisher
        update.effective_chat = message.chat
        update.effective_message = message

        context = MagicMock()
        context.bot_data = {"settings": settings, "notify_chat_id": -100}
        context.bot = AsyncMock()
        context.bot.username = "TestBot"

        await payment_out_message(update, context)

        message.reply_text.assert_awaited()
        args, kwargs = message.reply_text.await_args
        text = kwargs.get("text", args[0] if args else "")
        self.assertIn("OUT", text)


if __name__ == "__main__":
    unittest.main()
