"""Press-1 Telegram bot handlers."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict
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
import press1_schedule as schedule
import press1_ui as ui
from press1_settings import THREECX_PROFILES, format_settings_text
from press1_utils import convert_audio_for_asterisk, parse_csv, parse_numbers

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED = access.OWNERS | {
    int(x.strip())
    for x in os.getenv("TELEGRAM_ALLOWED_IDS", os.getenv("ADMIN_CHAT_ID", "")).split(",")
    if x.strip().isdigit()
}

HELP = (
    ui.card(
        "⚡  PRESS-1 DIALER",
        [
            ui.note("📥", "Paste numbers or send a .csv / .txt to load leads."),
            "",
            "🚀 <b>CAMPAIGN</b>",
            ui.bullet("/run", "dial the loaded leads", icon="▪️"),
            ui.bullet("/dashboard", "pinned live control room", icon="▪️"),
            ui.bullet("/status", "quick snapshot", icon="▪️"),
            ui.bullet("/pause", "hold new calls (this chat only)", icon="▪️"),
            ui.bullet("/unpause", "resume this chat's campaign", icon="▪️"),
            ui.bullet("/stop", "end this chat's campaign", icon="▪️"),
            ui.bullet("/testcall", "ring the test numbers", icon="▪️"),
            "",
            "⏰ <b>SCHEDULE</b>",
            ui.bullet("/schedule 9am", "run at a set time", icon="▪️"),
            ui.bullet("/schedule tomorrow 10:30", "run tomorrow", icon="▪️"),
            ui.bullet("/schedules", "list upcoming runs", icon="▪️"),
            ui.bullet("/unschedule", "cancel a run", icon="▪️"),
            "",
            "🎛 <b>SETUP</b>",
            ui.bullet("/audio", "change this chat's IVR message", icon="▪️"),
            ui.bullet("/settings", "transfer target for this chat", icon="▪️"),
            ui.bullet("/leads", "loaded lead count", icon="▪️"),
            ui.bullet("/clear", "clear loaded numbers", icon="▪️"),
            "",
            "🔐 <b>ACCESS</b> (owner only)",
            ui.bullet("/addkey @user 24h", "grant temporary access", icon="▪️"),
            ui.bullet("/listkeys", "active access keys", icon="▪️"),
            ui.bullet("/revokekey @user", "revoke access", icon="▪️"),
        ],
        expandable=True,
    )
    + "\n🔔 <i>Each group chat runs its own campaign. /pause and /unpause only affect that chat.</i>"
    + "\n🔔 <i>Every key a caller presses is streamed here live.</i>"
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
    user = update.effective_user
    if user:
        await asyncio.to_thread(
            access.remember_user, user.id, user.username, user.full_name
        )
    await update.message.reply_text(HELP)


async def _safe_edit(msg: Message, text: str) -> None:
    """Edit message; ignore Telegram 'message is not modified' (same text)."""
    try:
        await msg.edit_text(text)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise


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
    data = {
        "call_gap": float(summary["call_gap"]),
        "batch_size": int(summary["batch_size"]),
        "batch_pause": int(summary["batch_pause"]),
        "max_concurrent": int(summary["max_concurrent"]),
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
    pacing = _pacing(int(prog.get("chat_id", 0) or 0) or None)
    prog = progress or {}
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
    while not stop_event.is_set():
        try:
            progress = chat_progress(context.application, chat_id) or {}
            progress["_frame"] = frame
            frame += 1
            st = await asyncio.to_thread(vd.get_dial_stats, run_since, progress)
            err = progress.get("error")
            text = await _format_live_stats(st, total_leads, progress=progress)
            if progress.get("stalled"):
                text += _warn("Dialer stopped early on server — upload a new list and /run")
            if err:
                text += _warn(err)
            hopper = int(st.get("hopper", 0) or 0)
            live = int(st.get("live", 0) or 0)
            dial_state = st.get("dial_state", "")
            active = dial_state in ("running", "paused")
            dialed = int(st.get("dialed", 0) or 0)
            if text != last_text:
                await _safe_edit(msg, text)
                last_text = text
            if dial_state in ("finished", "stalled") and hopper == 0:
                idle_rounds += 1
                if idle_rounds >= 2:
                    final = await _format_live_stats(st, total_leads, finished=True, progress=progress)
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
                    final = await _format_live_stats(st, total_leads, finished=True, progress=progress)
                    try:
                        await _safe_edit(msg, final)
                    except BadRequest:
                        pass
                    break
            elif dial_state == "stalled" and hopper > 0 and dialed > 0:
                idle_rounds += 1
                if idle_rounds >= 4:
                    final = await _format_live_stats(st, total_leads, finished=True, progress=progress)
                    final += _warn("Dialer stopped early on server — upload a new list and /run")
                    try:
                        await _safe_edit(msg, final)
                    except BadRequest:
                        pass
                    break
            elif not active and dialed == 0 and idle_rounds >= 6:
                final = await _format_live_stats(st, total_leads, finished=True, progress=progress)
                err = progress.get("error") or "Dialer never started — try /run again"
                final += _warn(err)
                try:
                    await _safe_edit(msg, final)
                except BadRequest:
                    pass
                break
            else:
                idle_rounds = 0 if active or dialed > 0 else idle_rounds + 1
        except Exception as e:
            try:
                await msg.edit_text(ui.error(f"Live update error: {e}"))
            except Exception:
                pass
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
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
        f"💾 {len(s.numbers)} leads loaded. Send more or /run."
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    session_for(update, context).numbers.clear()
    await update.message.reply_text("🧹 Loaded leads cleared.")


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
    msg = await update.message.reply_text("⚙️ Loading settings…")
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


async def cmd_testcall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    msg = await update.message.reply_text("📞 Placing test calls…")
    try:
        placed = await asyncio.to_thread(vd.test_calls, None, update.effective_chat.id)
        card = ui.card(
            "📞  TEST CALLS PLACED",
            [ui.bullet(n, "", icon="☎️") for n in placed] or [ui.note("⚪", "none")],
        )
        await msg.edit_text(card)
    except Exception as e:
        await msg.edit_text(ui.error(f"Test call failed: {e}"))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await control_guard(update, context):
        return
    chat_id = update.effective_chat.id
    _stop_live_updater(context, chat_id)
    msg = await update.message.reply_text("🛑 Stopping campaign…")
    await msg.edit_text("🛑 Campaign stopped in this chat.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await control_guard(update, context):
        return
    chat_id = update.effective_chat.id
    progress = chat_progress(context.application, chat_id)
    run_id = str((progress or {}).get("run_id", "") or "")
    if not run_id:
        await update.message.reply_text(ui.error("No active campaign in this chat."))
        return
    msg = await update.message.reply_text("⏸ Pausing campaign…")
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
            + "\n<i>Live calls will finish · /unpause to continue.</i>"
        )
    except Exception as e:
        await msg.edit_text(ui.error(f"Pause failed: {e}"))


async def cmd_unpause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await control_guard(update, context):
        return
    chat_id = update.effective_chat.id
    progress = chat_progress(context.application, chat_id)
    run_id = str((progress or {}).get("run_id", "") or "")
    if not run_id:
        await update.message.reply_text(ui.error("No active campaign in this chat."))
        return
    msg = await update.message.reply_text("▶️ Resuming campaign…")
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
    try:
        await app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
        return True
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            return True
        if "message to edit not found" in msg or "message can't be edited" in msg:
            return False
        return True
    except Exception:
        return True


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
            await asyncio.wait_for(stop_event.wait(), timeout=3.0)
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
    app = context.application

    # Retire any previous pinned dashboard in this chat.
    old = context.application.bot_data.get("dashboards", {}).get(chat_id)
    if old and old.get("msg_id"):
        try:
            await context.bot.unpin_chat_message(chat_id, old["msg_id"])
        except Exception:
            pass

    msg = await update.message.reply_text("🎛  Booting control room…")
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
    text_intro = intro or (
        f"🚀 Launching {count} leads\n"
        f"{vd.BATCH_SIZE}/batch · {vd.CALL_GAP_SEC:g}s gap · "
        f"{vd.BATCH_PAUSE_SEC}s between batches…"
    )
    msg = await context.bot.send_message(chat_id, text_intro)
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
                if not access.is_allowed(uid) or not numbers:
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


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    s = session_for(update, context)
    if not s.numbers:
        await update.message.reply_text("📥 Load leads first — paste a list or send a .csv.")
        return
    numbers = list(s.numbers)
    s.numbers.clear()
    await _launch_campaign(
        context,
        update.effective_chat.id,
        update.effective_user.id,
        numbers,
    )


_AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".ogg", ".opus", ".flac", ".aac")


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
            sound_name = await asyncio.to_thread(vd.deploy_chat_audio, chat_id, files)
        context.user_data.pop("awaiting_ivr_audio", None)
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
    context.user_data["awaiting_ivr_audio"] = True
    await update.message.reply_text(
        ui.card(
            "🔊  CHANGE IVR AUDIO",
            [
                ui.note("🎧", "Send an MP3, WAV, M4A, OGG, or voice note now,"),
                ui.note("↩️", "or reply to an audio file with /audio."),
                ui.note("💬", "Applies to this chat only."),
            ],
        )
        + "\n💡 <i>Mono voice prompts sound best on phone lines.</i>"
    )


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    if not context.user_data.get("awaiting_ivr_audio"):
        return
    await _deploy_ivr_from_message(update, context, update.message)


async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    if not context.user_data.get("awaiting_ivr_audio"):
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
        if not context.user_data.get("awaiting_ivr_audio"):
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
    s = session_for(update, context)
    s.numbers = list(dict.fromkeys(s.numbers + nums))
    dest.unlink(missing_ok=True)
    await update.message.reply_text(
        f"📥 Loaded {len(nums)} leads ({len(s.numbers)} total). /run to dial."
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    nums = parse_numbers(text)
    if not nums:
        return
    s = session_for(update, context)
    s.numbers = list(dict.fromkeys(s.numbers + nums))
    await update.message.reply_text(
        f"📥 Added {len(nums)} leads ({len(s.numbers)} total). /run to dial."
    )


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
        await asyncio.sleep(2)


async def _dedup_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ignore the same Telegram update if two bot instances overlap briefly."""
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
    if uid in seen:
        raise ApplicationHandlerStop()
    seen[uid] = now


async def post_init(app: Application) -> None:
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if webhook_url:
        secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip() or None
        await app.bot.set_webhook(
            url=webhook_url,
            secret_token=secret,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        print(f"[press1] webhook active: {webhook_url}")
    else:
        await app.bot.delete_webhook(drop_pending_updates=True)
        print("[press1] polling mode (local / legacy)")
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Help"),
            BotCommand("audio", "Change IVR audio"),
            BotCommand("status", "Hopper & live calls"),
            BotCommand("dashboard", "Live control panel"),
            BotCommand("run", "Start campaign"),
            BotCommand("pause", "Pause campaign"),
            BotCommand("unpause", "Resume campaign"),
            BotCommand("stop", "Stop campaign"),
            BotCommand("testcall", "Ring test numbers"),
            BotCommand("settings", "Transfer target & options"),
            BotCommand("leads", "Loaded lead count"),
            BotCommand("clear", "Clear loaded numbers"),
            BotCommand("schedule", "Schedule a campaign"),
            BotCommand("schedules", "List scheduled runs"),
            BotCommand("addkey", "Grant temporary access"),
            BotCommand("listkeys", "List access keys"),
            BotCommand("revokekey", "Revoke access"),
        ]
    )
    try:
        ping = await asyncio.to_thread(vd.ping)
        print(f"[press1] dial server SSH OK: {ping.strip()[:80]}")
    except Exception as e:
        print(f"[press1] dial server SSH warning: {e}")
    try:
        stack = await asyncio.to_thread(vd.ensure_press1_stack)
        print(f"[press1] transfer target: {stack['label']} (dialplan + dtmf listener applied)")
    except Exception as e:
        print(f"[press1] press1 stack warning: {e}")
    app.bot_data["dtmf_offset"] = 0
    asyncio.create_task(_dtmf_notify_loop(app))
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
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("unpause", cmd_unpause))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("testcall", cmd_testcall))
    app.add_handler(CommandHandler("leads", cmd_leads))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("unschedule", cmd_unschedule))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("addkey", cmd_addkey))
    app.add_handler(CommandHandler("listkeys", cmd_listkeys))
    app.add_handler(CommandHandler("revokekey", cmd_revokekey))
    app.add_handler(CommandHandler("audio", cmd_audio))
    app.add_handler(CommandHandler("setaudio", cmd_audio))
    app.add_handler(CallbackQueryHandler(on_threex_choice, pattern=r"^p1_3cx:"))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.AUDIO, on_audio))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app
