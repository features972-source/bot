"""Log and list outbound payment announcements (e.g. \"4943 out\", \"4.5k out\")."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO

from telegram import InputMediaPhoto, Update
from telegram.ext import CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from config import Settings
from call_display import format_extension_user_plain
from database import (
    ExtensionLink,
    PaymentLeaderboardEntry,
    PaymentRecord,
    clear_all_payments,
    delete_payment_out,
    get_payment_by_id,
    get_payment_by_message,
    is_chat_blacklisted,
    update_payment_amount,
    get_payment_leaderboard,
    get_payment_starter_leaderboard,
    get_payment_notify_chat_id,
    get_payment_totals,
    list_links,
    list_payments_since,
    list_all_payments,
    list_recent_payments,
    payment_message_exists,
    record_payment_out,
    clear_paidside_epoch,
    get_paidside_epoch,
    set_ms_graph_refresh_token,
    set_paidside_epoch,
    update_payment_cleared,
)
from handlers.admin_access import is_bot_admin, require_admin
from handlers.payment_table import format_image_subtitle, status_summary_totals
from handlers.payment_table_image import live_report_title, render_payments_table_png
from handlers.stats_period import (
    _parse_stats_period,
    current_payment_week_start,
    stats_period_footnote,
    stats_timezone,
)
from payments_excel_export import (
    SYNC_ESTIMATE_SECONDS,
    excel_sync_with_timer,
    schedule_payments_excel_sync,
)

logger = logging.getLogger(__name__)

PENDING_CARD_KEY = "pending_payment_card"
PENDING_CLEAR_PAYMENTS_KEY = "pending_clear_payments"
CARD_LAST4_PATTERN = re.compile(r"^\d{4}$")

CALL_AGENT_LINE = re.compile(
    r"👤\s*(.+?)(?:\s*\n|$)",
    re.IGNORECASE | re.DOTALL,
)
ON_PHONE_STARTER_PATTERNS = (
    re.compile(
        r"📞🟢\s+ON CALL",
        re.IGNORECASE,
    ),
    re.compile(
        r"📞❌\s+CALL ENDED",
        re.IGNORECASE,
    ),
    re.compile(
        r"📞🟢\s+(.+?)\s+is on the phone",
        re.IGNORECASE | re.DOTALL,
    ),
)
USERNAME_IN_LABEL = re.compile(r"@([A-Za-z0-9_]{4,})")

# Matches out, ouut, ououtt, oouutt, etc. (letters between o and t may repeat)
FUZZY_OUT_PATTERN = re.compile(r"o[uot]+\s*(?:of|too)?", re.IGNORECASE)

from money_format import (
    INLINE_PAYMENT_OUT_PATTERN,
    PAYMENT_OUT_PATTERN,
    currency_symbol,
    format_amount,
    parse_amount_candidates,
)


@dataclass
class PendingPaymentOut:
    amount: float
    raw_text: str
    chat_id: int
    out_message_id: int
    finisher_user_id: int
    finisher_username: str | None
    finisher_display_name: str | None
    starter_user_id: int
    starter_username: str | None
    starter_display_name: str | None


@dataclass
class PendingClearPayments:
    admin_user_id: int


def _normalize_payment_text(text: str) -> str:
    stripped = re.sub(r"\s+", " ", text.strip())
    stripped = FUZZY_OUT_PATTERN.sub(" out of ", stripped)
    stripped = re.sub(r"(?<=\d),(?=\d)", "", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def build_payment_command_handlers() -> list:
    return [
        CommandHandler("out", out_command),
        CommandHandler("payments", payments_command),
        CommandHandler("sent", payments_command),
        CommandHandler("alltimepayments", alltimepayments_command),
        CommandHandler("alltime", alltimepayments_command),
        CommandHandler("outstats", outstats_command),
        CommandHandler("outleaderboard", outstats_command),
        CommandHandler("clearpayments", clearpayments_command),
        CommandHandler("todaypayments", todaypayments_command),
        CommandHandler("cleared", cleared_command),
        CommandHandler("setcleared", cleared_command),
        CommandHandler("notcleared", notcleared_command),
        CommandHandler("setnotcleared", notcleared_command),
        CommandHandler("setpayment", setpayment_command),
        CommandHandler("updatepayment", setpayment_command),
        CommandHandler("editpayment", setpayment_command),
        CommandHandler("removepayment", removepayment_command),
        CommandHandler("syncpayments", syncpayments_command),
        CommandHandler("paidside", paidside_command),
        CommandHandler("excelwebauth", excelwebauth_command),
        CommandHandler("myid", myid_command),
    ]


def build_payment_message_handlers() -> list:
    return [
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            payment_out_message,
            block=False,
        ),
    ]


def build_payment_handlers() -> list:
    return [
        *build_payment_command_handlers(),
        *build_payment_message_handlers(),
    ]


def _amount_from_match(match: re.Match[str]) -> float | None:
    amount = float(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").lower()
    if suffix == "k":
        amount *= 1000
    elif suffix == "m":
        amount *= 1_000_000
    if amount <= 0:
        return None
    return amount


def parse_payment_out(text: str) -> float | None:
    match = PAYMENT_OUT_PATTERN.match(_normalize_payment_text(text))
    if not match:
        return None
    return _amount_from_match(match)


def find_payment_out_in_text(text: str) -> tuple[float, str] | None:
    stripped = text.strip()
    whole = parse_payment_out(stripped)
    if whole is not None:
        return whole, stripped
    normalized = _normalize_payment_text(stripped)
    match = INLINE_PAYMENT_OUT_PATTERN.search(normalized)
    if not match:
        return None
    amount = _amount_from_match(match)
    if amount is None:
        return None
    return amount, match.group(0).strip()


def looks_like_payment_out(text: str, bot_username: str | None = None) -> bool:
    """True when plain text is e.g. 5182 out (used to avoid credo intercepting outs)."""
    cleaned = _strip_leading_bot_mention(text.strip(), bot_username)
    return find_payment_out_in_text(_strip_explicit_starter(cleaned)) is not None


def parse_payment_amount(text: str) -> tuple[float, str] | None:
    """Parse plain amounts or full out phrases (e.g. 5182 out)."""
    found = find_payment_out_in_text(text)
    if found is not None:
        return found
    stripped = text.strip()
    normalized = _normalize_payment_text(stripped)
    for candidate in parse_amount_candidates(stripped, normalized):
        amount = parse_payment_out(candidate)
        if amount is not None:
            return amount, candidate
    return None


def _cleared_status_label(cleared: bool | None) -> str:
    if cleared is None:
        return "🟠 Pending"
    return "🟢 Cleared" if cleared else "🔴 Not cleared"


_PAYMENTS_LIST_LIMIT = 25


def _format_card_saved_reply(payment_id: int) -> str:
    return f"✅ Added to the system · **#{payment_id}**"


def _format_card_prompt(
    amount: float,
    *,
    finisher_user_id: int,
    finisher_username: str | None,
    finisher_display_name: str | None,
    starter_user_id: int,
    starter_username: str | None,
    starter_display_name: str | None,
) -> str:
    amount_str = format_amount(amount)
    finisher = _stored_user_label(
        finisher_username, finisher_display_name, finisher_user_id
    )
    starter = _stored_user_label(
        starter_username, starter_display_name, starter_user_id
    )
    if starter_user_id == finisher_user_id:
        team = f"{starter} · starter & finisher"
    else:
        team = f"Starter {starter} → Finisher {finisher}"
    return (
        f"🔥 {amount_str} OUT 🔥\n\n"
        f"💸 {team}\n\n"
        "💳 Reply to **this message** and add the last **4 digits** of the card "
        "(e.g. `1234`) — or it will **not** be added to the system."
    )


def _format_payment_line(record: PaymentRecord) -> str:
    """Compact single line (admin commands)."""
    finisher = _stored_user_label(
        record.finisher_username,
        record.finisher_display_name,
        record.finisher_user_id,
    )
    when = _format_when(record.created_at)
    amount = format_amount(record.amount)
    status = _cleared_status_label(record.cleared)
    if record.starter_user_id is not None:
        starter = _stored_user_label(
            record.starter_username,
            record.starter_display_name,
            record.starter_user_id,
        )
        return (
            f"{status} · #{record.id} · Starter {starter} → Finisher {finisher} · "
            f"{amount} ({when})"
        )
    return f"{status} · #{record.id} · Finisher {finisher} · {amount} ({when})"


def _resolve_payment_from_reply(
    database_path: str,
    message,
) -> PaymentRecord | None:
    """Find a payment linked to a replied-to message (out post or thread)."""
    if message is None or message.reply_to_message is None:
        return None
    chat_id = message.chat_id
    current = message.reply_to_message
    for _ in range(6):
        if current is None:
            break
        record = get_payment_by_message(
            database_path,
            chat_id=chat_id,
            telegram_message_id=current.message_id,
        )
        if record is not None:
            return record
        current = current.reply_to_message
    return None


def _parse_payment_id_arg(raw: str) -> int | None:
    try:
        return int(raw.lstrip("#"))
    except ValueError:
        return None


def _format_payment_block(record: PaymentRecord) -> str:
    finisher = _stored_user_label(
        record.finisher_username,
        record.finisher_display_name,
        record.finisher_user_id,
    )
    when = _format_when(record.created_at)
    amount = format_amount(record.amount)
    status = _cleared_status_label(record.cleared)
    lines = [
        f"<b>#{record.id}</b>  <b>{html.escape(amount)}</b>",
        f"{html.escape(status)}  ·  {html.escape(when)}",
    ]
    if record.starter_user_id is not None:
        starter = _stored_user_label(
            record.starter_username,
            record.starter_display_name,
            record.starter_user_id,
        )
        if record.starter_user_id == record.finisher_user_id:
            lines.append(html.escape(f"{starter} · starter & finisher"))
        else:
            lines.append(
                f"{html.escape(starter)} → {html.escape(finisher)}"
            )
    else:
        lines.append(html.escape(finisher))
    if record.card_last4:
        lines.append(f"💳 ····{html.escape(record.card_last4)}")
    return "\n".join(lines)


def _today_start_utc() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def format_today_payments_paragraph(records: list[PaymentRecord]) -> str:
    if not records:
        return "No payments logged today yet."

    total_amount = sum(record.amount for record in records)
    count = len(records)
    lines: list[str] = []
    for record in records:
        lines.append(_format_payment_line(record))

    intro = (
        f"Today: {count} payment{'s' if count != 1 else ''} "
        f"totalling {format_amount(total_amount)}."
    )
    if len(lines) == 1:
        return f"{intro} {lines[0]}."
    body = "; ".join(lines[:-1]) + f"; and {lines[-1]}"
    return f"{intro} {body}."


def _today_payment_records(settings: Settings) -> list[PaymentRecord]:
    return list_payments_since(settings.database_path, since=_today_start_utc())


async def _pm_admin_today_payments(bot, settings: Settings) -> None:
    if settings.admin_chat_id is None:
        return
    text = format_today_payments_paragraph(_today_payment_records(settings))
    try:
        await bot.send_message(chat_id=settings.admin_chat_id, text=text)
    except Exception:
        logger.exception("Could not PM admin today's payment summary")


def _user_label(user) -> str:
    if user.username:
        return f"@{user.username}"
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    return name or str(user.id)


def _stored_user_label(
    username: str | None,
    display_name: str | None,
    user_id: int,
) -> str:
    if username:
        label = f"@{username.lstrip('@')}"
        if display_name:
            return f"{label} ({display_name})"
        return label
    if display_name:
        return display_name
    return str(user_id)


def _link_from_on_phone_label(database_path: str, label: str) -> ExtensionLink | None:
    cleaned = label.strip()
    if " — " in cleaned:
        cleaned = cleaned.split(" — ", 1)[0].strip()
    cleaned = re.sub(r"<[^>]+>", "", cleaned).strip()

    username_match = USERNAME_IN_LABEL.search(cleaned)
    if username_match:
        username = username_match.group(1)
        for link in list_links(database_path):
            if link.telegram_username and link.telegram_username.lower() == username.lower():
                return link

    for link in list_links(database_path):
        if format_extension_user_plain(link) == cleaned:
            return link
        if link.display_name and link.display_name in cleaned:
            return link
    return None


def _starter_from_on_phone_text(database_path: str, text: str) -> ExtensionLink | None:
    plain = re.sub(r"<[^>]+>", "", text)
    for pattern in ON_PHONE_STARTER_PATTERNS:
        if not pattern.search(plain):
            continue
        agent_match = CALL_AGENT_LINE.search(plain)
        if not agent_match:
            continue
        link = _link_from_on_phone_label(database_path, agent_match.group(1))
        if link is not None:
            return link
    return None


STARTER_FOR_PATTERN = re.compile(r"\bfor\s+(@[A-Za-z0-9_]{4,})\b", re.IGNORECASE)
TRAILING_MENTION_PATTERN = re.compile(r"@([A-Za-z0-9_]{4,})\s*$")


def _user_tuple(user) -> tuple[int, str | None, str | None]:
    return user.id, user.username, _display_name(user)


def _strip_leading_bot_mention(text: str, bot_username: str | None) -> str:
    if not bot_username:
        return text
    stripped = text.strip()
    prefix = f"@{bot_username.lstrip('@')}"
    if stripped.lower().startswith(prefix.lower()):
        rest = stripped[len(prefix) :].strip()
        if rest:
            return rest
    return text


def _starter_actor_from_message(message) -> tuple[int | None, str | None, str | None] | None:
    if message is None:
        return None
    if message.from_user and not message.from_user.is_bot:
        return _user_tuple(message.from_user)
    forward_user = getattr(message, "forward_from", None)
    if forward_user and not forward_user.is_bot:
        return _user_tuple(forward_user)
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        sender = getattr(origin, "sender_user", None)
        if sender and not sender.is_bot:
            return _user_tuple(sender)
    return None


def _reply_chain_root(message) -> object | None:
    root = message
    while root is not None and root.reply_to_message is not None:
        root = root.reply_to_message
    return root


def _strip_explicit_starter(text: str) -> str:
    cleaned = STARTER_FOR_PATTERN.sub("", text)
    cleaned = TRAILING_MENTION_PATTERN.sub("", cleaned)
    return cleaned.strip()


def _resolve_starter(
    *,
    settings: Settings,
    bot_data: dict,
    reply_to,
) -> tuple[int | None, str | None, str | None] | None:
    if reply_to is None:
        return None

    root = _reply_chain_root(reply_to)
    actor = _starter_actor_from_message(root) or _starter_actor_from_message(reply_to)
    if actor is not None:
        return actor

    from notify import _live_calls

    chat_id = reply_to.chat_id if reply_to.chat else None
    replied_id = reply_to.message_id
    if (
        chat_id is not None
        and reply_to.from_user
        and reply_to.from_user.is_bot
    ):
        nested = reply_to.reply_to_message
        if nested is not None and nested.from_user and not nested.from_user.is_bot:
            return _user_tuple(nested.from_user)

        for live_call in _live_calls(bot_data).values():
            if live_call.message_ids.get(chat_id) == replied_id:
                link = live_call.link
                return link.telegram_user_id, link.telegram_username, link.display_name

        text = reply_to.text or reply_to.caption or ""
        link = _starter_from_on_phone_text(settings.database_path, text)
        if link is not None:
            return link.telegram_user_id, link.telegram_username, link.display_name

    return None


def _display_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    return name or "Unknown"


def _payment_chat_ids(settings: Settings, bot_data: dict) -> set[int]:
    ids: set[int] = set()
    notify_id = bot_data.get("notify_chat_id") or settings.notify_chat_id
    if notify_id is not None:
        ids.add(notify_id)
    payment_notify_id = get_payment_notify_chat_id(settings.database_path)
    if payment_notify_id is not None:
        ids.add(payment_notify_id)
    if settings.copy_to_chat_id is not None:
        ids.add(settings.copy_to_chat_id)
    return ids


def _payment_chat_allowed(settings: Settings, bot_data: dict, chat) -> bool:
    if chat is None:
        return False
    allowed = _payment_chat_ids(settings, bot_data)
    if allowed:
        return chat.id in allowed
    return chat.type in ("group", "supergroup")


def _can_view_payments(
    update: Update, settings: Settings, bot_data: dict
) -> bool:
    user = update.effective_user
    if user and is_bot_admin(settings, settings.database_path, user.id):
        return True
    return _payment_chat_allowed(settings, bot_data, update.effective_chat)


async def _require_payment_view(
    update: Update, settings: Settings, bot_data: dict
) -> bool:
    if _can_view_payments(update, settings, bot_data):
        return True
    message = update.effective_message
    chat = update.effective_chat
    if message is None:
        return False
    allowed = _payment_chat_ids(settings, bot_data)
    hint = (
        f"Allowed chat id(s): {', '.join(str(i) for i in sorted(allowed))}"
        if allowed
        else "No notify group set — admin: run /setnotify in your payment group."
    )
    chat_id = chat.id if chat is not None else "unknown"
    await message.reply_text(
        "Payment commands only work in the **notify / payment group** "
        f"(this chat is `{chat_id}`).\n\n{hint}",
        parse_mode="Markdown",
    )
    return False


def _pending_card_map(bot_data: dict) -> dict[tuple[int, int], PendingPaymentOut]:
    return bot_data.setdefault(PENDING_CARD_KEY, {})


def _pending_clear_payments_map(
    bot_data: dict,
) -> dict[tuple[int, int], PendingClearPayments]:
    return bot_data.setdefault(PENDING_CLEAR_PAYMENTS_KEY, {})


async def _finalize_payment_out(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingPaymentOut,
    *,
    card_last4: str,
) -> None:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    if message is None:
        return

    payment_id = record_payment_out(
        settings.database_path,
        telegram_user_id=pending.finisher_user_id,
        telegram_username=pending.finisher_username,
        display_name=pending.finisher_display_name,
        amount=pending.amount,
        raw_text=pending.raw_text,
        chat_id=pending.chat_id,
        telegram_message_id=pending.out_message_id,
        starter_user_id=pending.starter_user_id,
        starter_username=pending.starter_username,
        starter_display_name=pending.starter_display_name,
        card_last4=card_last4,
    )
    if payment_id is None:
        await message.reply_text(
            "That payment was not saved (duplicate message or already logged)."
        )
        return

    record = get_payment_by_id(settings.database_path, payment_id)
    if record is None:
        return
    await message.reply_text(
        _format_card_saved_reply(payment_id), parse_mode="Markdown"
    )
    try:
        from quiet_wins import maybe_quiet_win_close_rate

        await maybe_quiet_win_close_rate(
            context.bot,
            settings,
            telegram_user_id=pending.finisher_user_id,
            telegram_username=pending.finisher_username,
            display_name=pending.finisher_display_name,
        )
    except Exception:
        logger.exception(
            "Quiet win close-rate check failed for user %s",
            pending.finisher_user_id,
        )
    try:
        from shadow_leaderboard import maybe_shadow_closer_rank

        await maybe_shadow_closer_rank(
            context.bot,
            settings,
            telegram_user_id=pending.finisher_user_id,
        )
    except Exception:
        logger.exception(
            "Shadow closer rank failed for user %s",
            pending.finisher_user_id,
        )
    await _pm_admin_today_payments(context.bot, settings)
    try:
        from handlers.payment_reports import schedule_payment_report_refresh

        schedule_payment_report_refresh(context.bot, settings)
    except Exception:
        logger.exception("Payment report refresh failed")
    if settings.payments_onedrive_path:
        timer_msg = await message.reply_text(
            f"⏳ Updating Excel… ~{SYNC_ESTIMATE_SECONDS}s remaining"
        )
        await excel_sync_with_timer(
            context.bot,
            chat_id=timer_msg.chat_id,
            message_id=timer_msg.message_id,
            settings=settings,
            show_full_detail=False,
        )


async def _try_complete_pending_card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    if message is None or not message.text or message.reply_to_message is None:
        return False

    reply = message.reply_to_message
    if not reply.from_user or not reply.from_user.is_bot:
        return False

    pending = _pending_card_map(context.bot_data).pop(
        (message.chat_id, reply.message_id),
        None,
    )
    if pending is None:
        return False

    last4 = message.text.strip()
    if not CARD_LAST4_PATTERN.fullmatch(last4):
        _pending_card_map(context.bot_data)[(message.chat_id, reply.message_id)] = (
            pending
        )
        await message.reply_text(
            "Send exactly 4 digits for the card, e.g. `1234`.",
            parse_mode="Markdown",
        )
        return True

    await _finalize_payment_out(update, context, pending, card_last4=last4)
    return True


async def _try_complete_pending_clearpayments(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    user = update.effective_user
    if message is None or not message.text or message.reply_to_message is None:
        return False
    if user is None:
        return False

    reply = message.reply_to_message
    if not reply.from_user or not reply.from_user.is_bot:
        return False

    key = (message.chat_id, reply.message_id)
    pending = _pending_clear_payments_map(context.bot_data).pop(key, None)
    if pending is None:
        return False

    if user.id != pending.admin_user_id:
        _pending_clear_payments_map(context.bot_data)[key] = pending
        await message.reply_text(
            "Only the admin who started /clearpayments can confirm."
        )
        return True

    if message.text.strip() != "DELETE":
        _pending_clear_payments_map(context.bot_data)[key] = pending
        await message.reply_text(
            "Not confirmed. Reply to the warning with DELETE (all capitals) to wipe "
            "all payments."
        )
        return True

    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return True

    cleared = clear_all_payments(settings.database_path)
    if cleared == 0:
        await message.reply_text("No payment records to clear.")
        return True

    await message.reply_text(
        f"Cleared {cleared} payment record(s). /payments will be empty until new "
        "outs are logged."
    )
    schedule_payments_excel_sync(settings)
    try:
        from handlers.payment_reports import schedule_payment_report_refresh

        schedule_payment_report_refresh(context.bot, settings)
    except Exception:
        logger.exception("Payment report refresh failed")
    return True


async def _process_payment_out(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    amount: float,
    raw_text: str,
) -> None:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return

    if message.reply_to_message is None:
        await message.reply_text(
            "Reply to the starter's notes (or the bot's **ON CALL** post), then send "
            "the amount, e.g. `6682 out` or `/out 6682`.",
            parse_mode="Markdown",
        )
        return

    starter = _resolve_starter(
        settings=settings,
        bot_data=context.bot_data,
        reply_to=message.reply_to_message,
    )
    if starter is None:
        bot_username = getattr(context.bot, "username", None) or "this bot"
        await message.reply_text(
            "Could not tell who the starter is from that reply.\n\n"
            "Reply to:\n"
            "• The starter's **notes** message, or\n"
            "• The bot's **📞 ON CALL** post for that call\n\n"
            "Then send e.g. `6682 out` or `/out 6682`.\n\n"
            "If plain `6682 out` does nothing, use `/out 6682` (works when "
            "Group Privacy is on) or turn off privacy in @BotFather → "
            f"**@{bot_username}** → Group Privacy.",
            parse_mode="Markdown",
        )
        return

    starter_user_id, starter_username, starter_display_name = starter

    if is_chat_blacklisted(
        settings.database_path,
        chat.id,
        telegram_user_id=starter_user_id,
        telegram_username=starter_username,
    ):
        label = (
            f"@{starter_username.lstrip('@')}"
            if starter_username
            else (starter_display_name or "That user")
        )
        await message.reply_text(
            f"{label} is blocked in this chat — payment not logged."
        )
        return

    if payment_message_exists(
        settings.database_path,
        chat_id=chat.id,
        telegram_message_id=message.message_id,
    ):
        await message.reply_text(
            "That payment was not saved (duplicate message or already logged)."
        )
        return

    prompt = await message.reply_text(
        _format_card_prompt(
            amount,
            finisher_user_id=user.id,
            finisher_username=user.username,
            finisher_display_name=_display_name(user),
            starter_user_id=starter_user_id,
            starter_username=starter_username,
            starter_display_name=starter_display_name,
        ),
        parse_mode="Markdown",
    )
    if prompt is None:
        return

    _pending_card_map(context.bot_data)[(chat.id, prompt.message_id)] = PendingPaymentOut(
        amount=amount,
        raw_text=raw_text,
        chat_id=chat.id,
        out_message_id=message.message_id,
        finisher_user_id=user.id,
        finisher_username=user.username,
        finisher_display_name=_display_name(user),
        starter_user_id=starter_user_id,
        starter_username=starter_username,
        starter_display_name=starter_display_name,
    )


async def out_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log a payment via /out 5182 (works with Telegram Group Privacy enabled)."""
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    if not _payment_chat_allowed(settings, context.bot_data, chat):
        allowed = sorted(_payment_chat_ids(settings, context.bot_data))
        logger.info(
            "Ignored /out in chat %s (allowed: %s)",
            chat.id,
            allowed,
        )
        await message.reply_text(
            "Payments only work in the announcement group for this bot.\n"
            f"This chat: `{chat.id}`",
            parse_mode="Markdown",
        )
        return

    amount_text = " ".join(context.args).strip()
    if not amount_text:
        await message.reply_text(
            "Reply to the starter's notes or the bot's **ON CALL** post, then:\n"
            "`/out 5182` or `/out 4.5k`",
            parse_mode="Markdown",
        )
        return

    parsed = parse_payment_amount(amount_text)
    if parsed is None:
        await message.reply_text(
            "Could not read that amount. Example: `/out 5182` or `/out 4.5k`",
            parse_mode="Markdown",
        )
        return

    amount, raw_text = parsed
    logger.info(
        "payment /out chat=%s user=%s amount=%s reply=%s",
        chat.id,
        user.id,
        amount,
        message.reply_to_message is not None,
    )
    await _process_payment_out(
        update,
        context,
        amount=amount,
        raw_text=raw_text,
    )


async def payment_out_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or not message.text:
        return
    if not _payment_chat_allowed(settings, context.bot_data, chat):
        text = _strip_leading_bot_mention(
            message.text, getattr(context.bot, "username", None)
        )
        if find_payment_out_in_text(_strip_explicit_starter(text)) is not None:
            allowed = _payment_chat_ids(settings, context.bot_data)
            hint = (
                f"Allowed chat id(s): {', '.join(str(i) for i in sorted(allowed))}"
                if allowed
                else "No notify group set — admin: run /setnotify in your payment group."
            )
            await message.reply_text(
                "Payments can only be logged in the **notify group** "
                f"(this chat is `{chat.id}`).\n\n{hint}\n\n"
                "Admin: run **/setnotify** in the group where you log payments.",
                parse_mode="Markdown",
            )
        return

    if await _try_complete_pending_card(update, context):
        return

    if await _try_complete_pending_clearpayments(update, context):
        return

    from handlers.panic import try_complete_pending_panic

    if await try_complete_pending_panic(update, context):
        return

    text = _strip_leading_bot_mention(
        message.text, getattr(context.bot, "username", None)
    )
    amount_result = find_payment_out_in_text(_strip_explicit_starter(text))
    if amount_result is None:
        return

    amount, raw_text = amount_result
    logger.info(
        "payment out text chat=%s user=%s amount=%s reply=%s",
        chat.id,
        user.id,
        amount,
        message.reply_to_message is not None,
    )
    await _process_payment_out(
        update,
        context,
        amount=amount,
        raw_text=raw_text,
    )


def _payment_records_for_period(
    database_path: str,
    *,
    since: datetime | None,
    limit: int | None = None,
) -> list[PaymentRecord]:
    if since is None:
        records = list_all_payments(database_path)
        records.reverse()
    else:
        records = list_payments_since(database_path, since=since)
        records.reverse()
    if limit is None:
        return records
    return records[:limit]


def _build_payments_summary_image(
    settings: Settings,
    *,
    since: datetime | None,
    period_label: str,
    records: list[PaymentRecord],
) -> bytes | list[bytes]:
    total_count, total_amount = get_payment_totals(settings.database_path, since=since)
    pending_count, pending_amount = get_payment_totals(
        settings.database_path, since=since, pending=True
    )
    cleared_count, cleared_amount = get_payment_totals(
        settings.database_path, since=since, cleared=True
    )
    not_cleared_count, not_cleared_amount = get_payment_totals(
        settings.database_path, since=since, cleared=False
    )

    if since is not None:
        lookup_records = list_payments_since(settings.database_path, since=since)
    else:
        lookup_records = list_all_payments(settings.database_path)

    hidden = 0
    status_totals = status_summary_totals(
        pending_amount=pending_amount,
        pending_count=pending_count,
        cleared_amount=cleared_amount,
        cleared_count=cleared_count,
        not_cleared_amount=not_cleared_amount,
        not_cleared_count=not_cleared_count,
    )
    title = live_report_title(settings.bot_display_name)
    total_label = "WEEK TOTAL"
    if since is None:
        title = "All-time payments"
        total_label = "ALL TIME"
    return render_payments_table_png(
        records,
        database_path=settings.database_path,
        total_amount=total_amount,
        total_count=total_count,
        lookup_records=lookup_records,
        title=title,
        subtitle=format_image_subtitle(period_label) if since is not None else "Every payment on record",
        status_totals=status_totals,
        live=False,
        full_excel=False,
        total_label=total_label,
        mobile=True,
    )


async def _send_payments_summary(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    since: datetime | None,
    period_label: str,
    empty_text: str,
) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await _require_payment_view(update, settings, context.bot_data):
        return

    message = update.effective_message
    if message is None:
        return

    total_count, _ = get_payment_totals(settings.database_path, since=since)
    if total_count == 0:
        await message.reply_text(empty_text, parse_mode="Markdown")
        return

    records = _payment_records_for_period(settings.database_path, since=since)
    user = update.effective_user
    include_admin = bool(
        user and is_bot_admin(settings, settings.database_path, user.id)
    )
    try:
        result = await asyncio.to_thread(
            _build_payments_summary_image,
            settings,
            since=since,
            period_label=period_label,
            records=records,
        )
    except Exception:
        logger.exception("Failed to build payments summary image")
        await message.reply_text(
            "Could not build the payment table image. Try again in a moment."
        )
        return

    pages = result if isinstance(result, list) else [result]

    caption_parts = []
    if since is not None:
        caption_parts.append(
            "<i>This week’s payments · new week every Sunday</i>\n"
            "<i>/alltimepayments — full history</i>"
        )
    else:
        caption_parts.append("<i>All payments on record</i>")
    if len(pages) > 1:
        caption_parts.append(
            f"<i>{len(pages)} pages — swipe through all images</i>"
        )
    if include_admin:
        caption_parts.append(
            "<i>Admin: use # from table in bot DM — /setcleared 12 · /setpayment 12 amount</i>"
        )
    caption = "\n".join(caption_parts) if caption_parts else None

    try:
        if len(pages) == 1:
            bio = BytesIO(pages[0])
            bio.name = "payments.jpg"
            bio.seek(0)
            await message.reply_photo(
                photo=bio,
                caption=caption,
                parse_mode="HTML",
            )
            return

        media: list[InputMediaPhoto] = []
        for index, png in enumerate(pages):
            bio = BytesIO(png)
            bio.name = f"payments-{index + 1}.jpg"
            bio.seek(0)
            media.append(
                InputMediaPhoto(
                    media=bio,
                    caption=caption if index == 0 else None,
                    parse_mode="HTML" if index == 0 else None,
                )
            )
        await message.reply_media_group(media=media)
    except Exception:
        logger.exception("Failed to send payments summary image")
        await message.reply_text(
            "Could not send the payment table image. Try again in a moment."
        )


async def payments_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    since, period_label = current_payment_week_start()
    await _send_payments_summary(
        update,
        context,
        since=since,
        period_label=period_label,
        empty_text=(
            "**No payments this week yet.**\n\n"
            "New week starts every **Sunday**.\n"
            "Use `/alltimepayments` to see everything on record.\n\n"
            "To log an out: reply to the starter’s notes, then send e.g. `5182 out`."
        ),
    )


async def alltimepayments_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _send_payments_summary(
        update,
        context,
        since=None,
        period_label="all time",
        empty_text=(
            "**No payments on record yet.**\n\n"
            "Reply to the starter’s notes, then send e.g. `5182 out`."
        ),
    )


def _payment_command_conversation_fallback(command_fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.clear()
        await command_fn(update, context)
        return ConversationHandler.END

    return wrapper


payments_conversation_fallback = _payment_command_conversation_fallback(
    payments_command
)
alltimepayments_conversation_fallback = _payment_command_conversation_fallback(
    alltimepayments_command
)
out_conversation_fallback = _payment_command_conversation_fallback(out_command)


def _leaderboard_user_label(entry: PaymentLeaderboardEntry) -> str:
    username = (entry.telegram_username or "").strip()
    display = (entry.display_name or "").strip()
    if username and display:
        return f"@{html.escape(username.lstrip('@'))} ({html.escape(display)})"
    if username:
        return f"@{html.escape(username.lstrip('@'))}"
    if display:
        return html.escape(display)
    return html.escape(str(entry.user_id))


def _parse_outstats_args(
    args: list[str],
) -> tuple[datetime | None, str, str | None]:
    """Return since, period label, and optional role filter (openers | closers)."""
    role: str | None = None
    rest = list(args)
    if rest:
        token = rest[0].strip().lower()
        if token in {"openers", "open", "starters", "starter"}:
            role = "openers"
            rest = rest[1:]
        elif token in {"closers", "close", "finishers", "finisher"}:
            role = "closers"
            rest = rest[1:]
    since, period_label = _parse_stats_period(rest)
    return since, period_label, role


def _format_leaderboard_lines(
    entries: list[PaymentLeaderboardEntry],
    *,
    empty_text: str,
) -> list[str]:
    if not entries:
        return [empty_text]
    medals = ("🥇", "🥈", "🥉")
    lines: list[str] = []
    for index, entry in enumerate(entries):
        prefix = medals[index] if index < len(medals) else f"{index + 1}."
        lines.append(
            f"{prefix} {_leaderboard_user_label(entry)} · "
            f"<b>{html.escape(format_amount(entry.total_amount))}</b> · "
            f"{entry.payment_count} out{'s' if entry.payment_count != 1 else ''}"
        )
    return lines


async def outstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not _can_view_payments(update, settings, context.bot_data):
        return

    since, period_label, role_filter = _parse_outstats_args(context.args or [])
    closers = get_payment_leaderboard(settings.database_path, since=since)
    openers = get_payment_starter_leaderboard(settings.database_path, since=since)
    total_count, total_amount = get_payment_totals(settings.database_path, since=since)

    if role_filter == "closers":
        if not closers:
            await update.effective_message.reply_text(
                f"💸 No closer stats for {period_label}.\n\n"
                "Reply to the starter's notes with e.g. `5182 out`.",
                parse_mode="Markdown",
            )
            return
        lines = [
            f"🔒 <b>Closers</b> — {html.escape(period_label)}",
            "",
            f"Total: <b>{html.escape(format_amount(total_amount))}</b> · "
            f"<b>{total_count}</b> payment{'s' if total_count != 1 else ''}",
            "",
            *_format_leaderboard_lines(
                closers,
                empty_text="<i>No closers yet.</i>",
            ),
        ]
    elif role_filter == "openers":
        if not openers:
            await update.effective_message.reply_text(
                f"🚪 No opener stats for {period_label}.\n\n"
                "Starters are tracked when OUT is logged as a reply to their notes.",
                parse_mode="Markdown",
            )
            return
        lines = [
            f"🚪 <b>Openers</b> — {html.escape(period_label)}",
            "",
            *_format_leaderboard_lines(
                openers,
                empty_text="<i>No openers yet.</i>",
            ),
        ]
    elif not closers and not openers:
        await update.effective_message.reply_text(
            f"💸 No payments logged for {period_label}.\n\n"
            "Reply to the starter's notes with e.g. `5182 out`.",
            parse_mode="Markdown",
        )
        return
    else:
        lines = [
            f"💸 <b>Out leaderboard</b> — {html.escape(period_label)}",
            "",
            f"Total: <b>{html.escape(format_amount(total_amount))}</b> · "
            f"<b>{total_count}</b> payment{'s' if total_count != 1 else ''}",
            "",
            "<b>🔒 Closers</b> <i>(who logged the OUT)</i>",
            *_format_leaderboard_lines(
                closers,
                empty_text="<i>No closers yet.</i>",
            ),
            "",
            "<b>🚪 Openers</b> <i>(reply-to notes on each OUT)</i>",
            *_format_leaderboard_lines(
                openers,
                empty_text=(
                    "<i>No opener data yet — reply to the starter's notes when logging OUT.</i>"
                ),
            ),
        ]

    lines.extend(
        [
            "",
            stats_period_footnote(),
            "",
            "<i>/outstats · /outstats openers · /outstats closers · today · 7 · all</i>",
        ]
    )
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def _set_payment_cleared_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    cleared: bool,
) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    if message is None:
        return

    cmd = "setcleared" if cleared else "setnotcleared"
    alt_cmd = "cleared" if cleared else "notcleared"
    payment_id: int | None = None

    if context.args:
        payment_id = _parse_payment_id_arg(context.args[0])

    if payment_id is None:
        record = _resolve_payment_from_reply(settings.database_path, message)
        if record is not None:
            payment_id = record.id

    if payment_id is None and not context.args:
        records = list_recent_payments(settings.database_path, limit=10)
        if not records:
            await message.reply_text("No payments logged yet.")
            return
        blocks = [_format_payment_block(record) for record in records]
        await message.reply_text(
            f"<b>Mark cleared status</b>\n\n"
            f"• <b>Easiest:</b> use the <b>#</b> column from /payments — "
            f"DM the bot: /{cmd} 12\n"
            f"• Or reply to the original <code>5182 out</code> with /{cmd}\n\n"
            f"<b>Recent payments</b>\n\n"
            f"{'\n\n'.join(blocks)}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if payment_id is None:
        await message.reply_text(
            f"Could not find that payment.\n\n"
            f"Reply to the <code>5182 out</code> message with /{cmd}, "
            f"or DM /{cmd} &lt;#&gt; using the # column on /payments."
        )
        return

    if not update_payment_cleared(
        settings.database_path, payment_id, cleared=cleared
    ):
        await message.reply_text(f"No payment with #{payment_id}.")
        return

    record = get_payment_by_id(settings.database_path, payment_id)
    if record is None:
        await message.reply_text(f"No payment with #{payment_id}.")
        return

    await message.reply_text(
        f"Updated to {html.escape(_cleared_status_label(cleared))}.\n\n"
        f"{_format_payment_block(record)}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await _pm_admin_today_payments(context.bot, settings)
    schedule_payments_excel_sync(settings)
    try:
        from handlers.payment_reports import schedule_payment_report_refresh

        schedule_payment_report_refresh(context.bot, settings)
    except Exception:
        logger.exception("Payment report refresh failed")


async def setpayment_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    if message is None:
        return

    if len(context.args) < 2:
        payment_id: int | None = None
        amount_text: str | None = None
        if len(context.args) == 1:
            payment_id = _parse_payment_id_arg(context.args[0])
            if payment_id is None:
                amount_text = context.args[0]
        if payment_id is None and amount_text:
            record = _resolve_payment_from_reply(settings.database_path, message)
            if record is not None:
                payment_id = record.id
                parsed = parse_payment_amount(amount_text)
                if parsed is not None:
                    new_amount, _ = parsed
                    old_amount = record.amount
                    if update_payment_amount(
                        settings.database_path, payment_id, amount=new_amount
                    ):
                        record = get_payment_by_id(settings.database_path, payment_id)
                        if record:
                            await message.reply_text(
                                f"Updated #{payment_id}: "
                                f"{html.escape(format_amount(old_amount))} → "
                                f"<b>{html.escape(format_amount(new_amount))}</b>\n\n"
                                f"{_format_payment_block(record)}",
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                            await _pm_admin_today_payments(context.bot, settings)
                            schedule_payments_excel_sync(settings)
                            try:
                                from handlers.payment_reports import (
                                    schedule_payment_report_refresh,
                                )

                                schedule_payment_report_refresh(context.bot, settings)
                            except Exception:
                                logger.exception("Payment report refresh failed")
                            return
        records = list_recent_payments(settings.database_path, limit=10)
        if not records:
            await message.reply_text("No payments logged yet.")
            return
        blocks = [_format_payment_block(record) for record in records]
        await message.reply_text(
            "<b>Fix a payment amount</b>\n\n"
            "• Reply to the <code>5182 out</code> message with "
            f"<code>/setpayment 260</code>\n"
            "• Or: /setpayment &lt;#&gt; &lt;amount&gt; (see # column on /payments)\n"
            "Aliases: /updatepayment · /editpayment\n\n"
            f"<b>Recent payments</b>\n\n"
            f"{'\n\n'.join(blocks)}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    raw_id = context.args[0].lstrip("#")
    try:
        payment_id = int(raw_id)
    except ValueError:
        await message.reply_text(
            "Could not read that payment #. Example: /setpayment 12 260"
        )
        return

    amount_text = " ".join(context.args[1:])
    parsed = parse_payment_amount(amount_text)
    if parsed is None:
        await message.reply_text(
            "Could not read that amount. Example: /setpayment 12 260"
        )
        return

    new_amount, _ = parsed
    record = get_payment_by_id(settings.database_path, payment_id)
    if record is None:
        await message.reply_text(f"No payment with #{payment_id}.")
        return

    old_amount = record.amount
    if not update_payment_amount(
        settings.database_path, payment_id, amount=new_amount
    ):
        await message.reply_text(f"Could not update payment #{payment_id}.")
        return

    record = get_payment_by_id(settings.database_path, payment_id)
    if record is None:
        await message.reply_text(f"No payment with #{payment_id}.")
        return

    await message.reply_text(
        f"Updated #{payment_id}: "
        f"{html.escape(format_amount(old_amount))} → "
        f"<b>{html.escape(format_amount(new_amount))}</b>\n\n"
        f"{_format_payment_block(record)}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await _pm_admin_today_payments(context.bot, settings)
    schedule_payments_excel_sync(settings)
    try:
        from handlers.payment_reports import schedule_payment_report_refresh

        schedule_payment_report_refresh(context.bot, settings)
    except Exception:
        logger.exception("Payment report refresh failed")


async def removepayment_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    if message is None:
        return

    payment_id: int | None = None
    if context.args:
        payment_id = _parse_payment_id_arg(context.args[0])
    if payment_id is None:
        record = _resolve_payment_from_reply(settings.database_path, message)
        if record is not None:
            payment_id = record.id

    if payment_id is None:
        records = list_recent_payments(settings.database_path, limit=10)
        if not records:
            await message.reply_text("No payments logged yet.")
            return
        blocks = [_format_payment_block(record) for record in records]
        await message.reply_text(
            "<b>Remove a payment</b>\n\n"
            "• Reply to the <code>5182 out</code> message with /removepayment\n"
            "• Or: /removepayment &lt;#&gt; (see # column on /payments)\n\n"
            f"<b>Recent payments</b>\n\n"
            f"{'\n\n'.join(blocks)}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    record = get_payment_by_id(settings.database_path, payment_id)
    if record is None:
        await message.reply_text(f"No payment with #{payment_id}.")
        return

    amount = format_amount(record.amount)
    if not delete_payment_out(settings.database_path, payment_id):
        await message.reply_text(f"Could not remove payment #{payment_id}.")
        return

    await message.reply_text(
        f"🗑 Removed {html.escape(amount)} "
        f"(#{payment_id}) from the payment list.",
        parse_mode="HTML",
    )
    await _pm_admin_today_payments(context.bot, settings)
    schedule_payments_excel_sync(settings)
    try:
        from handlers.payment_reports import schedule_payment_report_refresh

        schedule_payment_report_refresh(context.bot, settings)
    except Exception:
        logger.exception("Payment report refresh failed")


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show Telegram user/chat ids (for admin setup)."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user:
        return
    settings: Settings = context.bot_data["settings"]
    admin = is_bot_admin(settings, settings.database_path, user.id)
    lines = [
        f"Your user id: <code>{user.id}</code>",
        f"Bot admin: {'yes' if admin else 'no'}",
    ]
    if chat:
        lines.append(f"This chat id: <code>{chat.id}</code>")
    if chat and chat.type in ("group", "supergroup"):
        me = context.bot
        username = getattr(me, "username", None) or "Q1CallManagerBot"
        lines.append(
            f"In groups, try: <code>/setpayment@{username} 67</code> "
            "(or disable privacy in @BotFather → /setprivacy)."
        )
    await message.reply_text("\n".join(lines), parse_mode="HTML")


async def paidside_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    if message is None:
        return

    if not settings.payments_onedrive_path:
        await message.reply_text(
            "OneDrive Excel export is not configured.\n\n"
            "Add PAYMENTS_ONEDRIVE_PATH to .env (synced OneDrive folder)."
        )
        return

    epoch = datetime.now(timezone.utc)
    set_paidside_epoch(settings.database_path, epoch)
    timer_msg = await message.reply_text(
        "Paid-side mode started — clearing Excel and watching for new outs…\n\n"
        f"⏳ Updating Excel… ~{SYNC_ESTIMATE_SECONDS}s remaining"
    )
    ok, detail = await excel_sync_with_timer(
        context.bot,
        chat_id=timer_msg.chat_id,
        message_id=timer_msg.message_id,
        settings=settings,
    )
    if ok:
        await message.reply_text(
            "New payment outs logged in the group will be added to Excel automatically."
        )


async def excelwebauth_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """One-time Microsoft sign-in so sync pushes straight to Excel on the web."""
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    if message is None:
        return

    from onedrive_cloud_sync import (
        build_oauth_authorize_url,
        browser_oauth_flow,
        excel_web_setup_help,
        graph_app_configured,
    )

    if not graph_app_configured(settings):
        await message.reply_text(excel_web_setup_help(settings))
        return

    if settings.cloud_deployed:
        import secrets

        state = secrets.token_urlsafe(16)
        auth_url = build_oauth_authorize_url(settings, state=state)
        if not auth_url:
            await message.reply_text(excel_web_setup_help(settings))
            return
        pending = context.bot_data.setdefault("msgraph_oauth_states", {})
        pending[state] = message.chat_id
        await message.reply_text(
            "Open this link to sign in with Microsoft "
            "(same account that owns your OneDrive file):\n\n"
            f"{auth_url}\n\n"
            "After sign-in, return here — the bot will confirm when Excel is connected."
        )
        return

    await message.reply_text(
        "Opening your browser for Microsoft sign-in…\n\n"
        "Sign in with the **same account** that owns your OneDrive file "
        "(not a work/school account unless that owns the file)."
    )

    token_data = await asyncio.to_thread(browser_oauth_flow, settings)
    if not token_data or not token_data.get("refresh_token"):
        await message.reply_text(
            "Sign-in failed or timed out.\n\n"
            f"Make sure redirect URI is exactly {settings.ms_graph_redirect_uri} in Azure, "
            "then run /excelwebauth again."
        )
        return

    set_ms_graph_refresh_token(
        settings.database_path, token_data["refresh_token"]
    )
    await message.reply_text(
        "Excel on the web is connected.\n\n"
        "Run /syncpayments — updates push to your OneDrive file. "
        "Press F5 in the browser tab if it is already open."
    )


async def syncpayments_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    if message is None:
        return

    if not settings.payments_onedrive_path:
        from onedrive_cloud_sync import graph_configured

        if not graph_configured(settings):
            await message.reply_text(
                "Payment Excel export is not configured.\n\n"
                "On Render/cloud: set MS_GRAPH_CLIENT_ID and MS_GRAPH_CLIENT_SECRET, "
                "run /excelwebauth, then /syncpayments.\n\n"
                "On PC: add PAYMENTS_ONEDRIVE_PATH to .env (synced OneDrive folder)."
            )
        else:
            await message.reply_text(
                "Cloud Excel is connected — run /syncpayments to push to OneDrive."
            )
        return

    export_all = bool(context.args) and context.args[0].strip().lower() == "all"
    if export_all:
        clear_paidside_epoch(settings.database_path)
        intro = "Syncing all payments to Excel (paid-side mode turned off)…"
    else:
        epoch = get_paidside_epoch(settings.database_path)
        if epoch is not None:
            intro = (
                "Syncing new outs to Excel (paid-side mode — only outs since "
                f"{epoch.astimezone(stats_timezone()).strftime('%d %b %Y %H:%M')})…\n\n"
                "Use /syncpayments all to export every payment."
            )
        else:
            intro = "Syncing payments to Excel…"

    timer_msg = await message.reply_text(
        f"{intro}\n\n⏳ Updating Excel… ~{SYNC_ESTIMATE_SECONDS}s remaining"
    )
    await excel_sync_with_timer(
        context.bot,
        chat_id=timer_msg.chat_id,
        message_id=timer_msg.message_id,
        settings=settings,
        all_payments=export_all,
    )


async def cleared_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_payment_cleared_command(update, context, cleared=True)


async def notcleared_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_payment_cleared_command(update, context, cleared=False)


async def todaypayments_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    records = list_payments_since(settings.database_path, since=_today_start_utc())
    text = format_today_payments_paragraph(records)
    await _pm_admin_today_payments(context.bot, settings)
    await update.effective_message.reply_text(
        f"Sent today's summary to your DM.\n\n{text}"
    )


async def clearpayments_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    total_count, _ = get_payment_totals(settings.database_path)
    if total_count == 0:
        await message.reply_text("No payment records to clear.")
        return

    prompt = await message.reply_text(
        f"⚠️ This will permanently delete all <b>{total_count}</b> payment record"
        f"{'' if total_count == 1 else 's'}.\n\n"
        "Reply to this message with <code>DELETE</code> (all capitals) to confirm.",
        parse_mode="HTML",
    )
    _pending_clear_payments_map(context.bot_data)[
        (message.chat_id, prompt.message_id)
    ] = PendingClearPayments(admin_user_id=user.id)


def _format_when(iso_timestamp: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return iso_timestamp
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if dt.date() == now.date():
        return f"today {dt.strftime('%H:%M')}"
    return dt.strftime("%d %b %H:%M")
