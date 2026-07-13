"""Press-1 bot entry point for Render (health check + Telegram webhook)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from telegram import Update

load_dotenv()

from press1_bot import TOKEN, build_application

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BUILD = "press1-ready-v25"
WEBHOOK_PATH = os.getenv("TELEGRAM_WEBHOOK_PATH", "telegram/webhook").lstrip("/")
PUBLIC_URL = (
    os.getenv("TELEGRAM_WEBHOOK_URL_BASE")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "https://p1-bot.onrender.com"
).rstrip("/")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()


def resolve_webhook_url() -> str:
    """Public webhook URL for Telegram (never empty on Render)."""
    url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if url:
        return url
    return f"{PUBLIC_URL}/{WEBHOOK_PATH}"


def resolve_webhook_secret() -> str:
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if secret:
        return secret
    if TOKEN and ":" in TOKEN:
        return TOKEN.split(":", 1)[0]
    return ""


def _use_polling() -> bool:
    return os.getenv("P1_USE_POLLING", "").strip().lower() in ("1", "true", "yes")


def _run_polling() -> None:
    app = build_application()
    logger.info("Press-1 bot polling (local/dev only)…")
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
        poll_interval=1.0,
        timeout=30,
        close_loop=False,
    )


def _run_webhook() -> None:
    os.environ["TELEGRAM_WEBHOOK_URL"] = resolve_webhook_url()
    secret = resolve_webhook_secret()
    if secret:
        os.environ["TELEGRAM_WEBHOOK_SECRET"] = secret

    import threading

    loop = asyncio.new_event_loop()

    def _loop_runner() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=_loop_runner, daemon=True, name="tg-event-loop").start()

    tg_app = build_application()

    async def boot() -> None:
        await tg_app.initialize()
        await tg_app.start()
        logger.info("Press-1 bot ready via webhook %s/%s", PUBLIC_URL, WEBHOOK_PATH)

    asyncio.run_coroutine_threadsafe(boot(), loop).result()

    flask_app = Flask(__name__)

    from dash_api import register_dash_routes

    register_dash_routes(flask_app)

    @flask_app.get("/health")
    def health():
        return jsonify({"ok": True, "id": "p1", "bot": "P1 Press-1 Dialer", "build": BUILD})

    @flask_app.get("/")
    def root():
        return jsonify({"ok": True, "service": "p1-telegram-bot", "build": BUILD})

    def _log_update_done(future: asyncio.Future) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("process_update failed")

    @flask_app.post(f"/{WEBHOOK_PATH}")
    def telegram_webhook():
        secret = resolve_webhook_secret()
        if secret:
            header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if header != secret:
                logger.warning("webhook rejected: bad secret token")
                return "", 403
        data = request.get_json(force=True, silent=True)
        if not data:
            return "", 400
        update = Update.de_json(data, tg_app.bot)
        if update is None:
            return "", 400
        # Ack immediately — Telegram times out (~60s) if we wait for handlers.
        future = asyncio.run_coroutine_threadsafe(tg_app.process_update(update), loop)
        future.add_done_callback(_log_update_done)
        return "", 200

    port = int(os.getenv("PORT", "10000"))
    from werkzeug.serving import make_server

    httpd = make_server("0.0.0.0", port, flask_app, threaded=True)
    logger.info("Health + webhook on port %s", port)
    try:
        httpd.serve_forever()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        asyncio.run_coroutine_threadsafe(tg_app.stop(), loop).result(timeout=30)
        asyncio.run_coroutine_threadsafe(tg_app.shutdown(), loop).result(timeout=30)
        loop.call_soon_threadsafe(loop.stop)


def main() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    if _use_polling():
        _run_polling()
    else:
        _run_webhook()


if __name__ == "__main__":
    main()
