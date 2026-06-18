"""Live call audio streaming sessions (3CX PCM -> browser)."""

from __future__ import annotations

import html
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Iterator

import httpx

from config import Settings
from threex_ws import request_extension_subscribe_sync

logger = logging.getLogger(__name__)

SESSIONS_KEY = "listen_sessions"
SAMPLE_RATE = 8000


@dataclass
class ListenSession:
    token: str
    public_id: str
    extension: str
    participant_id: int
    caller_name: str
    caller_number: str
    agent_label: str
    created_at: float = field(default_factory=time.time)
    queue: Queue = field(default_factory=Queue)
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    error: str | None = None
    listeners: int = 0

    @property
    def display_title(self) -> str:
        name = self.caller_name.strip()
        number = self.caller_number.strip()
        if name and number and name == number:
            return number
        if name and number:
            return f"{name} · {number}"
        if number:
            return number
        return name or "Unknown caller"


def _slugify(text: str, *, max_len: int = 32) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    slug = slug.strip("-")
    return (slug[:max_len] if slug else "caller")


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def build_public_id(
    *,
    caller_name: str,
    caller_number: str,
    token: str,
) -> str:
    name_part = _slugify(caller_name or "unknown")
    number_part = _digits_only(caller_number)[-15:] or "unknown"
    short_token = token[:10]
    return f"{name_part}-{number_part}-{short_token}"


def listen_public_base(settings: Settings) -> str:
    if settings.listen_public_url:
        return settings.listen_public_url.rstrip("/")
    host = settings.webhook_host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{settings.webhook_port}"


def telegram_listen_ready(settings: Settings) -> bool:
    return settings.listen_public_url.strip().lower().startswith("https://")


def _sessions(bot_data: dict) -> dict[str, ListenSession]:
    return bot_data.setdefault(SESSIONS_KEY, {})


def start_listen_session(
    bot_data: dict,
    settings: Settings,
    *,
    extension: str,
    participant_id: int,
    caller_name: str = "",
    caller_number: str = "",
    agent_label: str = "",
) -> ListenSession:
    token = secrets.token_urlsafe(16)
    public_id = build_public_id(
        caller_name=caller_name,
        caller_number=caller_number,
        token=token,
    )
    session = ListenSession(
        token=token,
        public_id=public_id,
        extension=extension,
        participant_id=participant_id,
        caller_name=caller_name.strip(),
        caller_number=caller_number.strip(),
        agent_label=agent_label.strip() or f"ext {extension}",
    )
    session.thread = threading.Thread(
        target=_pump_3cx_stream,
        args=(bot_data, settings, session),
        name=f"listen-{extension}-{participant_id}",
        daemon=True,
    )
    _sessions(bot_data)[public_id] = session
    session.thread.start()
    return session


def get_listen_session(bot_data: dict, session_id: str) -> ListenSession | None:
    return _sessions(bot_data).get(session_id)


def stop_listen_session(bot_data: dict, session_id: str) -> None:
    session = _sessions(bot_data).pop(session_id, None)
    if session is None:
        return
    session.stop_event.set()
    session.queue.put(None)


def listen_page_html(session: ListenSession, *, phone_listen_ext: str = "") -> str:
    title = html.escape(session.display_title)
    agent = html.escape(session.agent_label)
    ext = html.escape(session.extension)
    phone_ext = html.escape(phone_listen_ext.strip())
    initial_error = html.escape(session.error or "")
    phone_hint = ""
    if phone_ext:
        phone_hint = f"""
  <div style="background:#e8f4fd;border-radius:0.75rem;padding:0.85rem 1rem;margin:1rem 0;line-height:1.45;">
    <strong>📞 Best option:</strong> Close this and tap <b>Listen on phone — ext {phone_ext}</b>
    in Telegram, then answer ext {phone_ext} on your 3CX app.
  </div>"""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 32rem; margin: 2rem auto; padding: 0 1rem; }}
    body.tg {{ margin: 1rem auto; padding: 0 0.75rem; background: var(--tg-theme-bg-color, #fff); color: var(--tg-theme-text-color, #111); }}
    h1 {{ font-size: 1.35rem; margin-bottom: 0.25rem; }}
    .sub {{ color: #555; margin-top: 0; }}
    body.tg .sub {{ color: var(--tg-theme-hint-color, #555); }}
    .live {{ color: #c00; font-weight: bold; }}
    .err {{ color: #b00020; line-height: 1.45; }}
    button {{ font-size: 1rem; padding: 0.85rem 1.2rem; margin-top: 1rem; width: 100%; border: 0; border-radius: 0.75rem; background: #2481cc; color: #fff; cursor: pointer; }}
    body.tg button {{ background: var(--tg-theme-button-color, #2481cc); color: var(--tg-theme-button-text-color, #fff); }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <p class="sub">Agent: <strong>{agent}</strong> · ext {ext}</p>
  {phone_hint}
  <p id="status">Tap the button below to start listening.</p>
  <button id="start">▶ Tap to listen live</button>
  <script>
    const sessionId = {session.public_id!r};
    const sampleRate = {SAMPLE_RATE};
    const initialError = {initial_error!r};
    const tg = window.Telegram && window.Telegram.WebApp;
    if (tg) {{
      tg.ready();
      tg.expand();
      document.body.classList.add('tg');
      if (tg.MainButton) {{
        tg.MainButton.setText('▶ Tap to listen live');
        tg.MainButton.show();
        tg.MainButton.onClick(startAudio);
      }}
    }}
    let ctx = null;
    let nextTime = 0;
    let started = false;

    function showError(msg) {{
      const el = document.getElementById('status');
      el.className = 'err';
      el.textContent = msg;
    }}

    function playPcm(bytes) {{
      const int16 = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
      const floats = new Float32Array(int16.length);
      for (let i = 0; i < int16.length; i++) floats[i] = int16[i] / 32768;
      const buf = ctx.createBuffer(1, floats.length, sampleRate);
      buf.copyToChannel(floats, 0);
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      if (nextTime < ctx.currentTime) nextTime = ctx.currentTime;
      src.start(nextTime);
      nextTime += buf.duration;
    }}

    async function stream() {{
      const resp = await fetch('/listen/stream/' + encodeURIComponent(sessionId));
      if (!resp.ok) {{
        const errText = await resp.text().catch(() => '');
        let msg = 'Could not connect audio stream (' + resp.status + ').';
        if (errText.includes('424') || errText.includes('Stream failed')) {{
          msg = 'Live audio is not available for agent extensions. Close this and tap Listen on phone in Telegram.';
        }} else if (errText) {{
          msg += ' ' + errText;
        }}
        showError(msg);
        return;
      }}
      document.getElementById('status').innerHTML = '<span class="live">● LIVE</span> listening…';
      document.getElementById('status').className = '';
      const reader = resp.body.getReader();
      let leftover = new Uint8Array(0);
      while (true) {{
        const {{ done, value }} = await reader.read();
        if (done) break;
        const merged = new Uint8Array(leftover.length + value.length);
        merged.set(leftover);
        merged.set(value, leftover.length);
        const usable = merged.length - (merged.length % 2);
        if (usable > 0) playPcm(merged.subarray(0, usable));
        leftover = merged.subarray(usable);
      }}
      showError('Stream ended.');
    }}

    async function startAudio() {{
      if (started) return;
      started = true;
      document.getElementById('start').disabled = true;
      if (tg && tg.MainButton) tg.MainButton.hide();
      document.getElementById('status').textContent = 'Connecting…';
      document.getElementById('status').className = '';
      if (initialError) {{
        showError(initialError.includes('424')
          ? 'Live audio is not available for agent extensions. Close this and tap Listen on phone in Telegram.'
          : initialError);
        return;
      }}
      try {{
        ctx = new AudioContext({{ sampleRate }});
        await ctx.resume();
        await stream();
      }} catch (e) {{
        showError('Audio error: ' + e);
      }}
    }}

    document.getElementById('start').onclick = startAudio;
  </script>
</body>
</html>"""


def _read_error_body(response: httpx.Response) -> str:
    try:
        body = response.read()
        if body:
            return body.decode("utf-8", errors="replace")[:200]
    except Exception:
        pass
    return ""


def iter_session_audio(session: ListenSession) -> Iterator[bytes]:
    session.listeners += 1
    try:
        if session.error:
            return
        while not session.stop_event.is_set():
            try:
                chunk = session.queue.get(timeout=1.0)
            except Empty:
                continue
            if chunk is None:
                break
            yield chunk
    finally:
        session.listeners -= 1
        if session.listeners <= 0:
            session.stop_event.set()


def _pump_3cx_stream(bot_data: dict, settings: Settings, session: ListenSession) -> None:
    fqdn = settings.threex_fqdn
    token_url = f"https://{fqdn}/connect/token"
    stream_url = (
        f"https://{fqdn}/callcontrol/{session.extension}"
        f"/participants/{session.participant_id}/stream"
    )

    try:
        request_extension_subscribe_sync(bot_data, session.extension, wait_seconds=0.2)

        with httpx.Client(timeout=httpx.Timeout(10.0, read=None)) as client:
            token_resp = client.post(
                token_url,
                data={
                    "client_id": settings.threex_client_id,
                    "client_secret": settings.threex_api_key,
                    "grant_type": "client_credentials",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if token_resp.status_code >= 400:
                session.error = f"Token failed ({token_resp.status_code})"
                session.queue.put(None)
                return

            token = token_resp.json().get("access_token")
            if not token:
                session.error = "Missing access token"
                session.queue.put(None)
                return

            with client.stream(
                "GET",
                stream_url,
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.status_code >= 400:
                    detail = _read_error_body(response)
                    session.error = (
                        f"Stream failed ({response.status_code})"
                        + (f": {detail}" if detail else "")
                    )
                    logger.warning(
                        "Listen stream failed ext %s pid %s: %s",
                        session.extension,
                        session.participant_id,
                        session.error,
                    )
                    session.queue.put(None)
                    return

                for chunk in response.iter_bytes(320):
                    if session.stop_event.is_set():
                        break
                    if chunk:
                        session.queue.put(chunk)
    except Exception as exc:
        session.error = str(exc)
        logger.exception("Listen stream error ext %s", session.extension)
    finally:
        session.queue.put(None)
