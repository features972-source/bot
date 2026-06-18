import asyncio
import json
import logging
import os
import re
import threading
from typing import Any

from flask import Flask, Response, jsonify, request
from telegram import Bot

from config import Settings
from database import get_link_by_extension
from listen_stream import (
    get_listen_session,
    iter_session_audio,
    listen_page_html,
)
from notify import announce_call_ended, announce_call_started
from threex_api import admin_extension

logger = logging.getLogger(__name__)

ANSWER_EVENTS = {
    "answered",
    "answer",
    "pickup",
    "pickupcall",
    "connected",
    "agent_answer",
    "agentanswer",
    "call_answered",
    "oncall",
}

END_EVENTS = {
    "ended",
    "end",
    "hangup",
    "completed",
    "offcall",
    "off_phone",
    "offphone",
    "call_ended",
}


def start_webhook_server(
    settings: Settings,
    bot: Bot,
    bot_data: dict,
    loop: asyncio.AbstractEventLoop,
) -> threading.Thread:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    @app.get("/listen/<path:session_id>")
    def listen_page(session_id: str):
        session = get_listen_session(bot_data, session_id)
        if session is None:
            return "Listen session not found or expired.", 404
        phone_ext = ""
        if os.getenv("LISTEN_USE_PHONE_FALLBACK", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            phone_ext = admin_extension(settings)
        return listen_page_html(session, phone_listen_ext=phone_ext)

    @app.get("/listen/stream/<path:session_id>")
    def listen_stream(session_id: str):
        session = get_listen_session(bot_data, session_id)
        if session is None:
            return "Not found", 404
        if session.error:

            def error_body():
                yield session.error.encode("utf-8")

            return Response(error_body(), status=502, mimetype="text/plain")

        return Response(
            iter_session_audio(session),
            mimetype="application/octet-stream",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.route("/webhook/3cx/<secret>", methods=["GET", "POST"])
    def webhook(secret: str):
        if secret != settings.webhook_secret:
            return "Forbidden", 403

        payload = _read_payload()
        extension = _extract_extension(payload)
        if not extension:
            logger.warning("Webhook missing extension: %s", payload)
            return jsonify({"ok": False, "error": "extension not found"}), 400

        event_kind = _classify_event(payload)
        if event_kind is None:
            return jsonify({"ok": True, "ignored": True})

        if settings.threex_enabled:
            logger.debug(
                "Ignoring webhook %s for ext %s (Call Control WebSocket handles announcements)",
                event_kind,
                _extract_extension(payload),
            )
            return jsonify({
                "ok": True,
                "ignored": True,
                "reason": "call control websocket active",
            })

        link = get_link_by_extension(settings.database_path, extension)
        if link is None:
            logger.info("No Telegram link for extension %s", extension)
            return jsonify({"ok": True, "ignored": True, "reason": "unlinked extension"})

        notify_chat_id = bot_data.get("notify_chat_id") or settings.notify_chat_id
        if notify_chat_id is None:
            return jsonify({"ok": False, "error": "NOTIFY_CHAT_ID not configured"}), 500

        if event_kind == "answer":
            future = asyncio.run_coroutine_threadsafe(
                announce_call_started(bot, settings, bot_data, link),
                loop,
            )
        else:
            future = asyncio.run_coroutine_threadsafe(
                announce_call_ended(bot, settings, bot_data, link),
                loop,
            )
        try:
            future.result(timeout=15)
        except Exception as exc:
            logger.exception("Failed to send Telegram message")
            return jsonify({"ok": False, "error": str(exc)}), 500

        return jsonify({"ok": True, "announced": extension, "event": event_kind})

    def run_server():
        from werkzeug.serving import make_server

        httpd = make_server(
            settings.webhook_host,
            settings.webhook_port,
            app,
            threaded=True,
        )
        httpd.serve_forever()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    return thread


def _read_payload() -> dict[str, Any]:
    data: dict[str, Any] = dict(request.args)

    if request.is_json:
        parsed = request.get_json(silent=True)
        if isinstance(parsed, dict):
            data.update(_flatten_dict(parsed))

    if request.form:
        data.update(request.form.to_dict())

    if request.data:
        text = request.data.decode("utf-8", errors="replace").strip()
        if text and "raw" not in data:
            if text.startswith("{"):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        data.update(_flatten_dict(parsed))
                except json.JSONDecodeError:
                    data["raw"] = text
            elif "=" in text and not data:
                for part in text.split("&"):
                    if "=" in part:
                        key, value = part.split("=", 1)
                        data[key] = value
            elif "raw" not in data:
                data["raw"] = text

    return data


def _flatten_dict(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, item in value.items():
        full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(item, dict):
            flat.update(_flatten_dict(item, full_key))
        else:
            flat[full_key] = item
            flat[key.lower()] = item
    return flat


def _extract_extension(payload: dict[str, Any]) -> str | None:
    keys = (
        "user",
        "dn",
        "extension",
        "ext",
        "agent",
        "agentextension",
        "extensionnumber",
        "destination",
        "answeredby",
        "agentdn",
    )
    for key in keys:
        for candidate_key, value in payload.items():
            if candidate_key.lower().replace("_", "") == key and value not in (None, ""):
                extension = _normalize_extension(str(value))
                if extension:
                    return extension
    return None


def _normalize_extension(value: str) -> str | None:
    value = value.strip()
    match = re.search(r"\d+", value)
    if not match:
        return None
    return match.group(0)


def _classify_event(payload: dict[str, Any]) -> str | None:
    call_type = ""
    for key, value in payload.items():
        if key.lower() in {"calltype", "call_type", "direction"}:
            call_type = str(value).strip().lower()

    if call_type in {"missed", "notanswered", "not_answered"}:
        return None

    for key, value in payload.items():
        if key.lower() in {"event", "eventtype", "status", "callstate", "state", "finishtype"}:
            normalized = str(value).strip().lower()
            if normalized in ANSWER_EVENTS:
                return "answer"
            if normalized in END_EVENTS:
                return "end"
            if normalized in {"ringing", "missed", "failed"}:
                return None

    source = str(payload.get("source", "")).strip().lower()
    if source == "q1centre":
        return None

    return "answer"
