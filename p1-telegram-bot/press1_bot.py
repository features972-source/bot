"""Press-1 VICIdial Telegram bot handlers."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from telegram import BotCommand, Message, Update
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import vicidial_client as vd
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


async def _format_live_stats(
    st: dict[str, str],
    total_leads: int,
    *,
    finished: bool = False,
) -> str:
    dialed = st.get("dialed", "0")
    answered = st.get("answered", "0")
    press1 = st.get("press1", "0")
    left = st.get("hopper", "0")
    live = st.get("live", "0")
    header = "✅ Campaign finished" if finished else "📊 Campaign live"
    return (
        f"{header} — {total_leads} leads\n\n"
        f"📞 Calls Made: {dialed}\n"
        f"⏳ Left to dial: {left}\n"
        f"📡 Live now: {live}\n"
        f"✅ Answered: {answered}\n"
        f"🔥 Press-1: {press1}"
    )


async def _format_status(st: dict[str, str], loaded_in_bot: int) -> str:
    """Shorter view for /status (today's totals)."""
    dialed = st.get("dialed", "?")
    answered = st.get("answered", "?")
    press1 = st.get("press1", "?")
    left = st.get("hopper", "?")
    live = st.get("live", "?")
    active = st.get("campaign_active", "?")
    return (
        f"📊 Server status\n\n"
        f"📞 Calls Made (today): {dialed}\n"
        f"⏳ Left to dial: {left}\n"
        f"📡 Live now: {live}\n"
        f"✅ Answered (today): {answered}\n"
        f"🔥 Press-1 (today): {press1}\n"
        f"Campaign active: {active}\n"
        f"Loaded in bot: {loaded_in_bot}"
    )


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
            if err:
                text += f"\n\n⚠️ {err}"
            hopper = int(st.get("hopper", 0) or 0)
            live = int(st.get("live", 0) or 0)
            active = (st.get("campaign_active") or "N").upper() == "Y"
            dialed = int(st.get("dialed", 0) or 0)
            if text != last_text:
                try:
                    await msg.edit_text(text)
                    last_text = text
                except Exception:
                    pass
            if not active and hopper == 0:
                idle_rounds += 1
                if idle_rounds >= 2:
                    final = await _format_live_stats(st, total_leads, finished=True)
                    err = progress.get("error")
                    if err:
                        final += f"\n\n⚠️ {err}"
                    try:
                        await msg.edit_text(final)
                    except Exception:
                        pass
                    break
            elif not active and dialed == 0 and idle_rounds >= 6:
                final = await _format_live_stats(st, total_leads, finished=True)
                err = progress.get("error") or "Dialer never started — try /run again"
                final += f"\n\n⚠️ {err}"
                try:
                    await msg.edit_text(final)
                except Exception:
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
        progress = context.application.bot_data.get("dial_progress")
        if progress and progress.get("running"):
            st = await asyncio.to_thread(vd.get_dial_stats, None, progress)
        else:
            st = await asyncio.to_thread(vd.get_dial_stats, None, {})
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
        f"Dialling {count} leads — {vd.BATCH_SIZE} per batch, {vd.BATCH_PAUSE_SEC}s pause…"
    )
    try:
        run_since = await asyncio.to_thread(vd.server_now)
        progress: dict = {
            "started": 0,
            "failed": 0,
            "total": count,
            "running": True,
            "stop": False,
        }
        context.application.bot_data["dial_progress"] = progress
        st = await asyncio.to_thread(vd.get_dial_stats, run_since, progress)
        await msg.edit_text(await _format_live_stats(st, count))

        async def dial_worker() -> None:
            try:
                await asyncio.to_thread(vd.launch_dial_campaign, numbers, progress)
            except Exception as e:
                progress["error"] = str(e)
                progress["running"] = False

        dial_task = asyncio.create_task(dial_worker())
        context.application.bot_data["dial_task"] = dial_task

        # Brief wait so launch errors surface before live stats poll
        await asyncio.sleep(3)
        st = await asyncio.to_thread(vd.get_dial_stats, run_since, progress)
        if progress.get("error"):
            await msg.edit_text(
                await _format_live_stats(st, count) + f"\n\n⚠️ {progress['error']}"
            )
            return
        await msg.edit_text(await _format_live_stats(st, count))

        stop_event = asyncio.Event()
        context.application.bot_data["live_updater_stop"] = stop_event
        task = asyncio.create_task(
            _live_campaign_updater(msg, count, run_since, stop_event, context)
        )
        context.application.bot_data["live_updater_task"] = task
    except Exception as e:
        await msg.edit_text(f"Run failed: {e}")


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
            BotCommand("leads", "Loaded lead count"),
            BotCommand("clear", "Clear loaded numbers"),
        ]
    )
    try:
        ping = await asyncio.to_thread(vd.ping)
        print(f"[press1] VICIdial SSH OK: {ping.strip()[:80]}")
    except Exception as e:
        print(f"[press1] VICIdial SSH warning: {e}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        print(
            "[press1] CONFLICT: another bot instance is using this token. "
            "Stop local start-bot.bat and keep only one Render service."
        )
        await context.application.stop()


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
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.AUDIO, on_audio))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app
