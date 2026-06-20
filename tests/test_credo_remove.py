"""Tests for credo card removal name resolution."""

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

from database import init_db, upsert_credo_credit_card  # noqa: E402
from handlers.credo import (  # noqa: E402
    _parse_setlimit_from_message,
    _resolve_credo_cards_for_removal,
)


class CredoRemoveResolveTests(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(
            tempfile.gettempdir(), f"credo_remove_{uuid.uuid4().hex}.db"
        )
        init_db(self.db_path)
        upsert_credo_credit_card(
            self.db_path,
            name="Lloyds",
            photo_file_id="photo1",
            capacity=6209,
        )
        upsert_credo_credit_card(
            self.db_path,
            name="Lloyds #2",
            photo_file_id="photo2",
            capacity=5000,
        )

    def test_remove_by_display_label(self):
        names, hint = _resolve_credo_cards_for_removal(self.db_path, "Lloyds #1")
        self.assertIsNone(hint)
        self.assertEqual(names, ["Lloyds"])

    def test_remove_second_by_display_label(self):
        names, hint = _resolve_credo_cards_for_removal(self.db_path, "Lloyds #2")
        self.assertIsNone(hint)
        self.assertEqual(names, ["Lloyds #2"])

    def test_lloyds_hash_two_picks_second_card_in_group(self):
        """Lloyds + Lloyds #2 in DB — #2 must be the second card, not the first."""
        names, hint = _resolve_credo_cards_for_removal(self.db_path, "Lloyds #2")
        self.assertIsNone(hint)
        self.assertEqual(names, ["Lloyds #2"])

    def test_setlimit_message_parser_keeps_hash(self):
        parsed = _parse_setlimit_from_message("/setlimit Lloyds #2 10000")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        card_query, amount = parsed
        self.assertEqual(card_query, "Lloyds #2")
        self.assertEqual(amount, 10000.0)

    def test_ambiguous_base_name(self):
        names, hint = _resolve_credo_cards_for_removal(self.db_path, "Lloyds")
        self.assertEqual(names, [])
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertIn("Lloyds #1", hint)
        self.assertIn("Lloyds #2", hint)

    def test_ambiguous_when_db_names_include_hash(self):
        db_path = os.path.join(
            tempfile.gettempdir(), f"credo_remove_{uuid.uuid4().hex}.db"
        )
        init_db(db_path)
        upsert_credo_credit_card(db_path, name="Lloyds #1", photo_file_id="p1", capacity=6209)
        upsert_credo_credit_card(db_path, name="Lloyds #2", photo_file_id="p2", capacity=5000)
        names, hint = _resolve_credo_cards_for_removal(db_path, "Lloyds")
        self.assertEqual(names, [])
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertIn("Lloyds #1", hint)
        self.assertIn("Lloyds #2", hint)


if __name__ == "__main__":
    unittest.main()
