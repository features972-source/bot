"""Press-1 VICIdial Telegram bot handlers."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import vicidial_client as vd
from press1_settings import THREECX_PROFILES, format_settings_text
from press1_utils import convert_audio_for_asterisk, parse_csv, parse_numbers

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {
    int(x.strip())
    for x in os.getenv("TELEGRAM_ALLOWED_IDS", os.getenv("ADMIN_CHAT_ID", "")).split(",")
    if x.strip().isdigit()
}

HELP = """Press-1 dialer (BitCall + press1-ivr)

Send:
• Voice or MP3/WAV — IVR message (press 1)
• Numbers or .csv / .txt — lead list

Commands:
/start — this help
/status — live calls & progress
/run — dial loaded leads (same path as /testcall)
/stop — stop dialing
/testcall — ring test numbers
/settings — 3CX target & dialer options
/leads — lead count in session
/clear — clear loaded numbers"""


@dataclass
class Session:
    numbers: list[str] = field(default_factory=list)


def session(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> Session:
    key = "press1_session"
    if key not in context.application.bot_data:
        context.application.bot_data[key] = {}
    store: dict[int, Session] = context.application.bot_data[key]
    if user_id not in store:
        store[user_id] = Session()
    return store[user_id]


def allowed(user_id: int) -> bool:
    return not ALLOWED or user_id in ALLOWED


async def guard(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    if not allowed(uid):
        if update.message:
            await update.message.reply_text("Access denied.")
        return False
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
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
    "finishing": "🟡 Finishing (list done, calls in flight)",
    "finished": "✅ Finished",
    "stalled": "⚠️ Stopped early",
    "idle": "⚪ Idle",
}


def _state_line(st: dict[str, str]) -> str:
    label = _STATE_LABELS.get(st.get("dial_state", ""), "⚪ Unknown")
    return f"Status: {label}"


async def _format_live_stats(
    st: dict[str, str],
    total_leads: int,
    *,
    finished: bool = False,
) -> str:
    total = int(st.get("list_size", 0) or 0) or total_leads
    dialed = st.get("dialed", "0")
    answered = st.get("answered", "0")
    press1 = st.get("press1", "0")
    left = st.get("hopper", "0")
    live = st.get("live", "0")
    failed = st.get("failed", "0")
    dial_state = st.get("dial_state", "")
    if finished or dial_state in ("finished", "stalled"):
        header = "✅ Campaign finished"
    elif dial_state == "finishing":
        header = f"🟡 Campaign finishing — {total} leads"
    else:
        header = f"📊 Campaign live — {total} leads"
    dialed_n = int(dialed or 0)
    pct = (dialed_n * 100 // total) if total > 0 else 0
    lines = [
        header + "\n",
        f"📞 Dialed: {dialed} / {total} ({pct}%)",
        f"⏳ Left: {left}",
        f"📡 Live now: {live}",
        f"✅ Answered: {answered}",
        f"🔥 Press-1: {press1}",
    ]
    if int(failed or 0) > 0:
        lines.append(f"❌ Failed: {failed}")
    if not finished:
        lines.append(_state_line(st))
    return "\n".join(lines)


async def _format_status(st: dict[str, str], loaded_in_bot: int) -> str:
    total = int(st.get("list_size", 0) or 0)
    dialed = st.get("dialed", "?")
    answered = st.get("answered", "?")
    press1 = st.get("press1", "?")
    left = st.get("hopper", "?")
    live = st.get("live", "?")
    failed = st.get("failed", "0")
    dialed_n = int(dialed or 0) if str(dialed).isdigit() else 0
    pct = (dialed_n * 100 // total) if total > 0 else 0
    lines = [
        "📊 Server status\n",
        f"📋 List on server: {total}",
        f"📞 Dialed: {dialed} / {total}" + (f" ({pct}%)" if total > 0 else ""),
        f"⏳ Left: {left}",
        f"📡 Live now: {live}",
        f"✅ Answered: {answered}",
        f"🔥 Press-1: {press1}",
    ]
    if int(failed or 0) > 0:
        lines.append(f"❌ Failed: {failed}")
    lines.append(_state_line(st))
    if loaded_in_bot != total:
        lines.append(f"💾 In bot session: {loaded_in_bot}")
    return "\n".join(lines)


async def _live_campaign_updater(
    msg: Message,
    total_leads: int,
    run_since: str,
    stop_event: asyncio.Event,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    last_text = ""
    idle_rounds = 0
    progress = context.application.bot_data.get("dial_progress", {})
    while not stop_event.is_set():
        try:
            st = await asyncio.to_thread(vd.get_dial_stats, run_since, progress)
            err = progress.get("error")
            text = await _format_live_stats(st, total_leads)
            if progress.get("stalled"):
                text += "\n\n⚠️ Dialer stopped early on server — upload a new list and /run"
            if err:
                text += f"\n\n⚠️ {err}"
            hopper = int(st.get("hopper", 0) or 0)
            live = int(st.get("live", 0) or 0)
            dial_state = st.get("dial_state", "")
            active = dial_state == "running"
            dialed = int(st.get("dialed", 0) or 0)
            if text != last_text:
                await _safe_edit(msg, text)
                last_text = text
            if dial_state in ("finished", "stalled") and hopper == 0:
                idle_rounds += 1
                if idle_rounds >= 2:
                    final = await _format_live_stats(st, total_leads, finished=True)
                    err = progress.get("error")
                    if err:
                        final += f"\n\n⚠️ {err}"
                    try:
                        await _safe_edit(msg, final)
                    except BadRequest:
                        pass
                    break
            elif dial_state == "finishing" and hopper == 0 and live == 0:
                idle_rounds += 1
                if idle_rounds >= 3:
                    final = await _format_live_stats(st, total_leads, finished=True)
                    try:
                        await _safe_edit(msg, final)
                    except BadRequest:
                        pass
                    break
            elif dial_state == "stalled" and hopper > 0 and dialed > 0:
                idle_rounds += 1
                if idle_rounds >= 4:
                    final = await _format_live_stats(st, total_leads, finished=True)
                    final += "\n\n⚠️ Dialer stopped early on server — upload a new list and /run"
                    try:
                        await _safe_edit(msg, final)
                    except BadRequest:
                        pass
                    break
            elif not active and dialed == 0 and idle_rounds >= 6:
                final = await _format_live_stats(st, total_leads, finished=True)
                err = progress.get("error") or "Dialer never started — try /run again"
                final += f"\n\n⚠️ {err}"
                try:
                    await _safe_edit(msg, final)
                except BadRequest:
                    pass
                break
            else:
                idle_rounds = 0 if active or dialed > 0 else idle_rounds + 1
        except Exception as e:
            try:
                await msg.edit_text(f"Live update error: {e}")
            except Exception:
                pass
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            break
        except asyncio.TimeoutError:
            pass
    progress["running"] = False


def _stop_dialer(context: ContextTypes.DEFAULT_TYPE) -> None:
    progress = context.application.bot_data.get("dial_progress")
    if progress:
        progress["stop"] = True
    try:
        vd._stop_remote_dialer()
    except Exception:
        pass
    task = context.application.bot_data.get("dial_task")
    if task and not task.done():
        task.cancel()
    context.application.bot_data.pop("dial_task", None)
def _stop_live_updater(context: ContextTypes.DEFAULT_TYPE) -> None:
    _stop_dialer(context)
    task = context.application.bot_data.get("live_updater_task")
    stop = context.application.bot_data.get("live_updater_stop")
    if stop:
        stop.set()
    if task and not task.done():
        task.cancel()
    context.application.bot_data.pop("live_updater_task", None)
    context.application.bot_data.pop("live_updater_stop", None)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    msg = await update.message.reply_text("Checking server…")
    try:
        st = await asyncio.to_thread(vd.get_dial_stats, None, context.application.bot_data.get("dial_progress"))
        s = session(update.effective_user.id, context)
        text = await _format_status(st, len(s.numbers))
        await msg.edit_text(text)
    except Exception as e:
        await msg.edit_text(f"Status failed: {e}")


async def cmd_leads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    s = session(update.effective_user.id, context)
    await update.message.reply_text(f"{len(s.numbers)} numbers loaded. Send more or /run.")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    session(update.effective_user.id, context).numbers.clear()
    await update.message.reply_text("Cleared loaded numbers.")


def _threex_keyboard(active_id: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for pid, info in THREECX_PROFILES.items():
        mark = " ✅" if pid == active_id else ""
        rows.append(
            [InlineKeyboardButton(f"{info['label']}{mark}", callback_data=f"p1_3cx:{pid}")]
        )
    return InlineKeyboardMarkup(rows)


def _settings_message() -> tuple[str, InlineKeyboardMarkup]:
    summary = vd.settings_summary()
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
    if not await guard(update):
        return
    msg = await update.message.reply_text("Loading settings…")
    try:
        text, keyboard = await asyncio.to_thread(_settings_message)
        await msg.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        await msg.edit_text(f"Settings failed: {e}")


async def on_threex_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    if not allowed(query.from_user.id if query.from_user else 0):
        await query.answer("Access denied.", show_alert=True)
        return
    if not query.data.startswith("p1_3cx:"):
        return
    profile_id = query.data.split(":", 1)[1]
    await query.answer("Updating 3CX target…")
    try:
        p = await asyncio.to_thread(vd.apply_threex_target, profile_id)
        text, keyboard = await asyncio.to_thread(_settings_message)
        note = f"\n\n✅ Press-1 calls now transfer to {p['label']} ({p['fqdn']})."
        await query.edit_message_text(text + note, reply_markup=keyboard)
    except Exception as e:
        await query.edit_message_text(f"3CX update failed: {e}")


async def cmd_testcall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    msg = await update.message.reply_text("Placing test calls…")
    try:
        placed = await asyncio.to_thread(vd.test_calls)
        await msg.edit_text("Test calls placed:\n" + "\n".join(f"• {n}" for n in placed))
    except Exception as e:
        await msg.edit_text(f"Test call failed: {e}")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    _stop_live_updater(context)
    msg = await update.message.reply_text("Stopping dialer…")
    await msg.edit_text("Dialer stopped.")


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    s = session(update.effective_user.id, context)
    if not s.numbers:
        await update.message.reply_text("Load numbers first (paste list or send .csv).")
        return
    _stop_live_updater(context)
    numbers = list(s.numbers)
    count = len(numbers)
    s.numbers.clear()
    msg = await update.message.reply_text(
        f"Dialling {count} leads — {vd.BATCH_SIZE}/batch, "
        f"{vd.CALL_GAP_SEC:g}s between calls, {vd.BATCH_PAUSE_SEC}s between batches…"
    )
    try:
        run_since = await asyncio.to_thread(vd.server_now)
        # Full reset so a new /run can never display the previous run's numbers.
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
        }
        context.application.bot_data["dial_progress"] = progress

        try:
            await asyncio.to_thread(vd.launch_dial_campaign, numbers, progress)
        except Exception as e:
            progress["error"] = str(e)
            progress["running"] = False
            st = await asyncio.to_thread(vd.get_dial_stats, run_since, progress)
            await _safe_edit(
                msg,
                await _format_live_stats(st, count) + f"\n\n⚠️ {e}",
            )
            return

        # Render a guaranteed-clean zeroed frame first so the new run never flashes
        # the previous run's totals while the dialer spins up. The live updater then
        # takes over with real server counters a few seconds later.
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
        await _safe_edit(msg, await _format_live_stats(fresh, count))

        stop_event = asyncio.Event()
        context.application.bot_data["live_updater_stop"] = stop_event
        task = asyncio.create_task(
            _live_campaign_updater(msg, count, run_since, stop_event, context)
        )
        context.application.bot_data["live_updater_task"] = task
    except Exception as e:
        try:
            await _safe_edit(msg, f"Run failed: {e}")
        except Exception:
            await update.message.reply_text(f"Run failed: {e}")


async def _save_audio(update: Update, context: ContextTypes.DEFAULT_TYPE, dest: Path) -> None:
    msg = await update.message.reply_text("Converting and uploading audio…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            files = await asyncio.to_thread(
                convert_audio_for_asterisk,
                dest,
                Path(tmp),
                vd.SOUND_NAME,
            )
            await asyncio.to_thread(vd.deploy_audio, files)
        await msg.edit_text(f"Audio updated ({vd.SOUND_NAME}). New calls will use it.")
    except Exception as e:
        await msg.edit_text(f"Audio upload failed: {e}")
    finally:
        dest.unlink(missing_ok=True)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    tg_file = await update.message.voice.get_file()
    dest = Path(tempfile.gettempdir()) / f"voice_{update.message.message_id}.ogg"
    await tg_file.download_to_drive(str(dest))
    await _save_audio(update, context, dest)


async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    attachment = update.message.audio or update.message.document
    if not attachment:
        return
    name = getattr(attachment, "file_name", None) or f"audio_{update.message.message_id}"
    tg_file = await attachment.get_file()
    dest = Path(tempfile.gettempdir()) / name
    await tg_file.download_to_drive(str(dest))
    await _save_audio(update, context, dest)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    doc = update.message.document
    if not doc or not doc.file_name:
        return
    name = doc.file_name.lower()
    if name.endswith((".mp3", ".wav", ".m4a", ".ogg")):
        await on_audio(update, context)
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
    s = session(update.effective_user.id, context)
    s.numbers = list(dict.fromkeys(s.numbers + nums))
    dest.unlink(missing_ok=True)
    await update.message.reply_text(
        f"Loaded {len(nums)} numbers ({len(s.numbers)} total). /run to dial."
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    nums = parse_numbers(text)
    if not nums:
        return
    s = session(update.effective_user.id, context)
    s.numbers = list(dict.fromkeys(s.numbers + nums))
    await update.message.reply_text(
        f"Added {len(nums)} numbers ({len(s.numbers)} total). /run to dial."
    )


async def post_init(app: Application) -> None:
    # Only one process may poll this token (Render OR local — not both).
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Help"),
            BotCommand("status", "Hopper & live calls"),
            BotCommand("run", "Start campaign"),
            BotCommand("stop", "Pause campaign"),
            BotCommand("testcall", "Ring test numbers"),
            BotCommand("settings", "3CX target & options"),
            BotCommand("leads", "Loaded lead count"),
            BotCommand("clear", "Clear loaded numbers"),
        ]
    )
    try:
        ping = await asyncio.to_thread(vd.ping)
        print(f"[press1] VICIdial SSH OK: {ping.strip()[:80]}")
    except Exception as e:
        print(f"[press1] VICIdial SSH warning: {e}")
    try:
        p = await asyncio.to_thread(vd.ensure_threex_target)
        print(f"[press1] 3CX target: {p['label']} ({p['fqdn']})")
    except Exception as e:
        print(f"[press1] 3CX settings warning: {e}")


_conflict_logged = False


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _conflict_logged
    err = context.error
    if isinstance(err, Conflict):
        if not _conflict_logged:
            _conflict_logged = True
            print(
                "[press1] CONFLICT: two bots share this token. "
                "Keep ONE Render service (p1-bot OR p1-telegram-bot, not both) "
                "and stop local start-bot.bat."
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
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("testcall", cmd_testcall))
    app.add_handler(CommandHandler("leads", cmd_leads))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(on_threex_choice, pattern=r"^p1_3cx:"))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.AUDIO, on_audio))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app
