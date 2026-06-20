"""Stress and error-path tests for pass queue (5 notes / 2 min volume)."""

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
os.environ.setdefault("BOT_TOKEN", "0000000000:TEST_TOKEN_NOT_REAL")
os.environ.setdefault("CLOUD_DEPLOYED", "true")
os.environ.setdefault("BOT_INSTANCE_ID", "q2")

from telegram.error import BadRequest, Forbidden  # noqa: E402

from database import (  # noqa: E402
    PassOffer,
    assign_pending_pass_to_user,
    clear_circulating_pass_notes,
    create_pass_offer,
    get_pass_offer,
    init_db,
    join_pass_queue,
    leave_pass_queue,
    list_pending_pass_notes,
    list_pending_pass_offers,
    pending_pass_assignee_user_ids,
    pending_pass_offer_for_notes,
    update_pass_offer,
    upsert_pending_pass_note,
)
from handlers import pass_queue  # noqa: E402
from handlers.pass_queue import _next_queue_assignee  # noqa: E402
from notes_detect import looks_like_notes, notes_has_balance  # noqa: E402

NOTES_TEMPLATE = """Customer {n}
01/01/1990
barclays
current {amount}"""


def _notes_text(n: int) -> str:
    return NOTES_TEMPLATE.format(n=n, amount=5000 + n)


def _simulate_incoming_note(
    db_path: str,
    *,
    chat_id: int,
    notes_message_id: int,
    starter_user_id: int,
    n: int,
) -> str:
    """Mirror _offer_pass assignment logic without Telegram I/O."""
    queue = __import__("database").list_pass_queue(db_path)
    busy = pending_pass_assignee_user_ids(db_path, chat_id=chat_id)
    assigned = (
        _next_queue_assignee(
            queue,
            exclude_user_id=starter_user_id,
            busy_user_ids=busy,
        )
        if queue
        else None
    )
    notes_text = _notes_text(n)
    if assigned is not None:
        create_pass_offer(
            db_path,
            chat_id=chat_id,
            notes_message_id=notes_message_id,
            starter_user_id=starter_user_id,
            starter_username=f"starter{n}",
            starter_display_name=f"Starter {n}",
            assigned_user_id=assigned.user_id,
            assigned_username=assigned.telegram_username,
            assigned_display_name=assigned.display_name,
            notes_text=notes_text,
        )
        return "offer"
    upsert_pending_pass_note(
        db_path,
        chat_id=chat_id,
        notes_message_id=notes_message_id,
        starter_user_id=starter_user_id,
        starter_username=f"starter{n}",
        starter_display_name=f"Starter {n}",
        notes_text=notes_text,
    )
    return "pending"


class PassQueueVolumeTests(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(
            tempfile.gettempdir(), f"pass_stress_vol_{uuid.uuid4().hex}.db"
        )
        init_db(self.db_path)
        self.chat_id = -100

    def _join_finishers(self, count: int) -> list[int]:
        ids = []
        for i in range(count):
            uid = 1000 + i
            join_pass_queue(
                self.db_path,
                telegram_user_id=uid,
                telegram_username=f"fin{i}",
                display_name=f"Finisher {i}",
            )
            ids.append(uid)
        return ids

    def test_five_notes_two_finishers_two_offers_three_pending(self):
        self._join_finishers(2)
        results = []
        for i in range(5):
            results.append(
                _simulate_incoming_note(
                    self.db_path,
                    chat_id=self.chat_id,
                    notes_message_id=500 + i,
                    starter_user_id=2000 + i,
                    n=i + 1,
                )
            )
        self.assertEqual(results.count("offer"), 2)
        self.assertEqual(results.count("pending"), 3)
        self.assertEqual(len(list_pending_pass_offers(self.db_path)), 2)
        self.assertEqual(len(list_pending_pass_notes(self.db_path)), 3)
        busy = pending_pass_assignee_user_ids(self.db_path, chat_id=self.chat_id)
        self.assertEqual(busy, {1000, 1001})

    def test_five_notes_five_finishers_all_offered(self):
        self._join_finishers(5)
        for i in range(5):
            self.assertEqual(
                _simulate_incoming_note(
                    self.db_path,
                    chat_id=self.chat_id,
                    notes_message_id=600 + i,
                    starter_user_id=3000 + i,
                    n=i + 1,
                ),
                "offer",
            )
        self.assertEqual(len(list_pending_pass_offers(self.db_path)), 5)
        self.assertEqual(list_pending_pass_notes(self.db_path), [])
        self.assertEqual(
            len(pending_pass_assignee_user_ids(self.db_path, chat_id=self.chat_id)),
            5,
        )

    def test_five_notes_no_finishers_all_waiting(self):
        for i in range(5):
            self.assertEqual(
                _simulate_incoming_note(
                    self.db_path,
                    chat_id=self.chat_id,
                    notes_message_id=700 + i,
                    starter_user_id=4000 + i,
                    n=i + 1,
                ),
                "pending",
            )
        self.assertEqual(len(list_pending_pass_notes(self.db_path)), 5)
        self.assertEqual(list_pending_pass_offers(self.db_path), [])

    def test_pending_fifo_on_sequential_joinqueue(self):
        for i in range(5):
            upsert_pending_pass_note(
                self.db_path,
                chat_id=self.chat_id,
                notes_message_id=800 + i,
                starter_user_id=5000 + i,
                starter_username=f"s{i}",
                starter_display_name=f"S{i}",
                notes_text=_notes_text(i + 1),
            )
        self.assertEqual(len(list_pending_pass_notes(self.db_path)), 5)

        assigned_notes_ids = []
        for fin_id in (1100, 1101, 1102, 1103, 1104):
            join_pass_queue(
                self.db_path,
                telegram_user_id=fin_id,
                telegram_username=f"late{fin_id}",
                display_name=f"Late {fin_id}",
            )
            offer = assign_pending_pass_to_user(
                self.db_path,
                assigned_user_id=fin_id,
                assigned_username=f"late{fin_id}",
                assigned_display_name=f"Late {fin_id}",
            )
            self.assertIsNotNone(offer)
            assert offer is not None
            assigned_notes_ids.append(offer.notes_message_id)

        self.assertEqual(assigned_notes_ids, [800, 801, 802, 803, 804])
        self.assertEqual(list_pending_pass_notes(self.db_path), [])

    def test_notes_templates_valid_for_detection(self):
        for i in range(1, 6):
            text = _notes_text(i)
            self.assertTrue(looks_like_notes(text, queue_waiting=True))
            self.assertTrue(notes_has_balance(text))

    def test_clearnotes_under_load(self):
        self._join_finishers(2)
        for i in range(5):
            _simulate_incoming_note(
                self.db_path,
                chat_id=self.chat_id,
                notes_message_id=900 + i,
                starter_user_id=6000 + i,
                n=i + 1,
            )
        counts = clear_circulating_pass_notes(self.db_path)
        self.assertEqual(counts["pending_offers"], 2)
        self.assertEqual(counts["pending_notes"], 3)
        self.assertEqual(list_pending_pass_offers(self.db_path), [])
        self.assertEqual(list_pending_pass_notes(self.db_path), [])

    def test_duplicate_active_offer_blocks_reoffer_for_same_message(self):
        self._join_finishers(1)
        _simulate_incoming_note(
            self.db_path,
            chat_id=self.chat_id,
            notes_message_id=950,
            starter_user_id=7000,
            n=1,
        )
        self.assertTrue(
            pending_pass_offer_for_notes(self.db_path, self.chat_id, 950)
        )
        offer = list_pending_pass_offers(self.db_path)[0]
        update_pass_offer(self.db_path, offer.id, status="taken")
        upsert_pending_pass_note(
            self.db_path,
            chat_id=self.chat_id,
            notes_message_id=950,
            starter_user_id=7000,
            starter_username="starter",
            starter_display_name="Starter",
            notes_text=_notes_text(1),
        )
        reassigned = assign_pending_pass_to_user(
            self.db_path,
            assigned_user_id=1000,
            assigned_username="fin0",
            assigned_display_name="Finisher 0",
        )
        self.assertIsNotNone(reassigned)
        assert reassigned is not None
        self.assertEqual(reassigned.notes_message_id, 950)
        self.assertTrue(
            pending_pass_offer_for_notes(self.db_path, self.chat_id, 950)
        )


class PassQueueDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db_path = os.path.join(
            tempfile.gettempdir(), f"pass_stress_del_{uuid.uuid4().hex}.db"
        )
        init_db(self.db_path)

    def _sample_offer(self) -> PassOffer:
        join_pass_queue(
            self.db_path,
            telegram_user_id=111,
            telegram_username="fin",
            display_name="Fin",
        )
        offer_id = create_pass_offer(
            self.db_path,
            chat_id=-100,
            notes_message_id=501,
            starter_user_id=222,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=111,
            assigned_username="fin",
            assigned_display_name="Fin",
            notes_text=_notes_text(1),
        )
        offer = get_pass_offer(self.db_path, offer_id)
        assert offer is not None
        return offer

    async def test_deliver_pass_offer_retries_without_reply(self):
        offer = self._sample_offer()
        bot = AsyncMock()
        bot.send_message = AsyncMock(
            side_effect=[
                BadRequest("Message to reply not found"),
                SimpleNamespace(message_id=999),
            ]
        )
        ok = await pass_queue._deliver_pass_offer(
            bot,
            self.db_path,
            offer,
            reply_to_message_id=501,
        )
        self.assertTrue(ok)
        self.assertEqual(bot.send_message.await_count, 2)
        refreshed = get_pass_offer(self.db_path, offer.id)
        assert refreshed is not None
        self.assertEqual(refreshed.offer_message_id, 999)

    async def test_deliver_pass_offer_fails_when_both_attempts_fail(self):
        offer = self._sample_offer()
        bot = AsyncMock()
        bot.send_message = AsyncMock(side_effect=BadRequest("Chat not found"))
        ok = await pass_queue._deliver_pass_offer(
            bot,
            self.db_path,
            offer,
            reply_to_message_id=501,
        )
        self.assertFalse(ok)
        refreshed = get_pass_offer(self.db_path, offer.id)
        assert refreshed is not None
        self.assertIsNone(refreshed.offer_message_id)

    async def test_joinqueue_delivery_failure_no_false_success(self):
        from config import load_settings

        upsert_pending_pass_note(
            self.db_path,
            chat_id=-100,
            notes_message_id=601,
            starter_user_id=222,
            starter_username="starter",
            starter_display_name="Starter",
            notes_text=_notes_text(2),
        )
        os.environ["DATABASE_PATH"] = self.db_path
        settings = load_settings()

        user = SimpleNamespace(
            id=111,
            username="fin",
            first_name="Fin",
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
        context.bot.send_message = AsyncMock(side_effect=BadRequest("blocked"))

        await pass_queue.joinqueue_command(update, context)

        message.reply_text.assert_awaited()
        args, kwargs = message.reply_text.await_args
        text = kwargs.get("text", args[0] if args else "")
        self.assertNotIn("waiting pass was sent", text)
        self.assertIn("could not post to the group", text)
        offer = get_pass_offer(self.db_path, 1)
        assert offer is not None
        self.assertEqual(offer.status, "pending")

    async def test_offer_survives_failed_delivery_for_joinqueue_retry(self):
        from config import load_settings

        join_pass_queue(
            self.db_path,
            telegram_user_id=111,
            telegram_username="fin",
            display_name="Fin",
        )
        os.environ["DATABASE_PATH"] = self.db_path
        settings = load_settings()

        starter = SimpleNamespace(
            id=222,
            username="starter",
            first_name="Starter",
            last_name="",
            is_bot=False,
        )
        chat = SimpleNamespace(id=-100, type="supergroup")
        notes_message = MagicMock()
        notes_message.message_id = 701
        notes_message.text = _notes_text(3)
        notes_message.chat = chat
        notes_message.from_user = starter

        update = MagicMock()
        update.effective_user = starter
        update.effective_chat = chat
        update.effective_message = notes_message

        context = MagicMock()
        context.bot_data = {"settings": settings}
        context.bot.send_message = AsyncMock(side_effect=BadRequest("fail"))

        await pass_queue.notes_message_handler(update, context)

        offers = list_pending_pass_offers(self.db_path)
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].assigned_user_id, 111)
        self.assertIsNone(offers[0].offer_message_id)

    async def test_take_pass_auto_assigns_oldest_pending_to_free_finisher(self):
        from config import load_settings

        join_pass_queue(
            self.db_path,
            telegram_user_id=111,
            telegram_username="fin1",
            display_name="Fin One",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=222,
            telegram_username="fin2",
            display_name="Fin Two",
        )
        offer_id = create_pass_offer(
            self.db_path,
            chat_id=-100,
            notes_message_id=801,
            starter_user_id=333,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=111,
            assigned_username="fin1",
            assigned_display_name="Fin One",
            notes_text=_notes_text(1),
        )
        for msg_id, n in ((802, 2), (803, 3)):
            upsert_pending_pass_note(
                self.db_path,
                chat_id=-100,
                notes_message_id=msg_id,
                starter_user_id=400 + n,
                starter_username=f"s{n}",
                starter_display_name=f"S{n}",
                notes_text=_notes_text(n),
            )

        os.environ["DATABASE_PATH"] = self.db_path
        settings = load_settings()

        taker = SimpleNamespace(
            id=111,
            username="fin1",
            first_name="Fin",
            last_name="One",
            is_bot=False,
        )
        query = MagicMock()
        query.data = f"pass:take:{offer_id}"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = MagicMock(message_id=900)

        update = MagicMock()
        update.callback_query = query
        update.effective_user = taker

        context = MagicMock()
        context.bot_data = {"settings": settings}
        context.bot.send_message = AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=901),
                SimpleNamespace(message_id=902),
            ]
        )

        await pass_queue.pass_callback(update, context)

        self.assertEqual(len(list_pending_pass_notes(self.db_path)), 1)
        pending_offers = list_pending_pass_offers(self.db_path)
        self.assertEqual(len(pending_offers), 1)
        self.assertEqual(pending_offers[0].assigned_user_id, 222)
        self.assertEqual(pending_offers[0].notes_message_id, 802)

    async def test_brush_frees_finisher_for_other_pending_pass(self):
        from config import load_settings

        join_pass_queue(
            self.db_path,
            telegram_user_id=111,
            telegram_username="fin1",
            display_name="Fin One",
        )
        join_pass_queue(
            self.db_path,
            telegram_user_id=222,
            telegram_username="fin2",
            display_name="Fin Two",
        )
        offer_id = create_pass_offer(
            self.db_path,
            chat_id=-100,
            notes_message_id=901,
            starter_user_id=333,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=111,
            assigned_username="fin1",
            assigned_display_name="Fin One",
            notes_text=_notes_text(1),
        )
        upsert_pending_pass_note(
            self.db_path,
            chat_id=-100,
            notes_message_id=902,
            starter_user_id=444,
            starter_username="s2",
            starter_display_name="S2",
            notes_text=_notes_text(2),
        )

        os.environ["DATABASE_PATH"] = self.db_path
        settings = load_settings()

        brusher = SimpleNamespace(
            id=111,
            username="fin1",
            first_name="Fin",
            last_name="One",
            is_bot=False,
        )
        query = MagicMock()
        query.data = f"pass:brush:{offer_id}"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = MagicMock(message_id=910)

        update = MagicMock()
        update.callback_query = query
        update.effective_user = brusher

        context = MagicMock()
        context.bot_data = {"settings": settings}
        context.bot.send_message = AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=911),
                SimpleNamespace(message_id=912),
            ]
        )

        await pass_queue.pass_callback(update, context)

        main = get_pass_offer(self.db_path, offer_id)
        assert main is not None
        self.assertEqual(main.assigned_user_id, 222)
        extra = [
            o
            for o in list_pending_pass_offers(self.db_path)
            if o.id != offer_id
        ]
        self.assertEqual(len(extra), 1)
        self.assertEqual(extra[0].assigned_user_id, 111)
        self.assertEqual(extra[0].notes_message_id, 902)
        self.assertEqual(list_pending_pass_notes(self.db_path), [])


class PassQueueCallbackErrorTests(unittest.IsolatedAsyncioTestCase):
    EXAMPLE = NOTES_TEMPLATE.format(n=1, amount=6000)

    def setUp(self):
        self.db_path = os.path.join(
            tempfile.gettempdir(), f"pass_stress_cb_{uuid.uuid4().hex}.db"
        )
        init_db(self.db_path)
        join_pass_queue(
            self.db_path,
            telegram_user_id=111,
            telegram_username="fin",
            display_name="Fin",
        )

    def _settings(self):
        os.environ["DATABASE_PATH"] = self.db_path
        from config import load_settings

        return load_settings()

    def _offer_id(self, **kwargs) -> int:
        defaults = dict(
            chat_id=-100,
            notes_message_id=501,
            starter_user_id=222,
            starter_username="starter",
            starter_display_name="Starter",
            assigned_user_id=111,
            assigned_username="fin",
            assigned_display_name="Fin",
            notes_text=self.EXAMPLE,
        )
        defaults.update(kwargs)
        return create_pass_offer(self.db_path, **defaults)

    async def _callback(
        self,
        *,
        action: str,
        offer_id: int,
        user_id: int,
        username: str = "user",
    ):
        settings = self._settings()
        user = SimpleNamespace(
            id=user_id,
            username=username,
            first_name=username.title(),
            last_name="",
            is_bot=False,
        )
        query = MagicMock()
        query.data = f"pass:{action}:{offer_id}"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = MagicMock(message_id=600)

        update = MagicMock()
        update.callback_query = query
        update.effective_user = user

        context = MagicMock()
        context.bot_data = {"settings": settings}
        context.bot.send_message = AsyncMock(
            return_value=SimpleNamespace(message_id=601)
        )

        await pass_queue.pass_callback(update, context)
        return query

    async def test_callback_missing_offer(self):
        query = await self._callback(action="take", offer_id=9999, user_id=111)
        query.answer.assert_awaited_with("This pass is no longer available.")

    async def test_callback_already_taken(self):
        offer_id = self._offer_id()
        update_pass_offer(self.db_path, offer_id, status="taken")
        query = await self._callback(action="take", offer_id=offer_id, user_id=111)
        query.answer.assert_awaited_with("This pass was already handled.")

    async def test_callback_expired_status(self):
        offer_id = self._offer_id()
        update_pass_offer(self.db_path, offer_id, status="expired")
        query = await self._callback(action="take", offer_id=offer_id, user_id=111)
        args, kwargs = query.answer.await_args
        self.assertEqual(args[0], "This pass timed out.")
        self.assertTrue(kwargs.get("show_alert"))

    async def test_callback_past_expiry_time(self):
        from unittest.mock import patch

        offer_id = self._offer_id()
        offer = get_pass_offer(self.db_path, offer_id)
        assert offer is not None
        with patch.object(pass_queue, "pass_offer_expired", return_value=True):
            query = await self._callback(action="take", offer_id=offer_id, user_id=111)
        args, kwargs = query.answer.await_args
        self.assertEqual(args[0], "This pass timed out.")
        refreshed = get_pass_offer(self.db_path, offer_id)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "expired")

    async def test_callback_wrong_user_on_assigned_pass(self):
        offer_id = self._offer_id()
        query = await self._callback(action="take", offer_id=offer_id, user_id=999)
        query.answer.assert_awaited_with(
            "This pass is assigned to someone else.",
            show_alert=True,
        )

    async def test_callback_starter_cannot_take_manual_override(self):
        offer_id = self._offer_id()
        update_pass_offer(self.db_path, offer_id, manual_override=True)
        query = await self._callback(
            action="take",
            offer_id=offer_id,
            user_id=222,
            username="starter",
        )
        query.answer.assert_awaited_with(
            "You can't take your own pass.",
            show_alert=True,
        )

    async def test_callback_brush_blocked_during_manual_override(self):
        offer_id = self._offer_id()
        update_pass_offer(self.db_path, offer_id, manual_override=True)
        query = await self._callback(action="brush", offer_id=offer_id, user_id=111)
        query.answer.assert_awaited_with(
            "Manual override — take the pass or wait for timeout.",
            show_alert=True,
        )

    async def test_callback_take_dm_forbidden(self):
        offer_id = self._offer_id()
        settings = self._settings()
        user = SimpleNamespace(
            id=111,
            username="fin",
            first_name="Fin",
            last_name="",
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
        context.bot.send_message = AsyncMock(side_effect=Forbidden("blocked"))

        await pass_queue.pass_callback(update, context)

        args, kwargs = query.answer.await_args
        self.assertIn("private chat", args[0].lower())
        self.assertTrue(kwargs.get("show_alert"))
        offer = get_pass_offer(self.db_path, offer_id)
        assert offer is not None
        self.assertEqual(offer.status, "pending")

    async def test_callback_invalid_action_and_id(self):
        settings = self._settings()
        query = MagicMock()
        query.data = "pass:fly:1"
        query.answer = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        update.effective_user = SimpleNamespace(id=1, is_bot=False)

        context = MagicMock()
        context.bot_data = {"settings": settings}

        await pass_queue.pass_callback(update, context)
        query.answer.assert_awaited()

        query.data = "pass:take:notanumber"
        await pass_queue.pass_callback(update, context)
        query.answer.assert_awaited_with("Invalid pass.")


if __name__ == "__main__":
    unittest.main()
