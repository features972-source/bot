"""Tests for notes detection and pass queue DB."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
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
    PassOffer,
    add_pass_queue_vip,
    create_pass_offer,
    get_active_pass_offer,
    get_pass_offer,
    get_pass_queue_position,
    init_db,
    is_pass_queue_vip,
    join_pass_queue,
    leave_pass_queue,
    list_pass_queue,
    pass_offer_for_notes,
    pending_pass_assignee_user_ids,
    remove_pass_queue_vip,
    rotate_pass_queue_user_to_back,
    update_pass_offer,
)
from handlers import pass_queue  # noqa: E402
from handlers.pass_queue import _next_queue_assignee, pass_reminder_due  # noqa: E402
from notes_detect import looks_like_notes, notes_has_balance  # noqa: E402


class PassReminderTests(unittest.TestCase):
    def test_reminder_not_due_immediately(self):
        offer = PassOffer(
            id=1,
            chat_id=-100,
            notes_message_id=1,
            offer_message_id=2,
            starter_user_id=10,
            starter_username=None,
            starter_display_name=None,
            assigned_user_id=20,
            assigned_username="fin",
            assigned_display_name="Fin",
            notes_text="notes",
            status="pending",
            created_at=datetime.now(timezone.utc).isoformat(),
            last_reminder_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertFalse(pass_reminder_due(offer))

    def test_reminder_due_after_minute(self):
        from datetime import timedelta

        past = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
        offer = PassOffer(
            id=1,
            chat_id=-100,
            notes_message_id=1,
            offer_message_id=2,
            starter_user_id=10,
            starter_username=None,
            starter_display_name=None,
            assigned_user_id=20,
            assigned_username="fin",
            assigned_display_name="Fin",
            notes_text="notes",
            status="pending",
            created_at=past,
            last_reminder_at=past,
        )
        self.assertTrue(pass_reminder_due(offer))


class PassQueueHandlerTests(unittest.IsolatedAsyncioTestCase):
    EXAMPLE = """Frank Williams

23/02/1943

barclays

balance 20,000

savers with 30k

also has hsbc"""

    def setUp(self):
        self.db_path = os.path.join(
            tempfile.gettempdir(), f"pass_handler_test_{uuid.uuid4().hex}.db"
        )
        init_db(self.db_path)
        join_pass_queue(
            self.db_path,
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

    async def test_notes_handler_assigns_second_note_to_next_free_finisher(self):
        from config import load_settings

        join_pass_queue(
            self.db_path,
            telegram_user_id=222,
            telegram_username="finisher2",
            display_name="Finisher Two",
        )
        os.environ["DATABASE_PATH"] = self.db_path
        settings = load_settings()

        create_pass_offer(
            self.db_path,
            chat_id=-100,
            notes_message_id=400,
            starter_user_id=333,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=111,
            assigned_username="finisher",
            assigned_display_name="Finisher",
            notes_text="existing pass",
        )

        user = SimpleNamespace(
            id=333,
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

        notes_message.reply_text.assert_awaited_once()
        args, kwargs = notes_message.reply_text.await_args
        text = kwargs.get("text", args[0] if args else "")
        self.assertIn("take this pass", text)
        self.assertIn("finisher2", text.lower())
        busy = pending_pass_assignee_user_ids(self.db_path, chat_id=-100)
        self.assertEqual(busy, {111, 222})

    async def test_notes_handler_waits_when_all_finishers_busy(self):
        from config import load_settings

        os.environ["DATABASE_PATH"] = self.db_path
        settings = load_settings()

        create_pass_offer(
            self.db_path,
            chat_id=-100,
            notes_message_id=400,
            starter_user_id=222,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=111,
            assigned_username="finisher",
            assigned_display_name="Finisher",
            notes_text="existing pass",
        )

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
        notes_message.reply_text = AsyncMock()

        update = MagicMock()
        update.effective_user = user
        update.effective_chat = chat
        update.effective_message = notes_message

        context = MagicMock()
        context.bot_data = {"settings": settings}

        await pass_queue.notes_message_handler(update, context)

        notes_message.reply_text.assert_awaited_once()
        args, kwargs = notes_message.reply_text.await_args
        text = kwargs.get("text", args[0] if args else "")
        self.assertIn("already has a pass pending", text)
        self.assertIn("take or brush", text)

    async def test_notes_handler_skips_starter_as_assignee(self):
        from config import load_settings

        leave_pass_queue(self.db_path, 111)
        join_pass_queue(
            self.db_path,
            telegram_user_id=222,
            telegram_username="starter",
            display_name="Starter",
        )

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
        notes_message.reply_to_message = None
        notes_message.reply_text = AsyncMock()

        update = MagicMock()
        update.effective_user = user
        update.effective_chat = chat
        update.effective_message = notes_message

        context = MagicMock()
        context.bot_data = {"settings": settings}

        await pass_queue.notes_message_handler(update, context)

        notes_message.reply_text.assert_awaited_once()
        args, kwargs = notes_message.reply_text.await_args
        text = kwargs.get("text", args[0] if args else "")
        self.assertIn("can't take their own pass", text)

    async def test_notes_handler_prompts_for_balance(self):
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
        notes_message.text = "james adams\n31/01/2000\nbk"
        notes_message.caption = None
        notes_message.chat = chat
        notes_message.from_user = user
        notes_message.reply_to_message = None
        notes_message.reply_text = AsyncMock()

        update = MagicMock()
        update.effective_user = user
        update.effective_chat = chat
        update.effective_message = notes_message

        context = MagicMock()
        context.bot_data = {"settings": settings}

        await pass_queue.notes_message_handler(update, context)

        notes_message.reply_text.assert_awaited_once()
        args, kwargs = notes_message.reply_text.await_args
        text = kwargs.get("text", args[0] if args else "")
        self.assertIn("add balance to your notes", text)

    async def test_notes_handler_offers_after_balance_reply(self):
        from config import load_settings

        os.environ["DATABASE_PATH"] = self.db_path
        settings = load_settings()

        starter = SimpleNamespace(
            id=222,
            username="starter",
            first_name="Starter",
            last_name="",
            is_bot=False,
        )
        parent = MagicMock()
        parent.message_id = 500
        parent.text = "james adams\n31/01/2000\nbk"
        parent.caption = None
        parent.from_user = starter
        parent.reply_text = AsyncMock(
            return_value=SimpleNamespace(message_id=503)
        )

        user = SimpleNamespace(
            id=222,
            username="starter",
            first_name="Starter",
            last_name="",
            is_bot=False,
        )
        chat = SimpleNamespace(id=-100, type="supergroup")
        notes_message = MagicMock()
        notes_message.message_id = 502
        notes_message.text = "current 13004"
        notes_message.caption = None
        notes_message.chat = chat
        notes_message.from_user = user
        notes_message.reply_to_message = parent
        notes_message.reply_text = AsyncMock(
            return_value=SimpleNamespace(message_id=503)
        )

        update = MagicMock()
        update.effective_user = user
        update.effective_chat = chat
        update.effective_message = notes_message

        context = MagicMock()
        context.bot_data = {"settings": settings}

        await pass_queue.notes_message_handler(update, context)

        parent.reply_text.assert_awaited_once()
        args, kwargs = parent.reply_text.await_args
        text = kwargs.get("text", args[0] if args else "")
        self.assertIn("take this pass", text)
        self.assertIn("Read notes before taking pass", text)

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

    def test_notes_has_balance_current(self):
        self.assertTrue(notes_has_balance("james adams\n31/01/2000\ncurrent 13004"))

    def test_notes_has_balance_savings_gbp(self):
        self.assertTrue(notes_has_balance("Frank\nsavings £2834"))

    def test_notes_has_balance_savings_plain(self):
        self.assertTrue(notes_has_balance("Name\nsavings 9322"))

    def test_notes_has_balance_frank_example(self):
        self.assertTrue(notes_has_balance(self.EXAMPLE))

    def test_notes_missing_balance(self):
        self.assertFalse(notes_has_balance("james adams 31/01/2000 bk"))
        self.assertFalse(notes_has_balance("Frank\n23/02/1943\nbarclays"))


class PassQueueDbTests(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(
            tempfile.gettempdir(), f"pass_queue_test_{uuid.uuid4().hex}.db"
        )
        init_db(self.db_path)

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
        self.assertIsNotNone(offer.last_reminder_at)
        update_pass_offer(self.db_path, offer_id, status="taken", offer_message_id=99)
        offer = get_pass_offer(self.db_path, offer_id)
        assert offer is not None
        self.assertEqual(offer.status, "taken")
        self.assertEqual(offer.offer_message_id, 99)

    def test_active_pass_offer(self):
        self.assertIsNone(get_active_pass_offer(self.db_path))
        offer_id = create_pass_offer(
            self.db_path,
            chat_id=-100,
            notes_message_id=50,
            starter_user_id=10,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=20,
            assigned_username="finisher",
            assigned_display_name="Finisher",
            notes_text="notes",
        )
        active = get_active_pass_offer(self.db_path, chat_id=-100)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.id, offer_id)
        self.assertEqual(active.status, "pending")
        update_pass_offer(self.db_path, offer_id, status="taken")
        self.assertIsNone(get_active_pass_offer(self.db_path, chat_id=-100))

    def test_next_queue_assignee_skips_excluded_user(self):
        join_pass_queue(
            self.db_path,
            telegram_user_id=301,
            telegram_username="solo",
            display_name="Solo",
        )
        queue = list_pass_queue(self.db_path)
        self.assertIsNone(_next_queue_assignee(queue, exclude_user_id=301))

        join_pass_queue(
            self.db_path,
            telegram_user_id=302,
            telegram_username="next",
            display_name="Next",
        )
        queue = list_pass_queue(self.db_path)
        next_user = _next_queue_assignee(queue, exclude_user_id=301)
        self.assertIsNotNone(next_user)
        assert next_user is not None
        self.assertEqual(next_user.user_id, 302)

    def test_next_queue_assignee_skips_busy_users(self):
        join_pass_queue(
            self.db_path,
            telegram_user_id=401,
            telegram_username="first",
            display_name="First",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=402,
            telegram_username="second",
            display_name="Second",
        )
        queue = list_pass_queue(self.db_path)
        next_user = _next_queue_assignee(queue, busy_user_ids={401})
        self.assertIsNotNone(next_user)
        assert next_user is not None
        self.assertEqual(next_user.user_id, 402)

    def test_pending_pass_assignee_user_ids(self):
        self.assertEqual(pending_pass_assignee_user_ids(self.db_path), set())
        offer_a = create_pass_offer(
            self.db_path,
            chat_id=-100,
            notes_message_id=1,
            starter_user_id=10,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=20,
            assigned_username="a",
            assigned_display_name="A",
            notes_text="notes a",
        )
        create_pass_offer(
            self.db_path,
            chat_id=-100,
            notes_message_id=2,
            starter_user_id=10,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=30,
            assigned_username="b",
            assigned_display_name="B",
            notes_text="notes b",
        )
        self.assertEqual(
            pending_pass_assignee_user_ids(self.db_path, chat_id=-100),
            {20, 30},
        )
        self.assertEqual(
            pending_pass_assignee_user_ids(self.db_path, chat_id=-100, exclude_offer_id=offer_a),
            {30},
        )

    def test_vip_joins_ahead_of_standard_users(self):
        join_pass_queue(
            self.db_path,
            telegram_user_id=1,
            telegram_username="standard",
            display_name="Standard",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=2,
            telegram_username="standard2",
            display_name="Standard Two",
        )
        add_pass_queue_vip(
            self.db_path,
            telegram_user_id=99,
            telegram_username="vip",
            display_name="VIP",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=99,
            telegram_username="vip",
            display_name="VIP",
        )
        queue = list_pass_queue(self.db_path)
        self.assertEqual([entry.user_id for entry in queue], [99, 1, 2])
        self.assertTrue(queue[0].is_vip)

    def test_vips_keep_fifo_among_themselves(self):
        add_pass_queue_vip(
            self.db_path,
            telegram_user_id=10,
            telegram_username="vip1",
            display_name="VIP One",
        )
        add_pass_queue_vip(
            self.db_path,
            telegram_user_id=11,
            telegram_username="vip2",
            display_name="VIP Two",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=1,
            telegram_username="standard",
            display_name="Standard",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=10,
            telegram_username="vip1",
            display_name="VIP One",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=11,
            telegram_username="vip2",
            display_name="VIP Two",
        )
        queue = list_pass_queue(self.db_path)
        self.assertEqual([entry.user_id for entry in queue], [10, 11, 1])

    def test_addvip_repositions_existing_queue_member(self):
        join_pass_queue(
            self.db_path,
            telegram_user_id=1,
            telegram_username="standard",
            display_name="Standard",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=2,
            telegram_username="late",
            display_name="Late",
        )
        add_pass_queue_vip(
            self.db_path,
            telegram_user_id=2,
            telegram_username="late",
            display_name="Late",
        )
        queue = list_pass_queue(self.db_path)
        self.assertEqual([entry.user_id for entry in queue], [2, 1])
        self.assertTrue(is_pass_queue_vip(self.db_path, 2))

    def test_removevip_moves_user_to_back(self):
        add_pass_queue_vip(
            self.db_path,
            telegram_user_id=10,
            telegram_username="vip",
            display_name="VIP",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=1,
            telegram_username="standard",
            display_name="Standard",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=10,
            telegram_username="vip",
            display_name="VIP",
        )
        remove_pass_queue_vip(self.db_path, 10)
        queue = list_pass_queue(self.db_path)
        self.assertEqual([entry.user_id for entry in queue], [1, 10])
        self.assertFalse(is_pass_queue_vip(self.db_path, 10))

    def test_vip_brush_stays_before_standard_users(self):
        add_pass_queue_vip(
            self.db_path,
            telegram_user_id=10,
            telegram_username="vip",
            display_name="VIP",
        )
        add_pass_queue_vip(
            self.db_path,
            telegram_user_id=11,
            telegram_username="vip2",
            display_name="VIP Two",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=1,
            telegram_username="standard",
            display_name="Standard",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=10,
            telegram_username="vip",
            display_name="VIP",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=11,
            telegram_username="vip2",
            display_name="VIP Two",
        )
        rotate_pass_queue_user_to_back(self.db_path, 10)
        queue = list_pass_queue(self.db_path)
        self.assertEqual([entry.user_id for entry in queue], [11, 10, 1])

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
