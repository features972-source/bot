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
    assign_pending_pass_to_user,
    clear_circulating_pass_notes,
    create_pass_offer,
    get_active_pass_offer,
    get_pass_offer,
    get_pass_offer_brushed_user_ids,
    get_pass_queue_position,
    init_db,
    is_pass_queue_vip,
    join_pass_queue,
    leave_pass_queue,
    list_pass_queue,
    list_pending_pass_notes,
    list_pending_pass_offers,
    pass_offer_for_notes,
    pending_pass_assignee_user_ids,
    pending_pass_offer_for_notes,
    record_pass_offer_brush,
    remove_pass_queue_vip,
    rotate_pass_queue_user_to_back,
    update_pass_offer,
    upsert_pending_pass_note,
)
from handlers import pass_queue  # noqa: E402
from handlers.pass_queue import (  # noqa: E402
    _next_queue_assignee,
    pass_offer_expired,
    pass_reminder_due,
)
from notes_detect import (  # noqa: E402
    extract_notes_pass_summary,
    format_notes_summary_html,
    looks_like_notes,
    notes_balance_only,
    notes_has_balance,
)


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
        context.bot.send_message = AsyncMock(
            return_value=SimpleNamespace(message_id=502)
        )

        await pass_queue.notes_message_handler(update, context)

        context.bot.send_message.assert_awaited()
        args, kwargs = context.bot.send_message.await_args
        text = kwargs.get("text", args[0] if args else "")
        self.assertIn("Take this pass", text)
        self.assertIn("Quick look", text)
        self.assertIn("Balance", text)
        self.assertIn("DOB", text)

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
        context.bot.send_message = AsyncMock(
            return_value=SimpleNamespace(message_id=502)
        )

        await pass_queue.notes_message_handler(update, context)

        context.bot.send_message.assert_awaited_once()
        args, kwargs = context.bot.send_message.await_args
        text = kwargs.get("text", args[0] if args else "")
        self.assertIn("Take this pass", text)
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
        self.assertIn("Notes saved", text)
        self.assertIn("already has a pass pending", text)

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
        self.assertIn("Notes saved", text)
        self.assertIn("can't take your own pass", text)
        self.assertIn("/joinqueue", text)

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
        context.bot.send_message = AsyncMock(
            return_value=SimpleNamespace(message_id=503)
        )

        await pass_queue.notes_message_handler(update, context)

        context.bot.send_message.assert_awaited_once()
        args, kwargs = context.bot.send_message.await_args
        text = kwargs.get("text", args[0] if args else "")
        self.assertIn("Take this pass", text)
        self.assertIn("Read full notes before taking pass", text)
        self.assertEqual(kwargs.get("reply_to_message_id"), 500)

    async def test_joinqueue_assigns_pending_pass_to_new_finisher(self):
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

        notes = "james adams 31/01/2000 bk LT - 23232 current 3223"
        upsert_pending_pass_note(
            self.db_path,
            chat_id=-100,
            notes_message_id=501,
            starter_user_id=222,
            starter_username="starter",
            starter_display_name="Starter",
            notes_text=notes,
        )

        user = SimpleNamespace(
            id=111,
            username="finisher",
            first_name="Finisher",
            last_name="",
            is_bot=False,
        )
        chat = SimpleNamespace(id=-100, type="supergroup")
        message = MagicMock()
        message.reply_text = AsyncMock()

        update = MagicMock()
        update.effective_user = user
        update.effective_message = message

        context = MagicMock()
        context.bot_data = {"settings": settings}
        context.bot.send_message = AsyncMock(
            return_value=SimpleNamespace(message_id=502)
        )

        await pass_queue.joinqueue_command(update, context)

        offer = get_pass_offer(self.db_path, 1)
        self.assertIsNotNone(offer)
        self.assertEqual(offer.assigned_user_id, 111)
        self.assertEqual(offer.notes_text, notes)
        self.assertEqual(list_pending_pass_notes(self.db_path), [])
        context.bot.send_message.assert_awaited()
        message.reply_text.assert_awaited()
        reply_args, reply_kwargs = message.reply_text.await_args
        reply_text = reply_kwargs.get("text", reply_args[0] if reply_args else "")
        self.assertIn("waiting pass was sent", reply_text)

    async def test_joinqueue_assigns_pending_when_already_in_queue(self):
        from config import load_settings

        join_pass_queue(
            self.db_path,
            telegram_user_id=333,
            telegram_username="free",
            display_name="Free",
        )
        upsert_pending_pass_note(
            self.db_path,
            chat_id=-100,
            notes_message_id=601,
            starter_user_id=222,
            starter_username="starter",
            starter_display_name="Starter",
            notes_text="notes with current 1000",
        )

        os.environ["DATABASE_PATH"] = self.db_path
        settings = load_settings()

        user = SimpleNamespace(
            id=333,
            username="free",
            first_name="Free",
            last_name="",
            is_bot=False,
        )
        message = MagicMock()
        message.reply_text = AsyncMock()

        update = MagicMock()
        update.effective_user = user
        update.effective_message = message

        context = MagicMock()
        context.bot_data = {"settings": settings}
        context.bot.send_message = AsyncMock(
            return_value=SimpleNamespace(message_id=602)
        )

        await pass_queue.joinqueue_command(update, context)

        context.bot.send_message.assert_awaited()
        message.reply_text.assert_awaited()
        args, kwargs = message.reply_text.await_args
        text = kwargs.get("text", args[0] if args else "")
        self.assertIn("waiting pass was sent", text)
        self.assertIn("already in the queue", text)

    async def test_manual_override_take_by_non_queue_user(self):
        from config import load_settings

        os.environ["DATABASE_PATH"] = self.db_path
        settings = load_settings()

        offer_id = create_pass_offer(
            self.db_path,
            chat_id=-100,
            notes_message_id=501,
            starter_user_id=222,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=111,
            assigned_username="finisher",
            assigned_display_name="Finisher",
            notes_text=self.EXAMPLE,
        )
        update_pass_offer(self.db_path, offer_id, manual_override=True)

        user = SimpleNamespace(
            id=999,
            username="random",
            first_name="Random",
            last_name="User",
            is_bot=False,
        )
        query = MagicMock()
        query.data = f"pass:take:{offer_id}"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = MagicMock(message_id=600)

        update = MagicMock()
        update.callback_query = query
        update.effective_user = user

        context = MagicMock()
        context.bot_data = {"settings": settings}
        context.bot.send_message = AsyncMock()

        await pass_queue.pass_callback(update, context)

        context.bot.send_message.assert_awaited()
        query.edit_message_text.assert_awaited()
        args, kwargs = query.edit_message_text.await_args
        text = kwargs.get("text", args[0] if args else "")
        self.assertIn("has taken the manual override", text)
        offer = get_pass_offer(self.db_path, offer_id)
        assert offer is not None
        self.assertEqual(offer.status, "taken")

    async def test_notes_handler_prompts_for_full_notes_when_balance_only(self):
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
        notes_message.text = "Current 5600\nSavings 3400"
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
        self.assertIn("send full notes, not just the balance", text)
        self.assertFalse(pass_offer_for_notes(self.db_path, -100, 501))

    async def test_notes_saved_when_queue_empty(self):
        from config import load_settings

        leave_pass_queue(self.db_path, 111)
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
        notes_message.text = "james adams 31/01/2000 bk LT - 23232 current 3223"
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
        self.assertIn("Notes saved", text)
        self.assertIn("/joinqueue", text)
        pending = list_pending_pass_notes(self.db_path)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].notes_message_id, 501)

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

    def test_single_line_notes_with_balance(self):
        notes = "james adams 31/01/2000 bk LT - 23232 current 3223"
        self.assertTrue(looks_like_notes(notes))
        self.assertTrue(looks_like_notes(notes, queue_waiting=True))
        self.assertTrue(notes_has_balance(notes))

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

    def test_notes_has_balance_amount_before_current(self):
        self.assertTrue(notes_has_balance("£3737.38 current"))
        self.assertTrue(
            notes_has_balance("David rechard\n20/03/56\nBk\n£3737.38 current")
        )
        self.assertTrue(notes_has_balance("3737.38 current"))

    def test_notes_has_balance_savings_gbp(self):
        self.assertTrue(notes_has_balance("Frank\nsavings £2834"))

    def test_notes_has_balance_savings_plain(self):
        self.assertTrue(notes_has_balance("Name\nsavings 9322"))

    def test_notes_has_balance_frank_example(self):
        self.assertTrue(notes_has_balance(self.EXAMPLE))

    def test_notes_has_balance_lt_and_bala(self):
        notes = "james adams\n31/01/2000\nbk\nLT - 23232\nbala 3222"
        self.assertTrue(notes_has_balance(notes))
        self.assertTrue(notes_has_balance("james adams\nbala 3222"))
        self.assertTrue(notes_has_balance("james adams\nLT - 23232"))

    def test_notes_has_balance_plain_gbp_line(self):
        notes = "50\nJannett Burt\n14/09/67\n£5000"
        self.assertTrue(notes_has_balance(notes))
        self.assertTrue(notes_has_balance("£5000"))
        self.assertTrue(notes_has_balance("5000"))
        self.assertFalse(notes_has_balance("50\nJannett Burt\n14/09/67"))
        self.assertTrue(notes_balance_only("£5000"))
        self.assertFalse(notes_balance_only(notes))

    def test_notes_pass_summary_frank_example(self):
        summary = extract_notes_pass_summary(self.EXAMPLE)
        self.assertIn("balance", (summary.balance or "").lower())
        self.assertEqual(summary.dob, "23/02/1943")
        self.assertIn("Barclays", summary.bank or "")
        self.assertIn("Hsbc", summary.bank or "")

    def test_notes_pass_summary_ian_davis(self):
        summary = extract_notes_pass_summary(self.EXAMPLE_1)
        self.assertEqual(summary.online, "No online banking")
        self.assertEqual(summary.bank, "Barclaycard")

    def test_notes_pass_summary_jaqueline(self):
        summary = extract_notes_pass_summary(self.EXAMPLE_2)
        self.assertEqual(summary.dob, "15/09/1967")
        self.assertEqual(summary.online, "Online banking")

    def test_notes_pass_summary_jannett_burt(self):
        notes = "50\nJannett Burt\n14/09/67\n£5000"
        summary = extract_notes_pass_summary(notes)
        self.assertEqual(summary.dob, "14/09/67")
        self.assertIn("£5000", summary.balance or "")
        html_summary = format_notes_summary_html(notes)
        self.assertIn("Quick look", html_summary)
        self.assertIn("💰", html_summary)
        self.assertIn("🎂", html_summary)

    def test_format_notes_summary_html(self):
        html_summary = format_notes_summary_html(
            "David rechard\n20/03/56\nBk\n£3737.38 current"
        )
        self.assertIn("Quick look", html_summary)
        self.assertIn("💰", html_summary)
        self.assertIn("🎂", html_summary)
        self.assertIn("🏦", html_summary)
        self.assertIn("Bk", html_summary)

    def test_notes_pass_summary_christopher_jenkins(self):
        notes = """Christopher Jenkins
11/05/1930
He's computer literate
Bala £100,038
Has crypto Bala - £20,393.29
IG11 7KG
Flat 18

WhatsApp prepped

Cookie level : 1000/10"""
        summary = extract_notes_pass_summary(notes)
        self.assertEqual(summary.dob, "11/05/1930")
        self.assertIn("100,038", summary.balance or "")
        self.assertIsNone(summary.online)
        self.assertEqual(summary.cookie_level, "1000/10")
        self.assertIn("Has crypto", summary.crypto or "")
        self.assertIn("20,393.29", summary.crypto or "")
        html_summary = format_notes_summary_html(notes)
        self.assertIn("🍪", html_summary)
        self.assertIn("🪙", html_summary)
        self.assertIn("1000/10", html_summary)
        self.assertTrue(notes_has_balance(notes))

    def test_notes_pass_summary_multiple_banks(self):
        notes = """Robert Gomm
2/3/1944
Halifax main bank last 4 dig 3378
barclays
hsbc
current 5000"""
        summary = extract_notes_pass_summary(notes)
        self.assertIn("Halifax", summary.bank or "")
        self.assertIn("Barclays", summary.bank or "")
        self.assertIn("Hsbc", summary.bank or "")
        self.assertEqual(
            summary.bank,
            "Halifax · Barclays · Hsbc",
        )

    def test_notes_missing_balance(self):
        self.assertFalse(notes_has_balance("james adams 31/01/2000 bk"))
        self.assertFalse(notes_has_balance("Frank\n23/02/1943\nbarclays"))

    def test_notes_balance_only(self):
        self.assertTrue(notes_balance_only("Current 5600\nSavings 3400"))
        self.assertTrue(notes_balance_only("current 5600"))
        self.assertTrue(notes_balance_only("£3737.38 current"))
        self.assertFalse(
            notes_balance_only("David rechard\n20/03/56\nBk\n£3737.38 current")
        )
        self.assertFalse(notes_balance_only(self.EXAMPLE))


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

    def test_assign_pending_pass_to_user(self):
        upsert_pending_pass_note(
            self.db_path,
            chat_id=-100,
            notes_message_id=501,
            starter_user_id=222,
            starter_username="starter",
            starter_display_name="Starter",
            notes_text="notes with current 1000",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=111,
            telegram_username="finisher",
            display_name="Finisher",
        )
        offer = assign_pending_pass_to_user(
            self.db_path,
            assigned_user_id=111,
            assigned_username="finisher",
            assigned_display_name="Finisher",
        )
        self.assertIsNotNone(offer)
        assert offer is not None
        self.assertEqual(offer.assigned_user_id, 111)
        self.assertEqual(offer.starter_user_id, 222)
        self.assertEqual(list_pending_pass_notes(self.db_path), [])
        self.assertIsNone(
            assign_pending_pass_to_user(
                self.db_path,
                assigned_user_id=111,
                assigned_username="finisher",
                assigned_display_name="Finisher",
            )
        )

    def test_assign_pending_skips_starter(self):
        upsert_pending_pass_note(
            self.db_path,
            chat_id=-100,
            notes_message_id=501,
            starter_user_id=222,
            starter_username="starter",
            starter_display_name="Starter",
            notes_text="notes with current 1000",
        )
        offer = assign_pending_pass_to_user(
            self.db_path,
            assigned_user_id=222,
            assigned_username="starter",
            assigned_display_name="Starter",
        )
        self.assertIsNone(offer)
        self.assertEqual(len(list_pending_pass_notes(self.db_path)), 1)

    def test_assign_pending_after_taken_offer_on_same_notes(self):
        offer_id = create_pass_offer(
            self.db_path,
            chat_id=-100,
            notes_message_id=501,
            starter_user_id=222,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=111,
            assigned_username="finisher",
            assigned_display_name="Finisher",
            notes_text="old pass",
        )
        update_pass_offer(self.db_path, offer_id, status="taken")
        upsert_pending_pass_note(
            self.db_path,
            chat_id=-100,
            notes_message_id=501,
            starter_user_id=222,
            starter_username="starter",
            starter_display_name="Starter",
            notes_text="notes with current 1000",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=333,
            telegram_username="free",
            display_name="Free",
        )
        offer = assign_pending_pass_to_user(
            self.db_path,
            assigned_user_id=333,
            assigned_username="free",
            assigned_display_name="Free",
        )
        self.assertIsNotNone(offer)
        assert offer is not None
        self.assertEqual(offer.status, "pending")
        self.assertTrue(pending_pass_offer_for_notes(self.db_path, -100, 501))

    def test_clear_circulating_pass_notes(self):
        create_pass_offer(
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
        offer_taken = create_pass_offer(
            self.db_path,
            chat_id=-100,
            notes_message_id=3,
            starter_user_id=10,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=40,
            assigned_username="c",
            assigned_display_name="C",
            notes_text="notes c",
        )
        update_pass_offer(self.db_path, offer_taken, status="taken")
        record_pass_offer_brush(self.db_path, 1, 20)
        upsert_pending_pass_note(
            self.db_path,
            chat_id=-100,
            notes_message_id=99,
            starter_user_id=10,
            starter_username="starter",
            starter_display_name="Starter",
            notes_text="waiting notes",
        )

        counts = clear_circulating_pass_notes(self.db_path)
        self.assertEqual(counts, {"pending_offers": 2, "pending_notes": 1})
        self.assertEqual(list_pending_pass_offers(self.db_path), [])
        self.assertEqual(list_pending_pass_notes(self.db_path), [])
        self.assertEqual(get_pass_offer_brushed_user_ids(self.db_path, 1), set())
        taken = get_pass_offer(self.db_path, offer_taken)
        assert taken is not None
        self.assertEqual(taken.status, "taken")

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

    def test_next_queue_assignee_skips_brushed_users(self):
        join_pass_queue(
            self.db_path,
            telegram_user_id=501,
            telegram_username="first",
            display_name="First",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=502,
            telegram_username="second",
            display_name="Second",
        )
        queue = list_pass_queue(self.db_path)
        next_user = _next_queue_assignee(queue, exclude_user_ids={501})
        self.assertIsNotNone(next_user)
        assert next_user is not None
        self.assertEqual(next_user.user_id, 502)
        self.assertIsNone(_next_queue_assignee(queue, exclude_user_ids={501, 502}))

    def test_record_pass_offer_brush(self):
        offer_id = create_pass_offer(
            self.db_path,
            chat_id=-100,
            notes_message_id=1,
            starter_user_id=10,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=20,
            assigned_username="a",
            assigned_display_name="A",
            notes_text="notes",
        )
        record_pass_offer_brush(self.db_path, offer_id, 20)
        record_pass_offer_brush(self.db_path, offer_id, 20)
        self.assertEqual(get_pass_offer_brushed_user_ids(self.db_path, offer_id), {20})

    def test_pass_offer_expired(self):
        from datetime import timedelta

        past = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
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
        self.assertTrue(pass_offer_expired(offer))
        self.assertFalse(pass_reminder_due(offer))

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
