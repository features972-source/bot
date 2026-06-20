"""Tests for credo bot license keys and subscription."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("BOT_TOKEN", "0000000000:test-token-for-unit-tests")
os.environ.setdefault("CREDO_ONLY_MODE", "true")

from config import load_settings  # noqa: E402
from database import (  # noqa: E402
    ADMIN_LICENSE_DAYS,
    add_bot_admin,
    create_admin_license_key,
    extend_credo_subscription,
    get_credo_subscription_active_until,
    init_db,
    is_credo_subscription_active,
    is_delegated_admin_active,
    list_bot_admins,
    redeem_admin_license_key,
)
from handlers.credo_subscription import normalize_license_key  # noqa: E402
from handlers.admin_access import is_bot_admin  # noqa: E402


class CredoSubscriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "test-credo.db")
        init_db(self.db_path)
        self.settings = load_settings()
        object.__setattr__(self.settings, "database_path", self.db_path)
        object.__setattr__(self.settings, "admin_chat_id", 1000)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_genkey_and_addadmin_extends_subscription(self) -> None:
        key = create_admin_license_key(self.db_path, created_by_user_id=1000)
        sub_until, admin_until = redeem_admin_license_key(
            self.db_path,
            key=key,
            redeemed_by_user_id=2000,
            grant_admin=True,
            telegram_username="alice",
            display_name="Alice",
        )
        self.assertIsNotNone(admin_until)
        self.assertTrue(is_credo_subscription_active(self.db_path))
        self.assertTrue(is_bot_admin(self.settings, self.db_path, 2000))
        stored = get_credo_subscription_active_until(self.db_path)
        self.assertIsNotNone(stored)
        self.assertGreater(stored, datetime.now(timezone.utc))
        delta = stored - datetime.now(timezone.utc)
        self.assertGreaterEqual(delta.days, ADMIN_LICENSE_DAYS - 1)

    def test_used_key_rejected(self) -> None:
        key = create_admin_license_key(self.db_path, created_by_user_id=1000)
        redeem_admin_license_key(
            self.db_path,
            key=key,
            redeemed_by_user_id=2000,
            grant_admin=True,
        )
        with self.assertRaises(ValueError):
            redeem_admin_license_key(
                self.db_path,
                key=key,
                redeemed_by_user_id=3000,
                grant_admin=True,
            )

    def test_expired_admin_loses_access(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        add_bot_admin(
            self.db_path,
            telegram_user_id=2000,
            telegram_username="bob",
            display_name="Bob",
            expires_at=past,
        )
        admin = list_bot_admins(self.db_path)[0]
        self.assertFalse(is_delegated_admin_active(admin))
        self.assertFalse(is_bot_admin(self.settings, self.db_path, 2000))

    def test_redeem_without_admin_only_extends_bot(self) -> None:
        key = create_admin_license_key(self.db_path, created_by_user_id=1000)
        redeem_admin_license_key(
            self.db_path,
            key=key,
            redeemed_by_user_id=1000,
            grant_admin=False,
        )
        self.assertTrue(is_credo_subscription_active(self.db_path))
        self.assertFalse(list_bot_admins(self.db_path))

    def test_active_subscription_allows_anyone(self) -> None:
        from handlers.credo import is_credo_allowed

        extend_credo_subscription(self.db_path)
        self.assertTrue(is_credo_allowed(self.settings, self.db_path, 999999))

    def test_trailing_hyphen_normalized(self) -> None:
        key = create_admin_license_key(self.db_path, created_by_user_id=1000)
        with_hyphen = key + "-"
        redeem_admin_license_key(
            self.db_path,
            key=normalize_license_key(with_hyphen),
            redeemed_by_user_id=2000,
            grant_admin=True,
        )
        self.assertTrue(is_bot_admin(self.settings, self.db_path, 2000))


if __name__ == "__main__":
    unittest.main()
