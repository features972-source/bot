import asyncio
import html
import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request
from telegram import Bot

from config import Settings
from database import get_link_by_extension, get_notify_chat_id, get_payment_totals
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


def start_multi_webhook_server(runtimes, loop: asyncio.AbstractEventLoop) -> threading.Thread:
    """One HTTP server for Q1 + Q2 (health, restore, listen, 3CX webhooks)."""
    primary = runtimes[0].settings
    return _start_webhook_app(
        runtimes=runtimes,
        primary_settings=primary,
        loop=loop,
    )


def start_webhook_server(
    settings: Settings,
    bot: Bot,
    bot_data: dict,
    loop: asyncio.AbstractEventLoop,
) -> threading.Thread:
    from bot_core import BotRuntime

    runtime = BotRuntime(
        instance_id="q1",
        settings=settings,
        application=type("_App", (), {"bot": bot, "bot_data": bot_data})(),
        notify_chat_id=bot_data.get("notify_chat_id"),
    )
    return _start_webhook_app(runtimes=[runtime], primary_settings=settings, loop=loop)


def _start_webhook_app(
    *,
    runtimes,
    primary_settings: Settings,
    loop: asyncio.AbstractEventLoop,
) -> threading.Thread:
    app = Flask(__name__)

    def _runtime_by_id(instance_id: str):
        for runtime in runtimes:
            if runtime.instance_id == instance_id:
                return runtime
        return None

    def _runtime_for_secret(secret: str):
        for runtime in runtimes:
            if runtime.settings.webhook_secret == secret:
                return runtime
        return None

    def _health_payload(runtime) -> dict:
        settings = runtime.settings
        bot_data = runtime.application.bot_data
        notify_id = bot_data.get("notify_chat_id") or settings.notify_chat_id
        if notify_id is None:
            notify_id = get_notify_chat_id(settings.database_path)
        payment_count, _ = get_payment_totals(settings.database_path, since=None)
        return {
            "id": runtime.instance_id,
            "bot": settings.bot_display_name,
            "database_path": settings.database_path,
            "notify_chat_id": notify_id,
            "payments_logged": payment_count,
            "persistent_data": settings.persistent_data,
        }

    @app.get("/health")
    def health():
        instances = [_health_payload(runtime) for runtime in runtimes]
        primary = instances[0]
        return jsonify(
            {
                "ok": True,
                "instances": instances,
                **primary,
            }
        )

    @app.post("/admin/restore-db")
    def restore_db():
        secret = request.args.get("secret", "")
        instance_id = (request.args.get("instance") or "q1").strip().lower()
        runtime = _runtime_by_id(instance_id)
        if runtime is None:
            return jsonify({"ok": False, "error": f"unknown instance {instance_id}"}), 400
        if not secret or secret != runtime.settings.webhook_secret:
            return jsonify({"ok": False, "error": "unauthorized"}), 403
        upload = request.files.get("file")
        if upload is None:
            return jsonify({"ok": False, "error": "missing file field"}), 400
        data = upload.read()
        if len(data) < 16 or not data.startswith(b"SQLite format 3"):
            return jsonify({"ok": False, "error": "not a sqlite database"}), 400
        db_path = Path(runtime.settings.database_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            backup = db_path.with_suffix(f"{db_path.suffix}.bak")
            shutil.copy2(db_path, backup)
        db_path.write_bytes(data)
        logger.info(
            "Restored database for %s to %s (%d bytes)",
            instance_id,
            db_path,
            len(data),
        )
        return jsonify(
            {
                "ok": True,
                "instance": instance_id,
                "path": str(db_path),
                "bytes": len(data),
            }
        )

    @app.post("/admin/restore-session")
    def restore_session():
        secret = request.args.get("secret", "")
        runtime = runtimes[0]
        if not secret or secret != runtime.settings.webhook_secret:
            return jsonify({"ok": False, "error": "unauthorized"}), 403
        upload = request.files.get("file")
        if upload is None:
            return jsonify({"ok": False, "error": "missing file field"}), 400
        data = upload.read()
        if not data:
            return jsonify({"ok": False, "error": "empty file"}), 400
        settings = runtime.settings
        filename = (request.form.get("name") or upload.filename or "mailer-links.session").strip()
        if not filename.endswith(".session"):
            filename = f"{filename}.session"
        if settings.data_dir:
            dest = Path(settings.data_dir) / filename
        else:
            dest = Path(settings.telethon_session_path).with_suffix(".session")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            backup = dest.with_suffix(dest.suffix + ".bak")
            shutil.copy2(dest, backup)
        dest.write_bytes(data)
        logger.info("Restored session to %s (%d bytes)", dest, len(data))
        return jsonify({"ok": True, "path": str(dest), "bytes": len(data)})

    @app.get("/oauth/msgraph/callback")
    def msgraph_oauth_callback():
        from onedrive_cloud_sync import exchange_oauth_code
        from database import set_ms_graph_refresh_token

        runtime = runtimes[0]
        settings = runtime.settings
        bot = runtime.application.bot

        error = request.args.get("error_description") or request.args.get("error")
        if error:
            return (
                "<html><body><h2>Sign-in failed</h2>"
                f"<p>{html.escape(str(error))}</p>"
                "<p>Return to Telegram and run /excelwebauth again.</p>"
                "</body></html>",
                400,
            )

        code = request.args.get("code", "").strip()
        state = request.args.get("state", "").strip()
        if not code:
            return "Missing authorization code.", 400

        pending = runtime.application.bot_data.get("msgraph_oauth_states") or {}
        chat_id = pending.pop(state, None)
        runtime.application.bot_data["msgraph_oauth_states"] = pending
        if chat_id is None:
            return (
                "<html><body><h2>Link expired</h2>"
                "<p>Run /excelwebauth again in Telegram.</p></body></html>",
                400,
            )

        token_data = exchange_oauth_code(settings, code)
        if not token_data or not token_data.get("refresh_token"):
            return (
                "<html><body><h2>Sign-in failed</h2>"
                "<p>Could not exchange the authorization code.</p>"
                "<p>Check Azure redirect URI matches this bot URL, then try again.</p>"
                "</body></html>",
                400,
            )

        set_ms_graph_refresh_token(
            settings.database_path, token_data["refresh_token"]
        )

        async def _notify() -> None:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "Excel on the web is connected.\n\n"
                        "Run /syncpayments — updates push to your OneDrive file. "
                        "Press F5 in the browser tab if it is already open."
                    ),
                )
            except Exception:
                logger.exception("Failed to notify admin after MS Graph OAuth")

        asyncio.run_coroutine_threadsafe(_notify(), loop)
        return (
            "<html><body><h2>Sign-in OK</h2>"
            "<p>Excel on the web is connected. You can close this tab and return to Telegram.</p>"
            "</body></html>"
        )

    def _find_listen_runtime(session_id: str):
        for runtime in runtimes:
            session = get_listen_session(runtime.application.bot_data, session_id)
            if session is not None:
                return runtime, session
        return None, None

    @app.get("/listen/<path:session_id>")
    def listen_page(session_id: str):
        runtime, session = _find_listen_runtime(session_id)
        if session is None or runtime is None:
            return "Listen session not found or expired.", 404
        phone_ext = ""
        if os.getenv("LISTEN_USE_PHONE_FALLBACK", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            phone_ext = admin_extension(runtime.settings)
        return listen_page_html(session, phone_listen_ext=phone_ext)

    @app.get("/listen/stream/<path:session_id>")
    def listen_stream(session_id: str):
        _runtime, session = _find_listen_runtime(session_id)
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
        runtime = _runtime_for_secret(secret)
        if runtime is None:
            return "Forbidden", 403
        settings = runtime.settings
        bot = runtime.application.bot
        bot_data = runtime.application.bot_data

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
            primary_settings.webhook_host,
            primary_settings.webhook_port,
            app,
            threaded=True,
        )
        httpd.serve_forever()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    return thread


# Legacy single-bot helpers removed below — _read_payload etc. unchanged


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
