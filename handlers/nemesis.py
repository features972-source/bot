"""Payment nemesis rivalry — periodic head-to-head updates in group chat."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime

from telegram.error import BadRequest, Forbidden
from telegram.ext import CommandHandler, ContextTypes

from config import Settings
from database import (
    PaymentNemesis,
    clear_payment_nemesis,
    get_payment_nemesis,
    get_user_payment_totals,
    list_payment_nemesis,
    set_payment_nemesis,
)
from handlers.admin_access import _display_name, _resolve_target_user, _user_label
from handlers.payments import _require_payment_view
from handlers.stats_period import current_payment_week_start, stats_timezone
from money_format import format_amount

logger = logging.getLogger(__name__)

NEMESIS_LAST_SLOT_KEY = "nemesis_last_slot"
NEMESIS_LOOP_SECONDS = 60
NEMESIS_POST_HOURS = (12, 14, 16, 18, 20)


def build_nemesis_handlers() -> list:
    return [
        CommandHandler("nemesis", nemesis_command),
    ]


def _nemesis_user_label(
    user_id: int,
    username: str | None,
    display: str | None,
) -> str:
    if username:
        return f"@{html.escape(username.lstrip('@'))}"
    if display:
        return html.escape(display)
    return html.escape(str(user_id))


def _nemesis_slot_key(now: datetime) -> str | None:
    if now.hour not in NEMESIS_POST_HOURS:
        return None
    return f"{now.date().isoformat()}:{now.hour}"


def format_nemesis_update(
    nemesis: PaymentNemesis,
    *,
    total_a: float,
    total_b: float,
    period_label: str,
) -> str:
    label_a = _nemesis_user_label(
        nemesis.user_a_id, nemesis.user_a_username, nemesis.user_a_display
    )
    label_b = _nemesis_user_label(
        nemesis.user_b_id, nemesis.user_b_username, nemesis.user_b_display
    )
    amount_a = html.escape(format_amount(total_a))
    amount_b = html.escape(format_amount(total_b))
    period = html.escape(period_label)

    if total_a > total_b:
        lead = html.escape(format_amount(total_a - total_b))
        headline = f"⚔️ <b>{label_a}</b> is <b>{lead}</b> ahead of <b>{label_b}</b>"
    elif total_b > total_a:
        lead = html.escape(format_amount(total_b - total_a))
        headline = f"⚔️ <b>{label_b}</b> is <b>{lead}</b> ahead of <b>{label_a}</b>"
    else:
        headline = f"⚔️ <b>Tied</b> at <b>{amount_a}</b>"

    return (
        f"{headline}\n"
        f"<i>{period}</i>\n\n"
        f"{label_a}: <b>{amount_a}</b>\n"
        f"{label_b}: <b>{amount_b}</b>"
    )


async def nemesis_command(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await _require_payment_view(update, settings, context.bot_data):
        return

    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        return

    args = [arg.strip() for arg in (context.args or []) if arg.strip()]
    if args and args[0].lower() in {"off", "stop", "clear", "cancel"}:
        if clear_payment_nemesis(settings.database_path, chat.id):
            await message.reply_text("Nemesis rivalry cleared for this chat.")
        else:
            await message.reply_text("No nemesis rivalry is set in this chat.")
        return

    if not args:
        existing = get_payment_nemesis(settings.database_path, chat.id)
        if existing is None:
            await message.reply_text(
                "Start a payment rivalry:\n"
                "• <code>/nemesis @username</code>\n"
                "• Reply to someone with <code>/nemesis</code>\n\n"
                "Updates post here every 2 hours (12pm–8pm).\n"
                "Use <code>/nemesis off</code> to stop.",
                parse_mode="HTML",
            )
            return
        since, period_label = current_payment_week_start()
        _, total_a = get_user_payment_totals(
            settings.database_path, existing.user_a_id, since=since
        )
        _, total_b = get_user_payment_totals(
            settings.database_path, existing.user_b_id, since=since
        )
        await message.reply_text(
            format_nemesis_update(
                existing,
                total_a=total_a,
                total_b=total_b,
                period_label=period_label,
            ),
            parse_mode="HTML",
        )
        return

    target = _resolve_target_user(
        update, args, database_path=settings.database_path
    )
    if target is None:
        await message.reply_text(
            "Pick your nemesis:\n"
            "• <code>/nemesis @username</code>\n"
            "• Reply to their message with <code>/nemesis</code>",
            parse_mode="HTML",
        )
        return

    if target.id == user.id:
        await message.reply_text("You can't nemesis yourself.")
        return

    set_payment_nemesis(
        settings.database_path,
        chat_id=chat.id,
        user_a_id=user.id,
        user_a_username=user.username,
        user_a_display=_display_name(user),
        user_b_id=target.id,
        user_b_username=getattr(target, "username", None),
        user_b_display=_display_name(target),
        created_by_id=user.id,
    )

    await message.reply_text(
        f"⚔️ Nemesis set: <b>{html.escape(_user_label(user))}</b> vs "
        f"<b>{html.escape(_user_label(target))}</b>\n\n"
        "Head-to-head updates every 2 hours between 12pm and 8pm "
        "(skipped if neither has logged an out this week).\n"
        "<code>/nemesis off</code> to stop.",
        parse_mode="HTML",
    )


async def _post_nemesis_update(bot, settings: Settings, nemesis: PaymentNemesis) -> bool:
    since, period_label = current_payment_week_start()
    count_a, total_a = get_user_payment_totals(
        settings.database_path, nemesis.user_a_id, since=since
    )
    count_b, total_b = get_user_payment_totals(
        settings.database_path, nemesis.user_b_id, since=since
    )
    if count_a + count_b == 0:
        return False

    text = format_nemesis_update(
        nemesis,
        total_a=total_a,
        total_b=total_b,
        period_label=period_label,
    )
    try:
        await bot.send_message(
            chat_id=nemesis.chat_id,
            text=text,
            parse_mode="HTML",
            disable_notification=True,
        )
        return True
    except Forbidden:
        logger.warning("Nemesis chat %s blocked the bot — clearing rivalry", nemesis.chat_id)
        clear_payment_nemesis(settings.database_path, nemesis.chat_id)
    except BadRequest as exc:
        logger.warning(
            "Could not post nemesis update to chat %s: %s",
            nemesis.chat_id,
            exc,
        )
    except Exception:
        logger.exception("Nemesis update failed for chat %s", nemesis.chat_id)
    return False


async def nemesis_loop(bot, settings: Settings, bot_data: dict) -> None:
    """Post nemesis payment updates every 2 hours between 12:00 and 18:00 local."""
    last_slots: dict[int, str] = bot_data.setdefault(NEMESIS_LAST_SLOT_KEY, {})
    try:
        while True:
            await asyncio.sleep(NEMESIS_LOOP_SECONDS)
            tz = stats_timezone()
            now = datetime.now(tz)
            slot = _nemesis_slot_key(now)
            if slot is None:
                continue

            for nemesis in list_payment_nemesis(settings.database_path):
                if last_slots.get(nemesis.chat_id) == slot:
                    continue
                posted = await _post_nemesis_update(bot, settings, nemesis)
                if posted:
                    last_slots[nemesis.chat_id] = slot
    except asyncio.CancelledError:
        raise
