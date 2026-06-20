"""Tests for /setcredolimit admin command."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ALLOW_LOCAL_RUN", "true")
os.environ.setdefault("BOT_TOKEN", "0000000000:TEST")
os.environ.setdefault("CLOUD_DEPLOYED", "true")

from database import (  # noqa: E402
    init_db,
    record_credo_card_usage,
    sum_credo_card_usage,
    upsert_credo_credit_card,
)
from handlers.credo import _card_balance  # noqa: E402
from config import load_settings  # noqa: E402


class SetCredoLimitTests(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(
            tempfile.gettempdir(), f"set_limit_{uuid.uuid4().hex}.db"
        )
        os.environ["DATABASE_PATH"] = self.db_path
        init_db(self.db_path)
        upsert_credo_credit_card(
            self.db_path,
            name="Lloyds",
            photo_file_id="p1",
            capacity=6209,
            card_last4="1234",
        )
        record_credo_card_usage(
            self.db_path,
            card_name="Lloyds",
            telegram_user_id=1,
            telegram_username="u",
            display_name="U",
            amount=6209,
        )
        self.settings = load_settings()

    def test_balance_shows_depleted_before_reset(self):
        _, capacity, remaining = _card_balance(self.settings, "Lloyds")
        self.assertEqual(capacity, 6209)
        self.assertEqual(remaining, 0)

    def test_clear_and_set_capacity(self):
        from database import clear_credo_card_usage, update_credo_credit_card_capacity

        clear_credo_card_usage(self.db_path, "Lloyds")
        update_credo_credit_card_capacity(self.db_path, "Lloyds", 5000)
        self.assertEqual(sum_credo_card_usage(self.db_path, "Lloyds"), 0)
        _, capacity, remaining = _card_balance(self.settings, "Lloyds")
        self.assertEqual(capacity, 5000)
        self.assertEqual(remaining, 5000)


if __name__ == "__main__":
    unittest.main()
