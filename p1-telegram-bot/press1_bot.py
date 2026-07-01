"""Press-1 VICIdial Telegram bot handlers."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from telegram import BotCommand, Message, Update
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

HELP = """Press-1 dialer (VICIdial + BitCall)

Send:
• Voice or MP3/WAV — IVR message (press 1)
• Numbers or .csv / .txt — lead list

Commands:
/start — this help
/status — hopper, live calls, leads
/run — upload leads & start campaign (live updates every 5s)
/stop — pause campaign
/testcall — ring both test numbers
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


async def _format_live_stats(st: dict[str, str], total_leads: int | None = None) -> str:
    hopper = st.get("hopper", "?")
    live = st.get("live", "?")
    dialed = st.get("dialed_today", "?")
    answered = st.get("answered_today", "?")
    press1 = st.get("press1_today", "?")
    active = st.get("campaign_active", "?")
    lines = [
        "📞 Campaign live",
        f"Live calls: {live}/{vd.MAX_CONCURRENT}",
        f"Hopper left: {hopper}",
        f"Calls today: {dialed}",
        f"Answered (5s+): {answered}",
        f"Press-1: {press1}",
        f"Campaign active: {active}",
    ]
    if total_leads is not None:
        lines.append(f"Leads uploaded: {total_leads}")
    return "\n".join(lines)


async def _live_campaign_updater(
    msg: Message,
    total_leads: int,
    stop_event: asyncio.Event,
) -> None:
    last_text = ""
    idle_rounds = 0
    while not stop_event.is_set():
        try:
            st = await asyncio.to_thread(vd.get_live_stats)
            text = await _format_live_stats(st, total_leads)
            hopper = int(st.get("hopper", 0) or 0)
            live = int(st.get("live", 0) or 0)
            active = (st.get("campaign_active") or "").upper()
            if text != last_text:
                try:
                    await msg.edit_text(text)
                    last_text = text
                except Exception:
                    pass
            if active == "N" or (hopper == 0 and live == 0):
                idle_rounds += 1
                if idle_rounds >= 6:
                    await msg.edit_text(text + "\n\n✅ Hopper empty — dialling finished.")
                    break
            else:
                idle_rounds = 0
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


def _stop_live_updater(context: ContextTypes.DEFAULT_TYPE) -> None:
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
        st = await asyncio.to_thread(vd.get_live_stats)
        s = session(update.effective_user.id, context)
        text = await _format_live_stats(st) + f"\nLoaded in bot: {len(s.numbers)}"
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
    msg = await update.message.reply_text("Stopping campaign…")
    try:
        await asyncio.to_thread(vd.stop_campaign)
        await msg.edit_text("Campaign paused.")
    except Exception as e:
        await msg.edit_text(f"Stop failed: {e}")


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    s = session(update.effective_user.id, context)
    if not s.numbers:
        await update.message.reply_text("Load numbers first (paste list or send .csv).")
        return
    _stop_live_updater(context)
    msg = await update.message.reply_text(
        f"Uploading {len(s.numbers)} leads and starting campaign…"
    )
    try:
        count = await asyncio.to_thread(vd.add_leads, list(s.numbers))
        await asyncio.to_thread(vd.start_campaign)
        s.numbers.clear()
        st = await asyncio.to_thread(vd.get_live_stats)
        await msg.edit_text(await _format_live_stats(st, count))
        stop_event = asyncio.Event()
        context.application.bot_data["live_updater_stop"] = stop_event
        task = asyncio.create_task(_live_campaign_updater(msg, count, stop_event))
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
    return app
