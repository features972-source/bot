"""Tests for notes detection and pass queue DB."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
import uuid
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

from database import (  # noqa: E402
    create_pass_offer,
    get_pass_offer,
    get_pass_queue_position,
    init_db,
    join_pass_queue,
    leave_pass_queue,
    list_pass_queue,
    pass_offer_for_notes,
    rotate_pass_queue_user_to_back,
    update_pass_offer,
)
from notes_detect import looks_like_notes  # noqa: E402
from handlers import pass_queue  # noqa: E402


class PassQueueHandlerTests(unittest.IsolatedAsyncioTestCase):
    EXAMPLE = """Frank Williams

23/02/1943

barclays

balance 20,000

savers with 30k

also has hsbc"""

    @classmethod
    def setUpClass(cls):
        cls.db_path = os.path.join(
            tempfile.gettempdir(), f"pass_handler_test_{uuid.uuid4().hex}.db"
        )
        init_db(cls.db_path)
        join_pass_queue(
            cls.db_path,
            telegram_user_id=111,
            telegram_username="finisher",
            display_name="Finisher",
        )

    async def test_notes_handler_offers_pass(self):
        from config import load_settings

        os.environ["DATABASE_PATH"] = self.db_path
        settings = load_settings()

        user = SimpleNamespace(
            id=222,
            username="starter",
            first_name="Starter",
            last_name="",
            is_bot=False,
        )
        chat = SimpleNamespace(id=-100, type="supergroup")
        notes_message = MagicMock()
        notes_message.message_id = 501
        notes_message.text = self.EXAMPLE
        notes_message.caption = None
        notes_message.chat = chat
        notes_message.from_user = user
        notes_message.reply_text = AsyncMock(
            return_value=SimpleNamespace(message_id=502)
        )

        update = MagicMock()
        update.effective_user = user
        update.effective_chat = chat
        update.effective_message = notes_message

        context = MagicMock()
        context.bot_data = {"settings": settings}

        await pass_queue.notes_message_handler(update, context)

        notes_message.reply_text.assert_awaited()
        args, kwargs = notes_message.reply_text.await_args
        self.assertIn("take this pass", kwargs.get("text", args[0] if args else ""))


class NotesDetectTests(unittest.TestCase):
    EXAMPLE_1 = """ian davis

bn1 3wf (work address)

barclaycard
credit

no online banking

has apay

around 4-7k"""

    EXAMPLE_2 = """Jaqueline Rodger's
15/09/1967

apay - yes
Online - yes
Coin - no"""

    EXAMPLE_3 = """Heather brey

22/09/65

Julie

Very stiff no Barclays

BK19CSS"""

    EXAMPLE_4 = """Frank Williams

23/02/1943

barclays

balance 20,000

savers with 30k

also has hsbc"""

    def test_explicit_notes_marker(self):
        text = "NOTES\nName: John Smith\nDOB: 01/01/1980"
        self.assertTrue(looks_like_notes(text))

    def test_label_heuristic(self):
        text = "Name: Jane Doe\nCard: 4532 1234 5678 9012\nSort: 12-34-56"
        self.assertTrue(looks_like_notes(text))

    def test_real_example_1_ian_davis(self):
        self.assertTrue(looks_like_notes(self.EXAMPLE_1))

    def test_real_example_2_jaqueline(self):
        self.assertTrue(looks_like_notes(self.EXAMPLE_2))

    def test_real_example_3_heather(self):
        self.assertTrue(looks_like_notes(self.EXAMPLE_3))

    def test_real_example_4_frank_williams(self):
        self.assertTrue(looks_like_notes(self.EXAMPLE_4))

    def test_rejects_payment_out(self):
        self.assertFalse(looks_like_notes("5182 out"))

    def test_rejects_short_message(self):
        self.assertFalse(looks_like_notes("hello"))

    def test_rejects_casual_chat(self):
        self.assertFalse(
            looks_like_notes("See you tomorrow\nThanks\nOk cool")
        )

    def test_queue_waiting_accepts_frank_notes(self):
        self.assertTrue(
            looks_like_notes(
                self.EXAMPLE_4,
                queue_waiting=True,
            )
        )

    def test_queue_waiting_rejects_casual(self):
        self.assertFalse(
            looks_like_notes(
                "See you tomorrow\nThanks",
                queue_waiting=True,
            )
        )

    def test_short_two_line_with_dob_and_bank(self):
        self.assertTrue(looks_like_notes("John Smith\n23/02/1943\nbarclays"))

    def test_minimal_three_line_name_and_detail(self):
        self.assertTrue(looks_like_notes("Sarah Jones\n15/01/1980\nLloyds"))

    def test_five_line_paste_without_strong_keywords(self):
        self.assertTrue(
            looks_like_notes("Mary Anne\n12/12/1950\nJulie\nNo barclays\nAB12 3CD")
        )


class PassQueueDbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = os.path.join(
            tempfile.gettempdir(), f"pass_queue_test_{uuid.uuid4().hex}.db"
        )
        init_db(cls.db_path)

    def test_queue_order_and_rotate(self):
        join_pass_queue(
            self.db_path,
            telegram_user_id=1,
            telegram_username="first",
            display_name="First",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=2,
            telegram_username="second",
            display_name="Second",
        )
        self.assertEqual(get_pass_queue_position(self.db_path, 1), 1)
        self.assertEqual(get_pass_queue_position(self.db_path, 2), 2)

        rotate_pass_queue_user_to_back(self.db_path, 1)
        queue = list_pass_queue(self.db_path)
        self.assertEqual([entry.user_id for entry in queue], [2, 1])

    def test_offer_lifecycle(self):
        offer_id = create_pass_offer(
            self.db_path,
            chat_id=-100,
            notes_message_id=42,
            starter_user_id=10,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=20,
            assigned_username="finisher",
            assigned_display_name="Finisher",
            notes_text="Name: Test\nCard: 1234",
        )
        self.assertTrue(pass_offer_for_notes(self.db_path, -100, 42))
        offer = get_pass_offer(self.db_path, offer_id)
        self.assertIsNotNone(offer)
        assert offer is not None
        self.assertEqual(offer.status, "pending")
        update_pass_offer(self.db_path, offer_id, status="taken", offer_message_id=99)
        offer = get_pass_offer(self.db_path, offer_id)
        assert offer is not None
        self.assertEqual(offer.status, "taken")
        self.assertEqual(offer.offer_message_id, 99)

    def test_leave_queue(self):
        join_pass_queue(
            self.db_path,
            telegram_user_id=99,
            telegram_username="temp",
            display_name="Temp",
        )
        self.assertTrue(leave_pass_queue(self.db_path, 99))
        self.assertFalse(leave_pass_queue(self.db_path, 99))


if __name__ == "__main__":
    unittest.main()
