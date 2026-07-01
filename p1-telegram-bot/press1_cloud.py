"""Press-1 bot entry point for Render (health check + Telegram polling)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading

from dotenv import load_dotenv
from flask import Flask, jsonify

load_dotenv()

from press1_bot import build_application

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "id": "p1", "bot": "P1 Press-1 Dialer"})

    @app.get("/")
    def root():
        return jsonify({"ok": True, "service": "p1-telegram-bot", "build": "8d4cbca-sftp"})

    from werkzeug.serving import make_server

    httpd = make_server("0.0.0.0", port, app, threaded=True)
    logger.info("Health server on port %s", port)
    httpd.serve_forever()


def main() -> None:
    if sys.version_info >= (3, 10):
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    threading.Thread(target=start_health_server, daemon=True).start()

    app = build_application()
    logger.info("Press-1 VICIdial bot polling…")
    app.run_polling(
        allowed_updates=["message"],
        drop_pending_updates=True,
        poll_interval=1.0,
        timeout=30,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
