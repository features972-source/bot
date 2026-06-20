"""Integration test for /addcredo multi-step DM flow."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ALLOW_LOCAL_RUN", "true")
os.environ.setdefault("BOT_TOKEN", "0000000000:TEST")
os.environ.setdefault("CLOUD_DEPLOYED", "true")

from config import load_settings  # noqa: E402
from database import init_db  # noqa: E402
from handlers.credo import (  # noqa: E402
    ADD_CARD_STEP_CAPACITY,
    ADD_CARD_STEP_KEY,
    addcredocard_route_text,
    addcredocard_start,
)
from telegram.ext import ApplicationHandlerStop  # noqa: E402


def _private_update(text: str, user_id: int = 8780653370):
    user = SimpleNamespace(
        id=user_id,
        username="admin",
        first_name="Admin",
        last_name="",
        is_bot=False,
    )
    chat = SimpleNamespace(id=user_id, type="private")
    message = MagicMock()
    message.message_id = 1
    message.text = text
    message.chat = chat
    message.from_user = user
    message.reply_to_message = None
    message.reply_text = AsyncMock()
    message.reply_photo = AsyncMock()

    update = MagicMock()
    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = message
    return update


class AddCredoFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_path = os.path.join(
            tempfile.gettempdir(), f"addcredo_{uuid.uuid4().hex}.db"
        )
        os.environ["DATABASE_PATH"] = self.db_path
        init_db(self.db_path)
        self.settings = load_settings()
        self.context = MagicMock()
        self.context.bot_data = {"settings": self.settings}
        self.context.user_data = {}
        self.context.application = MagicMock()
        self.context.application.handlers = {0: [], 1: []}
        self.context.application.bot_data = self.context.bot_data

    async def test_name_step_advances(self):
        start_update = _private_update("/addcredo")
        with patch(
            "handlers.credo.require_admin",
            new=AsyncMock(return_value=True),
        ):
            await addcredocard_start(start_update, self.context)

        session = self.context.application.bot_data["add_card_sessions"][8780653370]
        self.assertEqual(session["step"], "name")

        name_update = _private_update("Lloyds")
        with self.assertRaises(ApplicationHandlerStop):
            await addcredocard_route_text(name_update, self.context)

        self.assertEqual(session["step"], ADD_CARD_STEP_CAPACITY)
        name_update.effective_message.reply_text.assert_awaited()
        body = name_update.effective_message.reply_text.await_args.args[0]
        self.assertIn("Step 2", body)


if __name__ == "__main__":
    unittest.main()
