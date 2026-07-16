"""Press-1 Telegram bot handlers."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, RetryAfter
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    TypeHandler,
    filters,
)

import vicidial_client as vd
import press1_access as access
import press1_campaign as campaign
import press1_floor as floor
import press1_schedule as schedule
import press1_ui as ui
from press1_settings import THREECX_PROFILES, format_settings_text
from press1_utils import convert_audio_for_asterisk, parse_csv, parse_numbers

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")


def _webhook_public_url() -> str:
    url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if url:
        return url
    base = (
        os.getenv("TELEGRAM_WEBHOOK_URL_BASE")
        or os.getenv("RENDER_EXTERNAL_URL")
        or "https://p1-bot.onrender.com"
    ).rstrip("/")
    path = os.getenv("TELEGRAM_WEBHOOK_PATH", "telegram/webhook").lstrip("/")
    return f"{base}/{path}"


def _webhook_secret() -> str | None:
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if secret:
        return secret
    if TOKEN and ":" in TOKEN:
        return TOKEN.split(":", 1)[0]
    return None


def _cloud_deployed() -> bool:
    return os.getenv("CLOUD_DEPLOYED", "").strip().lower() in ("1", "true", "yes")


def _use_polling_mode() -> bool:
    return os.getenv("P1_USE_POLLING", "").strip().lower() in ("1", "true", "yes")


ALLOWED = access.OWNERS | {
    int(x.strip())
    for x in os.getenv("TELEGRAM_ALLOWED_IDS", os.getenv("ADMIN_CHAT_ID", "")).split(",")
    if x.strip().isdigit()
}

HELP = floor.help_card()


def _floor_pad() -> InlineKeyboardMarkup:
    """One-tap operator pad — the thing that makes the bot feel like a console."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🛫 GO", callback_data="floor:go"),
                InlineKeyboardButton("📡 PULSE", callback_data="floor:pulse"),
            ],
            [
                InlineKeyboardButton("⏸ PAUSE", callback_data="floor:pause"),
                InlineKeyboardButton("▶️ RESUME", callback_data="floor:unpause"),
                InlineKeyboardButton("🛑 STOP", callback_data="floor:stop"),
            ],
            [
                InlineKeyboardButton("📞 TEST", callback_data="floor:test"),
                InlineKeyboardButton("🎛 DASH", callback_data="floor:dash"),
                InlineKeyboardButton("🎯 ROUTE", callback_data="floor:settings"),
            ],
        ]
    )


@dataclass
class Session:
    numbers: list[str] = field(default_factory=list)


def _is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in ("group", "supergroup"))


def session_key_for(chat_id: int, user_id: int, *, group: bool) -> str:
    return f"chat:{chat_id}" if group else f"user:{user_id}"


def session_key(update: Update) -> str:
    chat = update.effective_chat
    user_id = update.effective_user.id if update.effective_user else 0
    if chat and chat.type in ("group", "supergroup"):
        return f"chat:{chat.id}"
    return f"user:{user_id}"


def session_for(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Session:
    key = session_key(update)
    store: dict[str, Session] = context.application.bot_data.setdefault("press1_session", {})
    if key not in store:
        store[key] = Session()
    return store[key]


def chat_progress(app: Application, chat_id: int) -> dict | None:
    entry = app.bot_data.get("chat_campaigns", {}).get(chat_id)
    if not entry:
        return None
    progress = entry.get("progress")
    return progress if isinstance(progress, dict) else None


def set_chat_progress(app: Application, chat_id: int, progress: dict) -> None:
    app.bot_data.setdefault("chat_campaigns", {}).setdefault(chat_id, {})["progress"] = progress


async def _campaign_run_id(app: Application, chat_id: int) -> tuple[str, dict | None]:
    """Resolve run_id from memory or the dial server (survives Render restarts)."""
    progress = chat_progress(app, chat_id)
    run_id = str((progress or {}).get("run_id", "") or "").strip()
    if not run_id:
        run_id = await asyncio.to_thread(vd.resolve_chat_run_id, chat_id) or ""
    if run_id:
        if progress is None:
            progress = {"chat_id": chat_id, "run_id": run_id, "running": True}
            set_chat_progress(app, chat_id, progress)
        elif not progress.get("run_id"):
            progress["run_id"] = run_id
            progress.setdefault("chat_id", chat_id)
    return run_id, progress


async def control_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Pause/unpause/stop: any member in group chats; DMs still require access."""
    if _is_group_chat(update):
        return True
    return await guard(update, context)


def session(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> Session:
    """Legacy per-user session (prefer session_for in handlers)."""
    key = f"user:{user_id}"
    store: dict[str, Session] = context.application.bot_data.setdefault("press1_session", {})
    if key not in store:
        store[key] = Session()
    return store[key]


def _note_user(app: Application, user) -> None:
    if not user:
        return
    store = app.bot_data.setdefault("known_users", {})
    store[str(user.id)] = {
        "user_id": user.id,
        "username": (user.username or "").lstrip("@").lower(),
        "name": user.full_name or "",
    }


def allowed(user_id: int) -> bool:
    return access.is_allowed(user_id)


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE | None = None) -> bool:
    user = update.effective_user
    uid = user.id if user else 0
    if user and context:
        _note_user(context.application, user)
    if not allowed(uid):
        if update.callback_query:
            await update.callback_query.answer()
        return False
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    chat_id = update.effective_chat.id
    s = session_for(update, context)
    transfer = ""
    try:
        transfer = str(_pacing(chat_id).get("transfer_label") or "")
    except Exception:
        pass
    await update.message.reply_text(
        floor.welcome_card(transfer=transfer, loaded=len(s.numbers)),
        reply_markup=_floor_pad(),
    )
    await update.message.reply_text(HELP, reply_markup=_floor_pad())
    user = update.effective_user
    if user:
        asyncio.create_task(
            asyncio.to_thread(access.remember_user, user.id, user.username, user.full_name)
        )


_MIN_EDIT_INTERVAL = 4.0
_last_edit_at: dict[tuple[int, int], float] = {}


def _flood_retry_seconds(exc: BaseException) -> float | None:
    if isinstance(exc, RetryAfter):
        return float(exc.retry_after) + 0.5
    msg = str(exc).lower()
    if "flood control" in msg or "retry after" in msg:
        match = re.search(r"retry in (\d+)", str(exc), re.I)
        return (float(match.group(1)) if match else 3.0) + 0.5
    return None


async def _edit_text_resilient(edit_coro, *, chat_id: int, message_id: int) -> None:
    """Edit with flood-control backoff; ignore unchanged text."""
    key = (chat_id, message_id)
    now = time.time()
    if now - _last_edit_at.get(key, 0.0) < _MIN_EDIT_INTERVAL:
        return
    for _ in range(5):
        try:
            await edit_coro()
            _last_edit_at[key] = time.time()
            return
        except BadRequest as e:
            low = str(e).lower()
            if "message is not modified" in low:
                _last_edit_at[key] = time.time()
                return
            wait = _flood_retry_seconds(e)
            if wait is not None:
                await asyncio.sleep(wait)
                continue
            raise
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
    return


async def _safe_edit(msg: Message, text: str) -> None:
    """Edit message; throttle and retry on Telegram flood limits."""
    await _edit_text_resilient(
        lambda: msg.edit_text(text),
        chat_id=msg.chat_id,
        message_id=msg.message_id,
    )


_STATE_LABELS = {
    "running": "🟢 Dialling",
    "paused": "⏸ Paused — live calls continue",
    "finishing": "🟡 Finishing — calls in flight",
    "finished": "✅ Finished",
    "stalled": "⚠️ Stopped early",
    "idle": "⚪ Idle",
}


def _state_line(st: dict[str, str]) -> str:
    label = _STATE_LABELS.get(st.get("dial_state", ""), "⚪ Unknown")
    return f"<i>{ui.esc(label)}</i>"


_PACING_CACHE: dict[str, object] = {"at": 0.0, "data": {}}


def _pacing(chat_id: int | None = None) -> dict:
    now = time.time()
    cached = _PACING_CACHE.get("data")
    if cached and now - float(_PACING_CACHE.get("at", 0)) < 60:
        return cached  # type: ignore[return-value]
    summary = vd.settings_summary(chat_id)
    # Prefer dialer_cap (live channel ceiling) over legacy max_concurrent (often 0/∞).
    try:
        cap = int(summary.get("dialer_cap") or 0)
    except ValueError:
        cap = 0
    if cap <= 0:
        try:
            cap = int(summary.get("max_concurrent") or 0)
        except ValueError:
            cap = 0
    if cap <= 0:
        cap = 40
    data = {
        "call_gap": float(summary["call_gap"]),
        "batch_size": int(summary["batch_size"]),
        "batch_pause": int(summary["batch_pause"]),
        "max_concurrent": cap,
        "transfer_label": summary.get("threex_label", summary["threex_target"]),
    }
    _PACING_CACHE["at"] = now
    _PACING_CACHE["data"] = data
    return data


async def _format_live_stats(
    st: dict[str, str],
    total_leads: int,
    *,
    finished: bool = False,
    progress: dict | None = None,
) -> str:
    prog = progress or {}
    pacing = _pacing(int(prog.get("chat_id", 0) or 0) or None)
    frame = int(prog.get("_frame", 0) or 0)
    body = campaign.format_campaign_body(
        st,
        total_leads,
        progress=prog,
        call_gap=pacing["call_gap"],
        batch_size=pacing["batch_size"],
        batch_pause=pacing["batch_pause"],
        frame=frame,
        finished=finished,
    )
    dial_state = st.get("dial_state", "")
    if not finished and dial_state not in ("finished", "stalled"):
        body += f"\n{_state_line(st)}"
    return body


def _warn(text: str) -> str:
    return f"\n\n⚠️ <i>{ui.esc(text)}</i>"


async def _format_status(st: dict[str, str], loaded_in_bot: int) -> str:
    total = int(st.get("list_size", 0) or 0)
    dialed = int(st.get("dialed", 0) or 0)
    answered = int(st.get("answered", 0) or 0)
    press1 = int(st.get("press1", 0) or 0)
    waiting = int(st.get("hopper", 0) or 0)
    live = int(st.get("live", 0) or 0)
    failed = int(st.get("failed", 0) or 0)
    pct = (dialed * 100 // total) if total > 0 else 0
    lines = [
        ui.esc(campaign.progress_line(pct, dialed, total)),
        "",
        ui.stat("List on server", total, icon="📋"),
        ui.stat("Dialed", dialed, icon="📞"),
        ui.stat("Live now", live, icon="📡"),
        ui.stat("Waiting", waiting, icon="⏳"),
    ]
    lines.append("")
    if dialed > 0:
        ans_pct = answered * 100 / dialed
        p1_pct = press1 * 100 / dialed
        lines.append(ui.stat("Answered", answered, icon="✅", suffix=f" ({ans_pct:.0f}%)"))
        lines.append(ui.stat("Press-1", press1, icon="🔥", suffix=f" ({p1_pct:.1f}%)"))
    else:
        lines.append(ui.stat("Answered", answered, icon="✅"))
        lines.append(ui.stat("Press-1", press1, icon="🔥"))
    if failed > 0:
        lines.append(ui.stat("Failed", failed, icon="❌"))
    if loaded_in_bot != total:
        lines.append(ui.stat("In bot session", loaded_in_bot, icon="💾"))
    lines.append("")
    lines.append(_state_line(st))
    return ui.card("📊 STATUS SNAPSHOT", lines)


async def _live_campaign_updater(
    msg: Message,
    total_leads: int,
    run_since: str,
    stop_event: asyncio.Event,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    last_text = ""
    idle_rounds = 0
    frame = 0
    last_press1 = 0
    while not stop_event.is_set():
        try:
            progress = chat_progress(context.application, chat_id) or {}
            progress["_frame"] = frame
            frame += 1
            st = await asyncio.to_thread(vd.get_dial_stats, run_since, progress)
            err = progress.get("error")
            live_total = int(st.get("list_size", 0) or 0) or total_leads
            press1_now = int(st.get("press1", 0) or 0)
            answered_now = int(st.get("answered", 0) or 0)
            if press1_now > last_press1:
                try:
                    await context.bot.send_message(
                        chat_id,
                        floor.hit_alert(
                            callsign=str(progress.get("callsign") or ""),
                            press1=press1_now,
                            answered=answered_now,
                        ),
                    )
                except Exception:
                    pass
                last_press1 = press1_now
            text = await _format_live_stats(st, live_total, progress=progress)
            if progress.get("stalled"):
                text += _warn("Dialer stopped early on server — upload a new list and /go")
            if err:
                text += _warn(err)
            hopper = int(st.get("hopper", 0) or 0)
            live = int(st.get("live", 0) or 0)
            dial_state = st.get("dial_state", "")
            active = dial_state in ("running", "paused")
            dialed = int(st.get("dialed", 0) or 0)
            if text != last_text:
                try:
                    await _edit_text_resilient(
                        lambda: msg.edit_text(text, reply_markup=_floor_pad()),
                        chat_id=msg.chat_id,
                        message_id=msg.message_id,
                    )
                except Exception:
                    await _safe_edit(msg, text)
                last_text = text
            if dial_state in ("finished", "stalled") and hopper == 0:
                idle_rounds += 1
                if idle_rounds >= 2:
                    final = await _format_live_stats(st, live_total, finished=True, progress=progress)
                    final += "\n" + floor.finished_banner(
                        callsign=str(progress.get("callsign") or ""),
                        dialed=dialed,
                        answered=answered_now,
                        press1=press1_now,
                    )
                    err = progress.get("error")
                    if err:
                        final += _warn(err)
                    try:
                        await _safe_edit(msg, final)
                    except BadRequest:
                        pass
                    break
            elif dial_state == "finishing" and hopper == 0 and live == 0:
                idle_rounds += 1
                if idle_rounds >= 3:
                    final = await _format_live_stats(st, live_total, finished=True, progress=progress)
                    final += "\n" + floor.finished_banner(
                        callsign=str(progress.get("callsign") or ""),
                        dialed=dialed,
                        answered=answered_now,
                        press1=press1_now,
                    )
                    try:
                        await _safe_edit(msg, final)
                    except BadRequest:
                        pass
                    break
            elif dial_state == "stalled" and hopper > 0 and dialed > 0:
                idle_rounds += 1
                if idle_rounds >= 4:
                    final = await _format_live_stats(st, live_total, finished=True, progress=progress)
                    final += _warn("Dialer stopped early on server — upload a new list and /go")
                    try:
                        await _safe_edit(msg, final)
                    except BadRequest:
                        pass
                    break
            elif not active and dialed == 0 and idle_rounds >= 6:
                final = await _format_live_stats(st, live_total, finished=True, progress=progress)
                err = progress.get("error") or "Dialer never started — try /go again"
                final += _warn(err)
                try:
                    await _safe_edit(msg, final)
                except BadRequest:
                    pass
                break
            else:
                idle_rounds = 0 if active or dialed > 0 else idle_rounds + 1
        except Exception as e:
            wait = _flood_retry_seconds(e)
            if wait is not None:
                await asyncio.sleep(wait)
                continue
            try:
                await msg.edit_text(ui.error(f"Live update error: {e}"))
            except Exception:
                pass
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.2 if dial_state in ("running", "paused") else 6.0)
            break
        except asyncio.TimeoutError:
            pass
    progress = chat_progress(context.application, chat_id)
    if progress:
        progress["running"] = False


def _stop_dialer(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    progress = chat_progress(context.application, chat_id)
    if progress:
        progress["stop"] = True
    run_id = str((progress or {}).get("run_id", "") or "")
    if not run_id:
        run_id = vd.resolve_chat_run_id(chat_id) or ""
    if run_id:
        try:
            vd._stop_remote_dialer(run_id)
        except Exception:
            pass
    task = context.application.bot_data.get("dial_tasks", {}).get(chat_id)
    if task and not task.done():
        task.cancel()
    context.application.bot_data.setdefault("dial_tasks", {}).pop(chat_id, None)


def _stop_live_updater(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    _stop_dialer(context, chat_id)
    tasks = context.application.bot_data.setdefault("live_updater_tasks", {})
    stops = context.application.bot_data.setdefault("live_updater_stops", {})
    stop = stops.pop(chat_id, None)
    if stop:
        stop.set()
    task = tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    msg = await update.message.reply_text("📡 Reading server…")
    try:
        st = await asyncio.to_thread(
            vd.get_dial_stats,
            None,
            chat_progress(context.application, update.effective_chat.id),
        )
        s = session_for(update, context)
        text = await _format_status(st, len(s.numbers))
        await msg.edit_text(text)
    except Exception as e:
        await msg.edit_text(ui.error(f"Status failed: {e}"))


async def cmd_leads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    s = session_for(update, context)
    await update.message.reply_text(
        f"💾 {len(s.numbers)} leads loaded. Send more or /go.",
        reply_markup=_floor_pad(),
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    chat_id = update.effective_chat.id
    session_for(update, context).numbers.clear()
    _stop_live_updater(context, chat_id)
    # Also wipe the server-side number file — session clear alone left the dialer
    # (or watchdog) still working the previous list.
    try:
        await asyncio.to_thread(vd.abandon_chat_campaign, chat_id)
    except Exception as e:
        print(f"[press1] abandon on /clear: {e}")
    camps = context.application.bot_data.get("chat_campaigns", {})
    camps.pop(chat_id, None)
    await update.message.reply_text(
        "🧹 Loaded leads cleared — any dialer for this chat was stopped."
    )


async def _replace_session_leads(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    nums: list[str],
) -> None:
    """Replace the Telegram lead list and abandon any dialer still using the old list."""
    chat_id = update.effective_chat.id
    s = session_for(update, context)
    prev = len(s.numbers)
    s.numbers = list(dict.fromkeys(nums))
    if prev > 0:
        _stop_live_updater(context, chat_id)
        try:
            await asyncio.to_thread(vd.abandon_chat_campaign, chat_id)
        except Exception as e:
            print(f"[press1] abandon on replace: {e}")
        camps = context.application.bot_data.get("chat_campaigns", {})
        camps.pop(chat_id, None)
    note = f" (replaced {prev} previously loaded)" if prev > 0 else ""
    cap = 40
    gap = 0.2
    try:
        pacing = _pacing(chat_id)
        cap = int(pacing.get("max_concurrent") or 40)
        gap = float(pacing.get("call_gap") or 0.2)
    except Exception:
        pass
    await update.message.reply_text(
        floor.leads_brief(count=len(s.numbers), replaced=prev, cap=cap, gap=gap),
        reply_markup=_floor_pad(),
    )


def _threex_keyboard(active_id: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for pid, info in THREECX_PROFILES.items():
        mark = " ✅" if pid == active_id else ""
        rows.append(
            [InlineKeyboardButton(f"{info['label']}{mark}", callback_data=f"p1_3cx:{pid}")]
        )
    return InlineKeyboardMarkup(rows)


def _settings_message(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    summary = vd.settings_summary(chat_id)
    text = format_settings_text(
        threex_id=summary["threex_target"],
        sound_name=summary["sound_name"],
        call_gap=float(summary["call_gap"]),
        batch_size=int(summary["batch_size"]),
        batch_pause=int(summary["batch_pause"]),
        max_concurrent=int(summary["max_concurrent"]),
    )
    return text, _threex_keyboard(summary["threex_target"])


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    chat_id = update.effective_chat.id
    reply = update.effective_message
    if not reply:
        return
    msg = await reply.reply_text("⚙️ Loading settings…")
    try:
        text, keyboard = await asyncio.to_thread(_settings_message, chat_id)
        await msg.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        await msg.edit_text(ui.error(f"Settings failed: {e}"))


async def on_threex_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    if not allowed(query.from_user.id if query.from_user else 0):
        await query.answer()
        return
    if not query.data.startswith("p1_3cx:"):
        return
    profile_id = query.data.split(":", 1)[1]
    chat_id = query.message.chat.id if query.message else 0
    await query.answer("Updating transfer target…")
    try:
        p = await asyncio.to_thread(vd.apply_threex_target, profile_id, chat_id)
        text, keyboard = await asyncio.to_thread(_settings_message, chat_id)
        note = f"\n\n✅ This chat now transfers to {ui.b(p['label'])}."
        await query.edit_message_text(text + note, reply_markup=keyboard)
    except Exception as e:
        await query.edit_message_text(ui.error(f"Transfer update failed: {e}"))


async def _resolve_addkey_target(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str | None, bool]:
    """Return (target, from_mention)."""
    for ent in update.message.entities or []:
        if ent.type == "text_mention" and ent.user:
            user = ent.user
            _note_user(context.application, user)
            await asyncio.to_thread(access.remember_user, user.id, user.username, user.full_name)
            return str(user.id), True
    args = context.args or []
    if args:
        return args[0], False
    return None, False


async def cmd_addkey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not access.is_owner(uid):
        await update.message.reply_text("🔒 Only the owner can grant access.")
        return
    target, from_mention = await _resolve_addkey_target(update, context)
    args = context.args or []
    if from_mention:
        duration = args[0] if args else None
    else:
        duration = args[1] if len(args) >= 2 else None
    if not target or not duration:
        await update.message.reply_text(
            "🔑 <b>Grant temporary access</b>\n\n"
            "Usage: /addkey @username 24h\n"
            "Or: /addkey &lt;user_id&gt; 24h\n\n"
            "Durations: 30m · 24h · 7d · 1w"
        )
        return
    extra = context.application.bot_data.get("known_users", {})
    try:
        grant = await asyncio.to_thread(
            access.add_grant,
            target=target,
            duration_text=duration,
            granted_by=uid,
            granter_name=update.effective_user.username if update.effective_user else None,
            extra_users=extra,
        )
        exp = datetime.fromtimestamp(grant["expires_at"], tz=timezone.utc)
        await update.message.reply_text(
            f"✅ Access granted to {ui.b(grant['label'])} ({ui.code(grant['user_id'])})\n"
            f"⏳ Expires {ui.esc(exp.strftime('%Y-%m-%d %H:%M UTC'))} "
            f"({ui.esc(grant['duration'])})"
        )
    except Exception as e:
        await update.message.reply_text(ui.error(e))


async def cmd_listkeys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not access.is_owner(uid):
        await update.message.reply_text("🔒 Only the owner can list access keys.")
        return
    try:
        text = await asyncio.to_thread(access.format_grant_list)
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(ui.error(e))


async def cmd_revokekey(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not access.is_owner(uid):
        await update.message.reply_text("🔒 Only the owner can revoke access.")
        return
    target, _from_mention = await _resolve_addkey_target(update, context)
    if not target:
        await update.message.reply_text(
            "🔑 <b>Revoke access</b>\n\n"
            "Usage: /revokekey @username\nOr: /revokekey &lt;user_id&gt;"
        )
        return
    extra = context.application.bot_data.get("known_users", {})
    try:
        label = await asyncio.to_thread(
            access.revoke_grant,
            target=target,
            revoked_by=uid,
            extra_users=extra,
        )
        await update.message.reply_text(f"✅ Revoked access for {ui.b(label)}.")
    except Exception as e:
        await update.message.reply_text(ui.error(e))


async def cmd_repair(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not access.is_owner(uid):
        await update.message.reply_text("🔒 Only the owner can run /repair.")
        return
    await update.message.reply_text("🛠 Re-syncing dialplan, BitCall, DTMF listeners…")
    try:
        result = await asyncio.to_thread(vd.repair_press1_server)
        lines = [f"• {k}: {str(v)[:120]}" for k, v in result.items()]
        await update.message.reply_text("✅ Stack repaired:\n" + "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(ui.error(e))


async def cmd_testnumber(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        await update.message.reply_text(ui.error("Access denied — use /addkey or ask an admin."))
        return
    from press1_utils import to_e164
    import re

    chat_id = update.effective_chat.id
    if not context.args:
        cfg = await asyncio.to_thread(vd.get_chat_settings, chat_id)
        tn = (cfg.get("test_number") or "").strip()
        if tn:
            shown = to_e164(tn) or tn
        else:
            nums = await asyncio.to_thread(vd.test_numbers, chat_id=chat_id, prefer_owner=True)
            shown = nums[0] if nums else ""
        if shown:
            await update.message.reply_text(
                ui.card("📱 TEST NUMBER", [ui.bullet(f"+{shown}", "used by /testcall in this chat", icon="☎️")])
            )
        else:
            await update.message.reply_text(
                ui.error("No test number set. Example: /testnumber 07769799593")
            )
        return
    raw = " ".join(context.args).strip()
    digits = to_e164(raw) or re.sub(r"\D", "", raw)
    if len(digits) < vd.MIN_PHONE_DIGITS + 2:
        await update.message.reply_text(ui.error("Invalid number. Example: /testnumber 07769799593"))
        return
    if not digits.startswith("44"):
        await update.message.reply_text(
            ui.error("Test number must be UK (+44). Example: /testnumber 07769799593")
        )
        return
    await asyncio.to_thread(vd.save_chat_settings, chat_id, test_number=digits)
    await update.message.reply_text(
        ui.card("📱 TEST NUMBER SAVED", [ui.bullet(f"+{digits}", "use /testcall to ring", icon="☎️")])
    )


async def cmd_testcall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply = update.effective_message
    if not await guard(update, context):
        if reply:
            await reply.reply_text(ui.error("Access denied — use /addkey or ask an admin."))
        return
    if not reply:
        return
    nums: list[str] | None = None
    user_id = update.effective_user.id if update.effective_user else 0
    if context.args:
        from press1_utils import to_e164
        import re

        if len(context.args) == 1 and context.args[0].lower() == "me":
            nums = await asyncio.to_thread(
                vd.test_numbers, chat_id=update.effective_chat.id, prefer_owner=True
            )
        else:
            parsed: list[str] = []
            for arg in context.args:
                if arg.lower() == "me":
                    continue
                digits = to_e164(arg) or re.sub(r"\D", "", arg)
                if len(digits) >= vd.MIN_PHONE_DIGITS + 2:
                    parsed.append(digits)
            if not parsed:
                await reply.reply_text(
                    ui.error("Invalid number(s). Example: /testcall 447934567847 or /testcall me")
                )
                return
            nums = parsed
    else:
        # Bare /testcall always rings the configured owner test mobile.
        nums = await asyncio.to_thread(
            vd.test_numbers, chat_id=update.effective_chat.id, prefer_owner=True
        )
        if not nums:
            nums = await asyncio.to_thread(vd.test_numbers, chat_id=update.effective_chat.id)
        if not nums:
            await reply.reply_text(
                ui.error(
                    "No test numbers configured on the server. "
                    "Use /testcall 447769799593 to dial your number."
                )
            )
            return
    preview = ", ".join(f"+{n}" for n in nums)
    msg = await reply.reply_text(f"📞 Placing test calls to {preview}…")
    try:
        placed = await asyncio.to_thread(vd.test_calls, nums, update.effective_chat.id)
        card = ui.card(
            "📞  TEST CALLS PLACED",
            [ui.bullet(n, "", icon="☎️") for n in placed] or [ui.note("⚪", "none")],
        )
        await msg.edit_text(card)
    except Exception as e:
        err = str(e)
        if "Wait " in err and "BitCall" in err:
            await msg.edit_text(ui.error(err))
        else:
            await msg.edit_text(
                ui.error(f"Test call failed: {e}")
                + "\n\n<i>If your phone didn't ring, wait 60s and try once. "
                "Rapid /testcall retries get blocked by the carrier.</i>"
            )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await control_guard(update, context):
        return
    chat_id = update.effective_chat.id
    reply = update.effective_message
    if not reply:
        return
    _stop_live_updater(context, chat_id)
    msg = await reply.reply_text("🛑 Stopping campaign…")
    try:
        await asyncio.to_thread(vd.abandon_chat_campaign, chat_id)
    except Exception as e:
        print(f"[press1] abandon on /stop: {e}")
    camps = context.application.bot_data.get("chat_campaigns", {})
    camps.pop(chat_id, None)
    await msg.edit_text("🛑 Campaign stopped — old lead file cleared for this chat.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await control_guard(update, context):
        return
    chat_id = update.effective_chat.id
    reply = update.effective_message
    if not reply:
        return
    run_id, progress = await _campaign_run_id(context.application, chat_id)
    if not run_id:
        await reply.reply_text(ui.error("No active campaign in this chat."))
        return
    msg = await reply.reply_text("⏸ Pausing campaign…")
    try:
        st = await asyncio.to_thread(vd.pause_dial_campaign, run_id)
        if progress:
            progress["paused"] = True
            progress["running"] = True
        await msg.edit_text(
            ui.card(
                "⏸  CAMPAIGN PAUSED",
                [
                    ui.bullet("Dialed", f"{st['dialed']} / {st['total']}", icon="📞"),
                    ui.bullet("Left", st["left"], icon="⏳"),
                ],
            )
            + (
                "\n<i>Dialer stopped — /unpause will resume the remaining leads.</i>"
                if st.get("stalled") == "Y"
                else "\n<i>Live calls will finish · /unpause to continue.</i>"
            )
        )
    except Exception as e:
        await msg.edit_text(ui.error(f"Pause failed: {e}"))


async def cmd_unpause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await control_guard(update, context):
        return
    chat_id = update.effective_chat.id
    reply = update.effective_message
    if not reply:
        return
    run_id, progress = await _campaign_run_id(context.application, chat_id)
    if not run_id:
        await reply.reply_text(ui.error("No active campaign in this chat."))
        return
    msg = await reply.reply_text("▶️ Resuming campaign…")
    try:
        st = await asyncio.to_thread(vd.unpause_dial_campaign, run_id)
        if progress:
            progress["paused"] = False
            progress["running"] = True
        await msg.edit_text(
            ui.card(
                "▶️  CAMPAIGN RESUMED",
                [
                    ui.bullet("Dialed", f"{st['dialed']} / {st['total']}", icon="📞"),
                    ui.bullet("Left", st["left"], icon="⏳"),
                ],
            )
        )
    except Exception as e:
        await msg.edit_text(ui.error(f"Unpause failed: {e}"))


async def _safe_edit_id(app: Application, chat_id: int, message_id: int, text: str) -> bool:
    """Edit a message by id; return False if the message is gone."""
    gone = False

    async def _do_edit() -> None:
        nonlocal gone
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text
            )
        except BadRequest as e:
            low = str(e).lower()
            if "message to edit not found" in low or "message can't be edited" in low:
                gone = True
                return
            raise

    try:
        await _edit_text_resilient(_do_edit, chat_id=chat_id, message_id=message_id)
    except Exception:
        return not gone
    return not gone


async def _persist_dashboards(app: Application) -> None:
    boards: dict = app.bot_data.get("dashboards", {})
    items = [
        {"chat_id": cid, "message_id": e["msg_id"], "user_id": e["user_id"]}
        for cid, e in boards.items()
        if e.get("msg_id")
    ]
    try:
        await asyncio.to_thread(vd.save_dashboards, items)
    except Exception as e:
        print(f"[press1] dashboard persist: {e}")


async def _dashboard_updater(
    app: Application,
    chat_id: int,
    message_id: int,
    stop_event: asyncio.Event,
    user_id: int,
) -> None:
    frame = 0
    last_text = ""
    while not stop_event.is_set():
        try:
            progress = chat_progress(app, chat_id) or {}
            progress["_frame"] = frame
            frame += 1
            run_since = progress.get("run_since") or None
            st = await asyncio.to_thread(vd.get_dial_stats, run_since, progress)
            total = int(st.get("list_size", 0) or 0) or int(progress.get("total", 0) or 0)
            store: dict = app.bot_data.get("press1_session", {})
            loaded = len(store.get(f"chat:{chat_id}", Session()).numbers)
            pacing = _pacing(chat_id)
            if frame == 1 or frame % 10 == 0:
                scheduled = await asyncio.to_thread(schedule.list_schedules, user_id)
                app.bot_data["_dash_sched"] = scheduled
            else:
                scheduled = app.bot_data.get("_dash_sched", [])
            text = campaign.format_dashboard(
                st,
                total_leads=total,
                loaded_in_bot=loaded,
                progress=progress,
                call_gap=pacing["call_gap"],
                batch_size=pacing["batch_size"],
                batch_pause=pacing["batch_pause"],
                max_concurrent=pacing["max_concurrent"],
                transfer_label=pacing["transfer_label"],
                frame=frame,
                scheduled_count=len(scheduled),
            )
            if text != last_text:
                alive = await _safe_edit_id(app, chat_id, message_id, text)
                if not alive:
                    # Message deleted by the user — retire this dashboard.
                    app.bot_data.get("dashboards", {}).pop(chat_id, None)
                    await _persist_dashboards(app)
                    return
                last_text = text
        except Exception as e:
            print(f"[press1] dashboard {chat_id}: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            break
        except asyncio.TimeoutError:
            pass


def _start_dashboard_task(
    app: Application, chat_id: int, message_id: int, user_id: int
) -> None:
    boards = app.bot_data.setdefault("dashboards", {})
    existing = boards.pop(chat_id, None)
    if existing:
        stop = existing.get("stop")
        if stop:
            stop.set()
        task = existing.get("task")
        if task and not task.done():
            task.cancel()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        _dashboard_updater(app, chat_id, message_id, stop_event, user_id)
    )
    boards[chat_id] = {
        "stop": stop_event,
        "task": task,
        "msg_id": message_id,
        "user_id": user_id,
    }


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    reply = update.effective_message
    if not reply:
        return
    app = context.application

    # Retire any previous pinned dashboard in this chat.
    old = context.application.bot_data.get("dashboards", {}).get(chat_id)
    if old and old.get("msg_id"):
        try:
            await context.bot.unpin_chat_message(chat_id, old["msg_id"])
        except Exception:
            pass

    msg = await reply.reply_text("🎛  Booting control room…")
    try:
        await context.bot.pin_chat_message(
            chat_id, msg.message_id, disable_notification=True
        )
    except Exception:
        pass
    _start_dashboard_task(app, chat_id, msg.message_id, user_id)
    await _persist_dashboards(app)


async def _resume_dashboards(app: Application) -> None:
    try:
        saved = await asyncio.to_thread(vd.load_dashboards)
    except Exception as e:
        print(f"[press1] dashboard resume load: {e}")
        return
    alive: list[dict] = []
    for d in saved:
        chat_id = int(d.get("chat_id", 0) or 0)
        message_id = int(d.get("message_id", 0) or 0)
        user_id = int(d.get("user_id", 0) or 0)
        if not chat_id or not message_id:
            continue
        ok = await _safe_edit_id(app, chat_id, message_id, "🎛  Reconnecting control room…")
        if not ok:
            continue
        _start_dashboard_task(app, chat_id, message_id, user_id)
        alive.append(
            {"chat_id": chat_id, "message_id": message_id, "user_id": user_id}
        )
    if alive != saved:
        try:
            await asyncio.to_thread(vd.save_dashboards, alive)
        except Exception:
            pass
    if alive:
        print(f"[press1] resumed {len(alive)} pinned dashboard(s)")


async def _launch_campaign(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    numbers: list[str],
    *,
    intro: str | None = None,
) -> None:
    count = len(numbers)
    _stop_live_updater(context, chat_id)
    # Same safe pacing as website campaigns — uncapped /run was why Telegram
    # campaigns got answers with 0 press-1s while /testcall still worked.
    dialer_cap = max(1, min(int(vd.DIALER_CONCURRENT_CAP or 40), 80))
    call_gap = max(0.2, float(vd.CALL_GAP_SEC or 0.2))
    callsign = floor.fresh_callsign()
    # Re-bind this chat's transfer/audio before dialing (matches /testcall path).
    try:
        await asyncio.to_thread(vd.apply_run_config, vd.chat_cfg_run_id(chat_id), chat_id)
    except Exception as e:
        print(f"[press1] apply_run_config before /run: {e}")
    text_intro = intro or floor.launch_banner(
        callsign=callsign, count=count, cap=dialer_cap, gap=call_gap
    )
    msg = await context.bot.send_message(chat_id, text_intro, reply_markup=_floor_pad())
    try:
        run_since = await asyncio.to_thread(vd.server_now)
        progress: dict = {
            "started": 0,
            "dialed": 0,
            "failed": 0,
            "press1": 0,
            "answered": 0,
            "live": 0,
            "total": count,
            "running": True,
            "stop": False,
            "run_since": run_since,
            "run_id": "",
            "chat_id": chat_id,
            "owner_id": user_id,
            "pace_samples": [],
            "_frame": 0,
            "dialer_cap": dialer_cap,
            "call_gap_sec": call_gap,
            "callsign": callsign,
        }
        set_chat_progress(context.application, chat_id, progress)
        try:
            await asyncio.to_thread(vd.launch_dial_campaign, numbers, progress)
        except Exception as e:
            progress["error"] = str(e)
            progress["running"] = False
            st = await asyncio.to_thread(vd.get_dial_stats, run_since, progress)
            await _safe_edit(
                msg,
                await _format_live_stats(st, count, progress=progress) + _warn(e),
            )
            return
        fresh = {
            "list_size": str(count),
            "dialed": "0",
            "hopper": str(count),
            "live": "0",
            "answered": "0",
            "press1": "0",
            "failed": "0",
            "dial_state": "running",
        }
        await _safe_edit(msg, await _format_live_stats(fresh, count, progress=progress))
        stop_event = asyncio.Event()
        context.application.bot_data.setdefault("live_updater_stops", {})[chat_id] = stop_event
        task = asyncio.create_task(
            _live_campaign_updater(msg, count, run_since, stop_event, context, chat_id)
        )
        context.application.bot_data.setdefault("live_updater_tasks", {})[chat_id] = task
    except Exception as e:
        try:
            await _safe_edit(msg, ui.error(f"Run failed: {e}"))
        except Exception:
            await context.bot.send_message(chat_id, ui.error(f"Run failed: {e}"))


async def _schedule_loop(app: Application) -> None:
    while True:
        try:
            due = await asyncio.to_thread(schedule.pop_due_schedules)
            for job in due:
                uid = int(job.get("user_id", 0) or 0)
                chat_id = int(job.get("chat_id", uid) or uid)
                numbers = list(job.get("numbers") or [])
                if not numbers:
                    continue
                if job.get("source") == "dashboard":
                    try:
                        from dash_api import launch_dashboard_campaign

                        await asyncio.to_thread(launch_dashboard_campaign, chat_id, numbers)
                    except Exception as e:
                        print(f"[press1] dashboard schedule failed: {e}")
                    continue
                if not access.is_allowed(uid):
                    continue
                run_at = datetime.fromtimestamp(
                    float(job.get("run_at", 0)), tz=schedule.TZ
                )
                intro = (
                    f"⏰ Scheduled campaign starting — {len(numbers)} leads "
                    f"(was due {run_at.strftime('%H:%M %Z')})…"
                )

                class _AppContext:
                    def __init__(self, application: Application) -> None:
                        self.application = application
                        self.bot = application.bot

                await _launch_campaign(_AppContext(app), chat_id, uid, numbers, intro=intro)
        except Exception as e:
            print(f"[press1] schedule loop: {e}")
        await asyncio.sleep(15)


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    args = context.args or []
    if args and args[0].lower() in ("list", "ls"):
        text = await asyncio.to_thread(
            schedule.format_schedule_list, update.effective_user.id
        )
        await update.message.reply_text(text)
        return
    s = session_for(update, context)
    if not s.numbers:
        await update.message.reply_text(
            "⏰ Schedule a campaign\n\n"
            "Load leads first, then:\n"
            "/schedule 9am\n"
            "/schedule tomorrow 10:30"
        )
        return
    try:
        run_at = await asyncio.to_thread(schedule.parse_schedule_args, args)
    except ValueError as e:
        await update.message.reply_text(ui.error(e))
        return
    numbers = list(s.numbers)
    s.numbers.clear()
    try:
        entry = await asyncio.to_thread(
            schedule.add_schedule,
            user_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            numbers=numbers,
            run_at=run_at,
        )
        await update.message.reply_text(
            ui.card(
                "⏰  CAMPAIGN SCHEDULED",
                [
                    ui.bullet("Leads", len(numbers), icon="📥"),
                    ui.bullet("When", run_at.strftime("%a %d %b %H:%M %Z"), icon="🗓"),
                    f"🆔 <b>ID</b>  {ui.code(entry['id'])}",
                ],
            )
            + f"\n<i>/schedules to view · /unschedule {ui.esc(entry['id'])} to cancel</i>"
        )
    except Exception as e:
        s.numbers = numbers
        await update.message.reply_text(ui.error(f"Schedule failed: {e}"))


async def cmd_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    try:
        text = await asyncio.to_thread(
            schedule.format_schedule_list, update.effective_user.id
        )
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(ui.error(e))


async def cmd_unschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("⏰ Usage: /unschedule &lt;id&gt;")
        return
    try:
        sid = await asyncio.to_thread(
            schedule.remove_schedule, args[0], update.effective_user.id
        )
        await update.message.reply_text(f"✅ Cancelled schedule {ui.code(sid)}.")
    except Exception as e:
        await update.message.reply_text(ui.error(e))


async def cmd_pulse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    msg = update.effective_message
    if not msg:
        return
    chat_id = update.effective_chat.id
    status = await msg.reply_text("📡 Reading the floor…")
    try:
        progress = chat_progress(context.application, chat_id)
        st = await asyncio.to_thread(vd.get_dial_stats, None, progress)
        transfer = ""
        try:
            transfer = str(_pacing(chat_id).get("transfer_label") or "")
        except Exception:
            pass
        s = session_for(update, context)
        text = floor.pulse_card(
            st,
            callsign=str((progress or {}).get("callsign") or ""),
            transfer=transfer,
            loaded=len(s.numbers),
        )
        await status.edit_text(text, reply_markup=_floor_pad())
    except Exception as e:
        await status.edit_text(ui.error(f"Pulse failed: {e}"))


async def _preflight_checks(chat_id: int, lead_count: int) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Leads", lead_count > 0, f"{lead_count} in hopper" if lead_count else "empty — paste a list"))
    transfer = "—"
    try:
        summary = await asyncio.to_thread(vd.settings_summary, chat_id)
        transfer = str(summary.get("threex_label") or summary.get("threex_target") or "—")
        checks.append(("Transfer", bool(transfer and transfer != "—"), transfer))
    except Exception as e:
        checks.append(("Transfer", False, str(e)[:80]))
    try:
        ready = await asyncio.to_thread(vd.ensure_press1_ready)
        dp = str(ready.get("dialplan", ""))
        dialplan_ok = "error" not in dp.lower() and bool(dp)
        dtmf = str(ready.get("dtmf", ""))
        dtmf_ok = "active" in dtmf.lower() or "ok" in dtmf.lower()
        audio = str(ready.get("audio_dtmf", "active"))
        audio_ok = "error" not in audio.lower()
        ep = str(ready.get("endpoints", ""))
        ep_ok = "error" not in ep.lower() and bool(ep)
        checks.append(("Dialplan", dialplan_ok, dp[:60] or "checked"))
        checks.append(("DTMF stack", dtmf_ok and audio_ok, f"ami={dtmf[:40]}"))
        checks.append(("3CX endpoints", ep_ok, ep[:60] or "checked"))
    except Exception as e:
        checks.append(("Stack", False, str(e)[:100]))
    return checks


async def cmd_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Preflight the stack, then launch — the signature THE FLOOR start."""
    if not await guard(update, context):
        return
    msg = update.effective_message
    if not msg:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    s = session_for(update, context)
    if not s.numbers:
        await msg.reply_text(
            "📥 Hopper empty — paste numbers or drop a CSV, then /go.",
            reply_markup=_floor_pad(),
        )
        return
    status = await msg.reply_text("🛫 Running preflight…")
    checks = await _preflight_checks(chat_id, len(s.numbers))
    await status.edit_text(floor.preflight_card(checks), reply_markup=_floor_pad())
    if not all(ok for _, ok, _ in checks):
        return
    numbers = list(s.numbers)
    s.numbers.clear()
    await _launch_campaign(context, chat_id, user_id, numbers)


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    msg = update.effective_message
    if not msg:
        return
    s = session_for(update, context)
    if not s.numbers:
        await msg.reply_text("📥 Load leads first — paste a list or send a .csv.")
        return
    numbers = list(s.numbers)
    s.numbers.clear()
    await _launch_campaign(
        context,
        update.effective_chat.id,
        update.effective_user.id,
        numbers,
    )


async def on_floor_pad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline console — GO / PULSE / PAUSE / RESUME / STOP / TEST / DASH / ROUTE."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("floor:"):
        return
    if not await guard(update, context):
        return
    action = query.data.split(":", 1)[1]

    if action == "go":
        await query.answer("Preflight…")
        await cmd_go(update, context)
        return
    if action == "pulse":
        await query.answer("Pulse")
        await cmd_pulse(update, context)
        return
    if action == "pause":
        await query.answer("Pause")
        await cmd_pause(update, context)
        return
    if action == "unpause":
        await query.answer("Resume")
        await cmd_unpause(update, context)
        return
    if action == "stop":
        await query.answer("Stop")
        await cmd_stop(update, context)
        return
    if action == "test":
        await query.answer("Test call")
        await cmd_testcall(update, context)
        return
    if action == "dash":
        await query.answer("Dashboard")
        await cmd_dashboard(update, context)
        return
    if action == "settings":
        await query.answer("Route")
        await cmd_settings(update, context)
        return
    await query.answer()


_AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".ogg", ".opus", ".flac", ".aac")


def _set_awaiting_ivr(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data["awaiting_ivr_audio"] = True
    context.user_data["awaiting_ivr_audio"] = True


def _clear_awaiting_ivr(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data.pop("awaiting_ivr_audio", None)
    context.user_data.pop("awaiting_ivr_audio", None)
    context.chat_data.pop("ivr_audio_prompt_msg_id", None)


def _is_awaiting_ivr(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(
        context.chat_data.get("awaiting_ivr_audio")
        or context.user_data.get("awaiting_ivr_audio")
    )


def _is_ivr_audio_reply(update: Update) -> bool:
    """Group privacy mode: bots only see files sent as replies to bot messages."""
    msg = update.message
    if not msg or not _message_has_audio(msg):
        return False
    replied = msg.reply_to_message
    if not replied or not replied.from_user:
        return False
    return bool(replied.from_user.is_bot)


def _should_process_ivr_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if _is_awaiting_ivr(context):
        return True
    return _is_ivr_audio_reply(update)


def _message_has_audio(msg: Message) -> bool:
    if msg.voice or msg.audio:
        return True
    doc = msg.document
    if not doc or not doc.file_name:
        return False
    return doc.file_name.lower().endswith(_AUDIO_EXTS)


async def _download_audio_message(msg: Message, dest: Path) -> None:
    if msg.voice:
        tg_file = await msg.voice.get_file()
    elif msg.audio:
        tg_file = await msg.audio.get_file()
    elif msg.document:
        tg_file = await msg.document.get_file()
    else:
        raise ValueError("No audio in message")
    await tg_file.download_to_drive(str(dest))


async def _save_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, dest: Path) -> None:
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("🔊 Converting and uploading audio…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            sound_name = vd.chat_sound_name(chat_id)
            files = await asyncio.to_thread(
                convert_audio_for_asterisk,
                dest,
                Path(tmp),
                sound_name,
            )
            sound_name = await asyncio.to_thread(
                vd.deploy_chat_audio,
                chat_id,
                files,
                str((chat_progress(context.application, chat_id) or {}).get("run_id", "") or "") or None,
            )
        _clear_awaiting_ivr(context)
        await msg.edit_text(
            f"✅ IVR audio updated for this chat ({ui.code(sound_name)}).\n"
            "<i>Only campaigns started in this chat use this message.</i>"
        )
    except Exception as e:
        await msg.edit_text(ui.error(f"Audio upload failed: {e}"))
    finally:
        dest.unlink(missing_ok=True)


async def _deploy_ivr_from_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, source: Message
) -> None:
    if not _message_has_audio(source):
        await update.message.reply_text(
            "⚠️ No audio in that message. Send MP3, WAV, M4A, OGG, or a voice note."
        )
        return
    if source.voice:
        dest = Path(tempfile.gettempdir()) / f"voice_{source.message_id}.ogg"
    elif source.audio:
        dest = Path(tempfile.gettempdir()) / f"audio_{source.message_id}.mp3"
    else:
        dest = Path(tempfile.gettempdir()) / source.document.file_name
    await _download_audio_message(source, dest)
    await _save_audio(update, context, dest)


async def cmd_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    replied = update.message.reply_to_message
    if replied and _message_has_audio(replied):
        await _deploy_ivr_from_message(update, context, replied)
        return
    _set_awaiting_ivr(context)
    sent = await update.message.reply_text(
        ui.card(
            "🔊  CHANGE IVR AUDIO",
            [
                ui.note(
                    "↩️",
                    "<b>In groups:</b> reply to this message with your audio file.",
                ),
                ui.note("🎧", "Or send MP3, WAV, M4A, OGG, or a voice note now."),
                ui.note("↪️", "Or reply to an audio file with /audio."),
                ui.note("💬", "Applies to this chat only."),
            ],
        )
        + "\n💡 <i>Mono voice prompts sound best on phone lines.</i>"
    )
    context.chat_data["ivr_audio_prompt_msg_id"] = sent.message_id


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    if not _should_process_ivr_audio(update, context):
        return
    await _deploy_ivr_from_message(update, context, update.message)


async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    if not _should_process_ivr_audio(update, context):
        return
    await _deploy_ivr_from_message(update, context, update.message)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    doc = update.message.document
    if not doc or not doc.file_name:
        return
    name = doc.file_name.lower()
    if name.endswith(_AUDIO_EXTS):
        if not _should_process_ivr_audio(update, context):
            await update.message.reply_text(
                "⚠️ Run /audio, then <b>reply to the bot's message</b> with your file. "
                "In group chats Telegram does not deliver standalone uploads to bots."
            )
            return
        await _deploy_ivr_from_message(update, context, update.message)
        return
    if not name.endswith((".csv", ".txt")):
        return
    tg_file = await doc.get_file()
    dest = Path(tempfile.gettempdir()) / doc.file_name
    await tg_file.download_to_drive(str(dest))
    content = dest.read_bytes()
    nums = parse_csv(content) if name.endswith(".csv") else parse_numbers(
        content.decode("utf-8-sig", errors="replace")
    )
    dest.unlink(missing_ok=True)
    await _replace_session_leads(update, context, nums)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    nums = parse_numbers(text)
    if not nums:
        return
    await _replace_session_leads(update, context, nums)


async def _format_dtmf_message(ev: dict[str, str]) -> str | None:
    kind = ev.get("e", "")
    lead = ev.get("lead", "").strip() or "unknown"
    if kind == "digit":
        digit = ev.get("d", "")
        icon = "🔥" if digit == "1" else "☎️"
        return ui.card(
            f"{icon}  KEYPRESS · {lead}",
            [
                ui.bullet("Pressed", digit, icon="🔢"),
                ui.bullet("Sequence", ev.get("seq", ""), icon="🧮"),
            ],
        )
    if kind == "summary":
        digits = ev.get("digits", "")
        if not digits:
            return None
        return ui.card(
            f"📴  CALL ENDED · {lead}",
            [ui.bullet("All digits", digits, icon="🧮")],
        )
    return None


async def _dtmf_notify_loop(app: Application) -> None:
    while True:
        try:
            if access.OWNERS:
                chats = list(access.allowed_user_ids())
            else:
                chats = list(ALLOWED)
            if not chats:
                await asyncio.sleep(5)
                continue
            offset = int(app.bot_data.get("dtmf_offset", 0) or 0)
            events, offset = await asyncio.to_thread(vd.fetch_dtmf_events, offset)
            app.bot_data["dtmf_offset"] = offset
            for ev in events:
                text = await _format_dtmf_message(ev)
                if not text:
                    continue
                for chat_id in chats:
                    try:
                        await app.bot.send_message(chat_id=chat_id, text=text)
                    except Exception as e:
                        print(f"[press1] dtmf send to {chat_id}: {e}")
        except Exception as e:
            print(f"[press1] dtmf notify: {e}")
        await asyncio.sleep(0.8)


async def _dedup_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop only rapid duplicate deliveries (not Telegram retries after timeout)."""
    uid = update.update_id
    if uid is None:
        return
    seen: dict[int, float] = context.application.bot_data.setdefault("_seen_updates", {})
    now = time.time()
    if len(seen) > 500:
        cutoff = now - 3600
        for key, ts in list(seen.items()):
            if ts < cutoff:
                del seen[key]
    prev = seen.get(uid)
    if prev is not None and now - prev < 3:
        raise ApplicationHandlerStop()
    seen[uid] = now


async def _bootstrap_press1_stack() -> None:
    try:
        stack = await asyncio.to_thread(vd.bootstrap_press1_stack)
        print(f"[press1] stack ready: {stack['label']} (dialplan + dtmf + xfer sync)")
    except Exception as e:
        print(f"[press1] press1 stack warning: {e}")


async def _webhook_watchdog(app: Application) -> None:
    """Re-register webhook if another instance cleared it (polling conflict)."""
    if _use_polling_mode() and not _cloud_deployed():
        return
    url = _webhook_public_url()
    secret = _webhook_secret()
    while True:
        await asyncio.sleep(60)
        try:
            info = await app.bot.get_webhook_info()
            current = (info.url or "").strip()
            if current == url:
                continue
            await app.bot.set_webhook(
                url=url,
                secret_token=secret,
                drop_pending_updates=False,
                allowed_updates=["message", "callback_query"],
            )
            print(f"[press1] webhook re-registered: {url} (was {current!r})")
        except Exception as e:
            print(f"[press1] webhook watchdog: {e}")


async def _dtmf_watchdog_loop(app: Application) -> None:
    """Restart Press-1 AMI + audio DTMF listeners if either dies on the dial server."""
    while True:
        try:
            await asyncio.sleep(300)
            status = await asyncio.to_thread(
                vd.run_remote,
                "systemctl is-active press1-dtmf 2>/dev/null; "
                "systemctl is-active press1-audio-dtmf 2>/dev/null",
                15,
            )
            lines = [ln.strip().lower() for ln in status.splitlines() if ln.strip()]
            if lines.count("active") < 2:
                print(f"[press1] DTMF listener(s) down ({lines!r}) — restarting")
                out = await asyncio.to_thread(vd.ensure_dtmf_listener)
                print(f"[press1] DTMF restart: {out[:200]}")
        except Exception as e:
            print(f"[press1] dtmf watchdog: {e}")


async def post_init(app: Application) -> None:
    if _use_polling_mode() and not _cloud_deployed():
        await app.bot.delete_webhook(drop_pending_updates=True)
        print("[press1] polling mode (local / legacy)")
    else:
        webhook_url = _webhook_public_url()
        secret = _webhook_secret()
        await app.bot.set_webhook(
            url=webhook_url,
            secret_token=secret,
            drop_pending_updates=False,
            allowed_updates=["message", "callback_query"],
        )
        print(f"[press1] webhook active: {webhook_url}")
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Enter THE FLOOR"),
            BotCommand("go", "Preflight + launch campaign"),
            BotCommand("pulse", "Live conversion intel"),
            BotCommand("run", "Launch without preflight"),
            BotCommand("audio", "Change IVR audio"),
            BotCommand("status", "Hopper & live calls"),
            BotCommand("dashboard", "Pinned control room"),
            BotCommand("pause", "Pause campaign"),
            BotCommand("unpause", "Resume campaign"),
            BotCommand("stop", "Stop campaign"),
            BotCommand("testcall", "Ring test numbers"),
            BotCommand("testnumber", "Set UK test mobile"),
            BotCommand("settings", "Transfer target & options"),
            BotCommand("leads", "Loaded lead count"),
            BotCommand("clear", "Clear loaded numbers"),
            BotCommand("clearleads", "Clear loaded numbers"),
            BotCommand("schedule", "Schedule a campaign"),
            BotCommand("schedules", "List scheduled runs"),
            BotCommand("addkey", "Grant temporary access"),
            BotCommand("listkeys", "List access keys"),
            BotCommand("revokekey", "Revoke access"),
            BotCommand("repair", "Re-sync dial server stack"),
        ]
    )
    try:
        ping = await asyncio.to_thread(vd.ping)
        print(f"[press1] dial server SSH OK: {ping.strip()[:80]}")
    except Exception as e:
        print(f"[press1] dial server SSH warning: {e}")
    asyncio.create_task(_bootstrap_press1_stack())
    asyncio.create_task(_webhook_watchdog(app))
    app.bot_data["dtmf_offset"] = 0
    asyncio.create_task(_dtmf_notify_loop(app))
    asyncio.create_task(_dtmf_watchdog_loop(app))
    asyncio.create_task(_schedule_loop(app))
    asyncio.create_task(_resume_dashboards(app))


_conflict_logged = False


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _conflict_logged
    err = context.error
    if isinstance(err, Conflict):
        if not _conflict_logged:
            _conflict_logged = True
            print(
                "[press1] CONFLICT: another bot is using this token — "
                "duplicate replies likely. Stop VPS p1-dialer "
                "(systemctl stop p1-dialer on 167.99.193.119), "
                "do not run Q1/p1-dialer/start-bot.bat locally, "
                "and keep only one Render P1 service."
            )
        return
    print(f"[press1] error: {err}")


def build_application() -> Application:
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )
    app.add_handler(TypeHandler(Update, _dedup_update), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("go", cmd_go))
    app.add_handler(CommandHandler("pulse", cmd_pulse))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("unpause", cmd_unpause))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("testcall", cmd_testcall))
    app.add_handler(CommandHandler("testnumber", cmd_testnumber))
    app.add_handler(CommandHandler("leads", cmd_leads))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("clearleads", cmd_clear))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("unschedule", cmd_unschedule))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("addkey", cmd_addkey))
    app.add_handler(CommandHandler("listkeys", cmd_listkeys))
    app.add_handler(CommandHandler("revokekey", cmd_revokekey))
    app.add_handler(CommandHandler("repair", cmd_repair))
    app.add_handler(CommandHandler("audio", cmd_audio))
    app.add_handler(CommandHandler("setaudio", cmd_audio))
    app.add_handler(CallbackQueryHandler(on_floor_pad, pattern=r"^floor:"))
    app.add_handler(CallbackQueryHandler(on_threex_choice, pattern=r"^p1_3cx:"))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.AUDIO, on_audio))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app
