"""Smoke tests for the credo-only bot wiring."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("BOT_TOKEN", "0000000000:test-token-for-unit-tests")
os.environ.setdefault("CREDO_ONLY_MODE", "true")

from config import load_settings  # noqa: E402
from handlers.bot_commands import build_credo_bot_handlers  # noqa: E402
from handlers.credo import (  # noqa: E402
    CREDO_ONLY_ACTIVE_ALLOWED_COMMANDS,
    build_credo_handlers,
    is_credo_active_command_allowed,
)


class CredoOnlyBotTests(unittest.TestCase):
    def test_settings_credo_only_mode(self) -> None:
        settings = load_settings()
        self.assertTrue(settings.credo_only_mode)

    def test_build_credo_only_handlers(self) -> None:
        handlers = build_credo_handlers(credo_only=True)
        self.assertGreater(len(handlers), 0)
        bot_handlers = build_credo_bot_handlers()
        self.assertGreater(len(bot_handlers), len(handlers))

    def test_active_session_allows_cc_not_payments(self) -> None:
        settings = load_settings()
        self.assertTrue(is_credo_active_command_allowed("/cc", settings))
        self.assertTrue(is_credo_active_command_allowed("/finished", settings))
        self.assertFalse(is_credo_active_command_allowed("/payments", settings))
        self.assertFalse(is_credo_active_command_allowed("/mail", settings))
        self.assertIn("cc", CREDO_ONLY_ACTIVE_ALLOWED_COMMANDS)


if __name__ == "__main__":
    unittest.main()
