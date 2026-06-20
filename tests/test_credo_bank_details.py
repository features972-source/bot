"""Tests for credo sort code / account number handling."""

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

from database import get_credo_credit_card, init_db, upsert_credo_credit_card  # noqa: E402
from handlers.credo import (  # noqa: E402
    _format_bank_details_block,
    _format_dm_photo_caption,
    _normalize_account_number,
    _normalize_sort_code,
)


class CredoBankDetailsTests(unittest.TestCase):
    def test_normalize_sort_code(self):
        self.assertEqual(_normalize_sort_code("12-34-56"), "12-34-56")
        self.assertEqual(_normalize_sort_code("123456"), "12-34-56")
        self.assertIsNone(_normalize_sort_code("12345"))

    def test_normalize_account_number(self):
        self.assertEqual(_normalize_account_number("12345678"), "12345678")
        self.assertIsNone(_normalize_account_number("1234567"))

    def test_format_bank_details_block(self):
        block = _format_bank_details_block("12-34-56", "12345678")
        self.assertIn("12-34-56", block)
        self.assertIn("12345678", block)

    def test_dm_caption_includes_bank_details(self):
        caption = _format_dm_photo_caption(
            "Lloyds #1",
            "Don't send more than £500.",
            sort_code="12-34-56",
            account_number="12345678",
        )
        self.assertIn("Sort code", caption)
        self.assertIn("Account number", caption)
        self.assertIn("12-34-56", caption)

    def test_persist_bank_details(self):
        db_path = os.path.join(tempfile.gettempdir(), f"credo_bank_{uuid.uuid4().hex}.db")
        init_db(db_path)
        upsert_credo_credit_card(
            db_path,
            name="Lloyds",
            photo_file_id="photo1",
            capacity=5000,
            card_last4="1234",
            sort_code="12-34-56",
            account_number="12345678",
        )
        card = get_credo_credit_card(db_path, "Lloyds")
        assert card is not None
        self.assertEqual(card.sort_code, "12-34-56")
        self.assertEqual(card.account_number, "12345678")


if __name__ == "__main__":
    unittest.main()
