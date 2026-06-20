"""Tests for /export profit summary."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ALLOW_LOCAL_RUN", "true")
os.environ.setdefault("BOT_TOKEN", "0000000000:TEST_TOKEN_NOT_REAL")
os.environ.setdefault("CLOUD_DEPLOYED", "true")
os.environ.setdefault("BOT_INSTANCE_ID", "q2")

from config import load_settings  # noqa: E402
from database import init_db, record_expense, record_payment_out  # noqa: E402
from handlers.profit_export import build_profit_export_summary  # noqa: E402
from money_format import init_currency  # noqa: E402


class ProfitExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_currency("£")
        cls.db_path = os.path.join(
            tempfile.gettempdir(), f"profit_export_{uuid.uuid4().hex}.db"
        )
        os.environ["DATABASE_PATH"] = cls.db_path
        cls.settings = load_settings()
        init_db(cls.db_path)
        now = datetime.now(timezone.utc).isoformat()
        record_payment_out(
            cls.db_path,
            telegram_user_id=1,
            telegram_username="closer",
            display_name="Closer",
            amount=1000.0,
            raw_text="1000 out",
            chat_id=-100,
            starter_user_id=2,
            starter_username="opener",
            starter_display_name="Opener",
        )
        record_expense(
            cls.db_path,
            telegram_user_id=2,
            telegram_username="opener",
            display_name="Opener",
            amount=50.0,
            raw_text="£50 test",
            reason="Tools",
            chat_id=-100,
            created_at=now,
        )

    def test_summary_totals(self):
        summary = build_profit_export_summary(
            self.settings, since=None, period_label="all time"
        )
        self.assertEqual(summary.gross, 1000.0)
        self.assertEqual(summary.starter_pay, 50.0)
        self.assertEqual(summary.finisher_pay, 150.0)
        self.assertEqual(summary.centre_pay, 200.0)
        self.assertEqual(summary.expense_total, 50.0)
        self.assertEqual(summary.net_profit, 150.0)
        self.assertEqual(len(summary.expense_by_user), 1)
        self.assertEqual(summary.expense_by_user[0].total_amount, 50.0)
        self.assertEqual(len(summary.payout_by_user), 2)
        by_id = {entry.user_id: entry for entry in summary.payout_by_user}
        self.assertEqual(by_id[2].starter_amount, 50.0)
        self.assertEqual(by_id[2].starter_count, 1)
        self.assertEqual(by_id[2].total_owed, 50.0)
        self.assertEqual(by_id[1].finisher_amount, 150.0)
        self.assertEqual(by_id[1].finisher_count, 1)
        self.assertEqual(by_id[1].total_owed, 150.0)
        self.assertEqual(summary.total_owed_to_staff, 200.0)


if __name__ == "__main__":
    unittest.main()
