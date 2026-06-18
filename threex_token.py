"""Shared 3CX OAuth token for Call Control, XAPI, and transcripts."""

from __future__ import annotations

import asyncio
import logging

import httpx

from config import Settings

logger = logging.getLogger(__name__)

THREECX_TOKENS_KEY = "threex_tokens"


class TokenHolder:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token_url = f"https://{settings.threex_fqdn}/connect/token"
        self._token: str | None = None
        self._lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()

    async def get(self) -> str | None:
        async with self._lock:
            if self._token is None:
                await self._refresh_unlocked()
            return self._token

    async def refresh(self) -> str | None:
        async with self._refresh_lock:
            async with self._lock:
                return await self._refresh_unlocked()

    async def _refresh_unlocked(self) -> str | None:
        self._token = await fetch_token(self._settings)
        return self._token


async def fetch_token(settings: Settings) -> str | None:
    url = f"https://{settings.threex_fqdn}/connect/token"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                url,
                data={
                    "client_id": settings.threex_client_id,
                    "client_secret": settings.threex_api_key,
                    "grant_type": "client_credentials",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code >= 400:
                detail = (response.text or "").strip()[:200]
                hint = ""
                if response.status_code in {401, 403}:
                    hint = (
                        " — check THREECX_CLIENT_ID matches this API key "
                        "(3CX Admin → Integrations → API)"
                    )
                logger.warning(
                    "3CX token failed (%s) fqdn=%s client_id=%s: %s%s",
                    response.status_code,
                    settings.threex_fqdn,
                    settings.threex_client_id,
                    detail or "(empty response)",
                    hint,
                )
                return None
            token = response.json().get("access_token")
            if not token:
                logger.error("3CX token response missing access_token")
                return None
            return token
    except Exception:
        logger.exception("Failed to get 3CX access token")
        return None


def get_token_holder(bot_data: dict, settings: Settings) -> TokenHolder:
    holder = bot_data.get(THREECX_TOKENS_KEY)
    if isinstance(holder, TokenHolder):
        return holder
    holder = TokenHolder(settings)
    bot_data[THREECX_TOKENS_KEY] = holder
    return holder
