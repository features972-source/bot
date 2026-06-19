"""Chat scope helpers (group and private commands both allowed)."""

from __future__ import annotations

from telegram.ext import filters

PM_ONLY = filters.ChatType.PRIVATE
GROUP_ONLY = filters.ChatType.GROUPS
