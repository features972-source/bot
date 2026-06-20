"""Tests for notes detection and pass queue DB."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

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
