"""Push q1.xlsx to personal OneDrive for Excel on the web."""

from __future__ import annotations

import base64
import logging
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx

from config import Settings
from database import get_excel_web_url, get_ms_graph_refresh_token, set_excel_web_url, set_ms_graph_refresh_token

logger = logging.getLogger(__name__)

GRAPH_SCOPE = "Files.ReadWrite offline_access"
GRAPH_AUTHORITY = "https://login.microsoftonline.com/common"
REDIRECT_URI = "http://localhost:53682/"
REDIRECT_PORT = 53682
ONEDRIVE_REMOTE_PATH = "q1.xlsx"
CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

EXCEL_WEB_SETUP_HELP = (
    "Excel on the web needs a free Microsoft app (one-time setup):\n\n"
    "1. portal.azure.com → App registrations → New registration\n"
    "2. Name: Q1 Excel · Accounts: Personal Microsoft accounts only\n"
    "3. Redirect URI: Web → http://localhost:53682/\n"
    "4. Certificates & secrets → New client secret → copy the Value\n"
    "5. API permissions → Microsoft Graph → Delegated → Files.ReadWrite\n"
    "6. Add to .env:\n"
    "   MS_GRAPH_CLIENT_ID=your-app-id\n"
    "   MS_GRAPH_CLIENT_SECRET=your-secret\n"
    "7. Restart the bot, then run /excelwebauth again"
)


def graph_app_configured(settings: Settings) -> bool:
    return bool(settings.ms_graph_client_id and settings.ms_graph_client_secret)


def graph_configured(settings: Settings) -> bool:
    return graph_app_configured(settings) and bool(
        get_ms_graph_refresh_token(settings.database_path)
    )


def encode_sharing_url(share_url: str) -> str:
    encoded = base64.b64encode(share_url.encode("utf-8")).decode("ascii")
    encoded = encoded.rstrip("=").replace("/", "_").replace("+", "-")
    return f"u!{encoded}"


def is_onedrive_share_url(url: str) -> bool:
    lowered = url.lower()
    return "1drv.ms" in lowered or "onedrive.live.com" in lowered


def _token_endpoint() -> str:
    return f"{GRAPH_AUTHORITY}/oauth2/v2.0/token"


def _auth_endpoint() -> str:
    return f"{GRAPH_AUTHORITY}/oauth2/v2.0/authorize"


def _token_request_data(settings: Settings, **extra: str) -> dict[str, str]:
    data = {
        "client_id": settings.ms_graph_client_id or "",
        "scope": GRAPH_SCOPE,
        **extra,
    }
    if settings.ms_graph_client_secret:
        data["client_secret"] = settings.ms_graph_client_secret
    return data


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    auth_code: str | None = None
    auth_error: str | None = None

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        if "code" in query:
            _OAuthCallbackHandler.auth_code = query["code"][0]
            body = (
                b"<html><body><h2>Sign-in OK</h2>"
                b"<p>You can close this tab and return to Telegram.</p></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        _OAuthCallbackHandler.auth_error = query.get("error_description", ["cancelled"])[0]
        self.send_response(400)
        self.end_headers()
        self.wfile.write(b"Sign-in failed. Return to Telegram and try again.")

    def log_message(self, format: str, *args: Any) -> None:
        return


def browser_oauth_flow(settings: Settings) -> dict[str, Any] | None:
    """Open the system browser and capture the OAuth code on localhost."""
    if not graph_app_configured(settings):
        return None

    _OAuthCallbackHandler.auth_code = None
    _OAuthCallbackHandler.auth_error = None
    state = secrets.token_urlsafe(16)
    auth_url = (
        f"{_auth_endpoint()}?client_id={quote(settings.ms_graph_client_id or '')}"
        f"&response_type=code&redirect_uri={quote(REDIRECT_URI)}"
        f"&scope={quote(GRAPH_SCOPE)}&response_mode=query&state={quote(state)}"
    )

    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _OAuthCallbackHandler)
    server.timeout = 300
    webbrowser.open(auth_url)
    server.handle_request()

    if _OAuthCallbackHandler.auth_error:
        logger.warning("Browser OAuth failed: %s", _OAuthCallbackHandler.auth_error)
        return None
    if not _OAuthCallbackHandler.auth_code:
        return None

    response = httpx.post(
        _token_endpoint(),
        data=_token_request_data(
            settings,
            grant_type="authorization_code",
            code=_OAuthCallbackHandler.auth_code,
            redirect_uri=REDIRECT_URI,
        ),
        timeout=30,
    )
    if response.status_code >= 400:
        logger.warning("OAuth code exchange failed: %s", response.text[:300])
        return None
    return response.json()


def refresh_access_token(settings: Settings) -> str | None:
    refresh_token = get_ms_graph_refresh_token(settings.database_path)
    if not refresh_token or not graph_app_configured(settings):
        return None

    response = httpx.post(
        _token_endpoint(),
        data=_token_request_data(
            settings,
            refresh_token=refresh_token,
            grant_type="refresh_token",
        ),
        timeout=30,
    )
    if response.status_code >= 400:
        logger.warning("Graph token refresh failed: %s", response.text[:300])
        return None

    data = response.json()
    new_refresh = data.get("refresh_token")
    if new_refresh:
        set_ms_graph_refresh_token(settings.database_path, new_refresh)
    return data.get("access_token")


def _graph_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": CONTENT_TYPE,
    }


def _upload_to_url(token: str, url: str, content: bytes) -> dict[str, Any] | None:
    response = httpx.put(
        url,
        headers=_graph_headers(token),
        content=content,
        timeout=120,
    )
    if response.status_code >= 400:
        logger.warning(
            "OneDrive upload failed (%s): %s",
            response.status_code,
            response.text[:300],
        )
        return None
    try:
        return response.json()
    except ValueError:
        return {}


def _upload_via_share_link(
    settings: Settings,
    token: str,
    content: bytes,
    share_url: str,
) -> str | None:
    share_token = encode_sharing_url(share_url)

    item = httpx.get(
        f"https://graph.microsoft.com/v1.0/shares/{share_token}/driveItem",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if item.status_code >= 400:
        logger.warning(
            "Could not resolve share link (%s): %s",
            item.status_code,
            item.text[:300],
        )
        return None

    item_data = item.json()
    item_id = item_data.get("id")
    drive_id = (item_data.get("parentReference") or {}).get("driveId")
    if not item_id or not drive_id:
        return None

    upload_url = (
        f"https://graph.microsoft.com/v1.0/drives/{drive_id}"
        f"/items/{item_id}/content"
    )
    result = _upload_to_url(token, upload_url, content)
    if result is None:
        return None

    web_url = result.get("webUrl") or item_data.get("webUrl") or share_url
    set_excel_web_url(settings.database_path, web_url)
    logger.info("Uploaded workbook via share link to Excel on the web")
    return web_url


def upload_workbook_to_onedrive(
    settings: Settings,
    content: bytes,
    *,
    remote_path: str = ONEDRIVE_REMOTE_PATH,
) -> str | None:
    """Upload workbook bytes to OneDrive. Returns the file web URL if successful."""
    token = refresh_access_token(settings)
    if not token:
        return None

    share_url = resolve_excel_web_url(settings)
    if share_url and is_onedrive_share_url(share_url):
        uploaded = _upload_via_share_link(settings, token, content, share_url)
        if uploaded:
            return uploaded

    upload_url = (
        "https://graph.microsoft.com/v1.0/me/drive/root:"
        f"/{remote_path}:/content"
    )
    result = _upload_to_url(token, upload_url, content)
    if result is None:
        return None

    web_url = result.get("webUrl") or share_url
    if web_url:
        set_excel_web_url(settings.database_path, web_url)
        logger.info("Uploaded %s to OneDrive for Excel on the web", remote_path)
    return web_url


def resolve_excel_web_url(settings: Settings) -> str | None:
    if settings.excel_web_url:
        return settings.excel_web_url
    return get_excel_web_url(settings.database_path)


def remember_excel_web_url(settings: Settings) -> None:
    """Persist EXCEL_WEB_URL from settings into the database."""
    if settings.excel_web_url:
        set_excel_web_url(settings.database_path, settings.excel_web_url)
