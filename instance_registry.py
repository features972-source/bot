"""Registry of bot instances (Q1/Q2) for cross-instance admin commands."""

from __future__ import annotations

from config import Settings

_REGISTRY: dict[str, Settings] = {}


def register_instance(instance_id: str, settings: Settings) -> None:
    _REGISTRY[instance_id] = settings


def get_instance(instance_id: str) -> Settings | None:
    return _REGISTRY.get(instance_id)


def list_instances() -> list[tuple[str, Settings]]:
    return list(_REGISTRY.items())
