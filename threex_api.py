"""3CX Call Control API helpers for admin commands."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from config import Settings
from database import ExtensionLink, get_link_by_extension

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = frozenset(
    {
        "connected",
        "talking",
        "answered",
        "active",
        "oncall",
        "on-call",
        "ringing",
    }
)

CONNECTED_STATUSES = frozenset(
    {
        "connected",
        "talking",
        "answered",
        "active",
        "oncall",
        "on-call",
    }
)

RINGING_STATUSES = frozenset(
    {"ringing", "dialing", "trying", "alerting", "progress"}
)


class ThreeCXApiError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ActiveCall:
    extension: str
    participant_id: int
    status: str
    callid: str
    caller_id: str
    caller_name: str
    link: ExtensionLink | None


class ThreeCXClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token: str | None = None

    @property
    def _fqdn(self) -> str:
        return self._settings.threex_fqdn

    @property
    def _token_url(self) -> str:
        return f"https://{self._fqdn}/connect/token"

    async def _fetch_token(self) -> str:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                self._token_url,
                data={
                    "client_id": self._settings.threex_client_id,
                    "client_secret": self._settings.threex_api_key,
                    "grant_type": "client_credentials",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code >= 400:
                raise ThreeCXApiError(
                    f"Token request failed ({response.status_code}): {response.text[:200]}",
                    status_code=response.status_code,
                )
            token = response.json().get("access_token")
            if not token:
                raise ThreeCXApiError("Token response missing access_token")
            return token

    async def _get_token(self) -> str:
        if self._token is None:
            self._token = await self._fetch_token()
        return self._token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"https://{self._fqdn}{path}"
        for attempt in range(2):
            token = await self._get_token()
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(
                    method,
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json=json_body,
                )
                if response.status_code == 401 and attempt == 0:
                    self._token = None
                    continue
                if response.status_code >= 400:
                    detail = response.text[:300]
                    raise ThreeCXApiError(
                        f"API error ({response.status_code}): {detail}",
                        status_code=response.status_code,
                    )
                if not response.content:
                    return None
                try:
                    return response.json()
                except ValueError:
                    return response.text

    async def list_participants(self, extension: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/callcontrol/{extension}/participants")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def get_participant(
        self, extension: str, participant_id: int
    ) -> dict[str, Any] | None:
        data = await self._request(
            "GET", f"/callcontrol/{extension}/participants/{participant_id}"
        )
        return data if isinstance(data, dict) else None

    async def participant_action(
        self,
        extension: str,
        participant_id: int,
        action: str,
        *,
        destination: str | None = None,
        timeout: int = 30,
        reason: str = "None",
    ) -> Any:
        body: dict[str, Any] | None = None
        if action in {"transferto", "routeto", "divert"}:
            if not destination:
                raise ThreeCXApiError(f"{action} requires a destination extension")
            body = {
                "reason": reason,
                "destination": destination,
                "timeout": timeout,
            }
        return await self._request(
            "POST",
            f"/callcontrol/{extension}/participants/{participant_id}/{action}",
            json_body=body,
        )

    async def drop_participant(self, extension: str, participant_id: int) -> Any:
        return await self.participant_action(extension, participant_id, "drop")

    async def transfer_participant(
        self, extension: str, participant_id: int, destination: str
    ) -> Any:
        return await self.participant_action(
            extension,
            participant_id,
            "transferto",
            destination=destination,
        )

    async def listen_participant(
        self, extension: str, participant_id: int, admin_extension: str
    ) -> Any:
        return await self.participant_action(
            extension,
            participant_id,
            "routeto",
            destination=admin_extension,
        )


def get_client(bot_data: dict, settings: Settings) -> ThreeCXClient:
    key = "threex_client"
    client = bot_data.get(key)
    if client is None:
        client = ThreeCXClient(settings)
        bot_data[key] = client
    return client


def _participant_status(participant: dict[str, Any]) -> str:
    return str(participant.get("status") or "").strip()


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "none":
        return ""
    return text


def participant_callid(participant: dict[str, Any]) -> int | None:
    raw = participant.get("callid")
    if raw in (None, "", 0, "0"):
        return None
    try:
        callid = int(raw)
    except (TypeError, ValueError):
        return None
    return callid if callid > 0 else None


def is_active_participant(participant: dict[str, Any]) -> bool:
    status = _participant_status(participant).lower()
    if status in RINGING_STATUSES:
        return False
    return status in ACTIVE_STATUSES


def is_connected_participant(participant: dict[str, Any]) -> bool:
    status = _participant_status(participant).lower()
    if status in RINGING_STATUSES:
        return False
    return status in CONNECTED_STATUSES


def is_real_connected_participant(
    participant: dict[str, Any],
    *,
    extension: str = "",
) -> bool:
    """Connected participant that looks like an actual external call, not a phantom leg."""
    if not is_connected_participant(participant):
        return False
    if participant_callid(participant) is None:
        return False

    ext = _normalize_text(extension)
    part_dn = _normalize_text(participant.get("dn"))
    if ext and part_dn and part_dn != ext:
        return False

    if participant.get("direct_control") is True:
        return False

    party_dn_type = _normalize_text(participant.get("party_dn_type")).lower()
    if party_dn_type != "wexternalline":
        return False

    party_dn = _normalize_text(participant.get("party_dn"))
    if ext and party_dn == ext:
        return False

    return True


def find_connected_participant(
    participants: list[dict[str, Any]],
    *,
    extension: str = "",
) -> dict[str, Any] | None:
    for participant in participants:
        if is_real_connected_participant(participant, extension=extension):
            return participant
    return None


def filter_real_connected_participants(
    participants: list[dict[str, Any]],
    *,
    extension: str = "",
) -> list[dict[str, Any]]:
    return [
        participant
        for participant in participants
        if is_real_connected_participant(participant, extension=extension)
    ]


def extract_participant_bodies_from_ws(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract participant dict(s) from a Call Control WS event payload."""
    event = payload.get("event") or {}
    attached = event.get("attached_data") or event.get("AttachedData")
    if attached is None:
        return []

    candidates: list[dict[str, Any]] = []
    if isinstance(attached, dict):
        body = (
            attached.get("body")
            or attached.get("Body")
            or attached.get("data")
            or attached.get("Data")
        )
        if isinstance(body, dict):
            candidates.append(body)
        elif isinstance(body, list):
            candidates.extend(item for item in body if isinstance(item, dict))
        elif attached.get("id") is not None:
            candidates.append(attached)
    elif isinstance(attached, list):
        candidates.extend(item for item in attached if isinstance(item, dict))
    return candidates


def is_inbound_ringing_participant(
    participant: dict[str, Any],
    *,
    extension: str = "",
) -> bool:
    """Inbound call ringing an extension (not outbound dial in progress)."""
    if not is_external_caller_participant(participant, extension=extension):
        return False
    status = _participant_status(participant).lower()
    if status not in RINGING_STATUSES:
        return False
    ext = _normalize_text(extension)
    originated = _normalize_text(participant.get("originated_by_dn"))
    if ext and originated == ext:
        return False
    return True


def is_external_caller_participant(
    participant: dict[str, Any],
    *,
    extension: str = "",
) -> bool:
    """External caller leg (same filters as is_real_connected, without status check)."""
    if participant_callid(participant) is None:
        return False

    ext = _normalize_text(extension)
    part_dn = _normalize_text(participant.get("dn"))
    if ext and part_dn and part_dn != ext:
        return False

    if participant.get("direct_control") is True:
        return False

    party_dn_type = _normalize_text(participant.get("party_dn_type")).lower()
    if party_dn_type != "wexternalline":
        return False

    party_dn = _normalize_text(participant.get("party_dn"))
    if ext and party_dn == ext:
        return False

    return True


def is_agent_leg_participant(
    participant: dict[str, Any],
    *,
    extension: str = "",
) -> bool:
    """Agent-owned leg: direct_control, own extension, or non-external party type."""
    if participant.get("direct_control") is True:
        return True

    ext = _normalize_text(extension)
    party_dn = _normalize_text(participant.get("party_dn"))
    if ext and party_dn == ext:
        return True

    party_dn_type = _normalize_text(participant.get("party_dn_type")).lower()
    if party_dn_type and party_dn_type != "wexternalline":
        return True

    return False


def participant_from_ws_event(
    payload: dict[str, Any],
    *,
    extension: str,
) -> dict[str, Any] | None:
    """Extract a connected external participant from a Call Control WS event payload."""
    for participant in extract_participant_bodies_from_ws(payload):
        if is_real_connected_participant(participant, extension=extension):
            return participant
    return None


def find_active_participant(
    participants: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for participant in participants:
        if is_active_participant(participant):
            return participant
    return None


def _participant_id(participant: dict[str, Any]) -> int | None:
    raw = participant.get("id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _link_label(link: ExtensionLink | None) -> str:
    if link is None:
        return "—"
    if link.telegram_username:
        return f"@{link.telegram_username}"
    if link.display_name:
        return link.display_name
    return str(link.telegram_user_id)


async def list_all_active_calls(
    client: ThreeCXClient,
    settings: Settings,
    extensions: list[str],
) -> list[ActiveCall]:
    active_calls: list[ActiveCall] = []
    for extension in extensions:
        try:
            participants = await client.list_participants(extension)
        except ThreeCXApiError as exc:
            logger.warning("Could not list participants for ext %s: %s", extension, exc)
            continue

        link = get_link_by_extension(settings.database_path, extension)
        for participant in participants:
            if not is_real_connected_participant(participant, extension=extension):
                continue
            participant_id = _participant_id(participant)
            if participant_id is None:
                continue
            active_calls.append(
                ActiveCall(
                    extension=extension,
                    participant_id=participant_id,
                    status=_participant_status(participant),
                    callid=str(participant.get("callid") or ""),
                    caller_id=str(participant.get("party_caller_id") or ""),
                    caller_name=str(participant.get("party_caller_name") or ""),
                    link=link,
                )
            )
    return active_calls


def format_active_calls(active_calls: list[ActiveCall]) -> str:
    if not active_calls:
        return "📭 No active calls on linked extensions."

    lines = [f"📋 Active calls ({len(active_calls)}):"]
    for call in active_calls:
        caller = call.caller_name or call.caller_id or "Unknown"
        if call.caller_name and call.caller_id:
            caller = f"{call.caller_name} ({call.caller_id})"
        lines.append(
            f"📞 ext {call.extension} ({_link_label(call.link)})\n"
            f"  📊 status: {call.status} | 🆔 callid: {call.callid or '?'}\n"
            f"  👤 caller: {caller}\n"
            f"  🔢 participant: {call.participant_id}"
        )
    return "\n\n".join(lines)


def admin_extension(settings: Settings, override: str | None = None) -> str:
    if override:
        return override.strip()
    if settings.threex_admin_ext:
        return settings.threex_admin_ext
    return settings.threex_client_id
