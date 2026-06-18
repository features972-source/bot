"""Registry of bot instances (Q1/Q2) for cross-instance admin commands."""

from __future__ import annotations

from telegram import Bot

from config import Settings

_REGISTRY: dict[str, Settings] = {}
_BOTS: dict[str, Bot] = {}


def register_instance(instance_id: str, settings: Settings) -> None:
    _REGISTRY[instance_id] = settings


def register_bot(instance_id: str, bot: Bot) -> None:
    _BOTS[instance_id] = bot


def get_instance(instance_id: str) -> Settings | None:
    return _REGISTRY.get(instance_id)


def list_instances() -> list[tuple[str, Settings]]:
    return list(_REGISTRY.items())


def list_bots() -> list[tuple[str, Bot, Settings]]:
    rows: list[tuple[str, Bot, Settings]] = []
    for instance_id, settings in _REGISTRY.items():
        bot = _BOTS.get(instance_id)
        if bot is not None:
            rows.append((instance_id, bot, settings))
    return rows
