"""Log and list outbound payment announcements (e.g. \"4943 out\", \"4.5k out\")."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import Settings
from call_display import format_extension_user_plain
from database import (
    ExtensionLink,
    PaymentLeaderboardEntry,
    PaymentRecord,
    clear_all_payments,
    clear_payments_before,
    delete_payment_out,
    count_payments_before,
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
    get_paidside_epoch,
    set_paidside_epoch,
    update_payment_cleared,
)
from handlers.admin_access import is_bot_admin, iter_bot_admin_user_ids, require_admin
from handlers.payment_table import (
    format_image_subtitle,
    render_payments_mobile_html,
    render_payments_status_html,
    status_summary_totals,
    PAYMENTS_PAGE_SIZE,
)
from handlers.payment_table_image import (
    live_report_title,
    payment_table_input_file,
    payment_table_jpeg_input_file,
    render_payments_table_png,
)
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
    REVERSED_PAYMENT_OUT_PATTERN,
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
    keep_since_utc: datetime | None = None


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
        CommandHandler("leaderboard", leaderboard_command),
        CommandHandler("outstats", leaderboard_command),
        CommandHandler("outleaderboard", leaderboard_command),
        CommandHandler("clearpayments", clearpayments_command),
        CommandHandler("clearalldata", clearalldata_command),
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
        CommandHandler("myid", myid_command),
        CallbackQueryHandler(payments_page_callback, pattern=r"^paypage:"),
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
    stripped = re.sub(r"(?<=\d),(?=\d)", "", text.strip())
    if REVERSED_PAYMENT_OUT_PATTERN is not None:
        reversed_match = REVERSED_PAYMENT_OUT_PATTERN.match(stripped)
        if reversed_match:
            return _amount_from_match(reversed_match)
    match = PAYMENT_OUT_PATTERN.match(_normalize_payment_text(text))
    if not match:
        return None
    return _amount_from_match(match)


def _effective_message_text(message) -> str | None:
    if message is None:
        return None
    text = (message.text or message.caption or "").strip()
    return text or None


def find_payment_out_in_text(text: str) -> tuple[float, str] | None:
    stripped = text.strip()
    whole = parse_payment_out(stripped)
    if whole is not None:
        return whole, stripped
    normalized = _normalize_payment_text(stripped)
    match = INLINE_PAYMENT_OUT_PATTERN.search(normalized)
    if match:
        amount = _amount_from_match(match)
        if amount is not None:
            return amount, match.group(0).strip()
    return None


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
    return f"✅ Added to the system · Payment #{payment_id}"


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
        team = f"{finisher} · starter and finisher"
    else:
        team = f"Starter {starter} → Finisher {finisher}"
    return (
        f"🔥 {amount_str} OUT 🔥\n\n"
        f"💸 {team}\n\n"
        "💳 Reply to this message and add the last 4 digits of the cc — "
        "⚠️If you fail to do so you will not be paid⚠️"
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


def format_today_payment_admin_alert(
    record: PaymentRecord,
    *,
    payment_count_today: int,
    total_amount: float,
) -> str:
    user = _stored_user_label(
        record.finisher_username,
        record.finisher_display_name,
        record.finisher_user_id,
    )
    return (
        f"{user} scored a goal. "
        f"Payment #{payment_count_today}. "
        f"Total done today is {format_amount(total_amount)}"
    )


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


async def _pm_admin_today_payments(
    bot,
    settings: Settings,
    *,
    latest: PaymentRecord | None = None,
) -> None:
    records = _today_payment_records(settings)
    if not records:
        return
    count = len(records)
    total_amount = sum(record.amount for record in records)
    record = latest or records[-1]
    text = format_today_payment_admin_alert(
        record,
        payment_count_today=count,
        total_amount=total_amount,
    )
    admin_ids = iter_bot_admin_user_ids(settings, settings.database_path)
    if not admin_ids:
        return
    for admin_id in admin_ids:
        try:
            await bot.send_message(chat_id=admin_id, text=text)
        except Exception:
            logger.exception(
                "Could not PM admin %s today's payment summary", admin_id
            )


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


def _starter_from_pass_offer_chain(
    path: str,
    reply_to,
) -> tuple[int | None, str | None, str | None] | None:
    """Resolve starter from pass-offer posts when replying to bot pass messages."""
    from database import get_pass_offer_by_notes_message, get_pass_offer_by_offer_message

    if reply_to is None:
        return None
    chat_id = reply_to.chat_id if reply_to.chat else None
    if chat_id is None:
        return None

    msg = reply_to
    depth = 0
    while msg is not None and depth < 12:
        message_id = getattr(msg, "message_id", None)
        if message_id is not None:
            offer = get_pass_offer_by_offer_message(
                path,
                chat_id=chat_id,
                offer_message_id=message_id,
            )
            if offer is not None:
                return (
                    offer.starter_user_id,
                    offer.starter_username,
                    offer.starter_display_name,
                )
            offer = get_pass_offer_by_notes_message(
                path,
                chat_id=chat_id,
                notes_message_id=message_id,
            )
            if offer is not None:
                return (
                    offer.starter_user_id,
                    offer.starter_username,
                    offer.starter_display_name,
                )
        msg = msg.reply_to_message
        depth += 1
    return None


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

    starter = _starter_from_pass_offer_chain(settings.database_path, reply_to)
    if starter is not None:
        return starter

    return None


def _display_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    return name or "Unknown"


def _payment_chat_ids(settings: Settings, bot_data: dict) -> set[int]:
    from database import get_notify_chat_id

    ids: set[int] = set()
    notify_id = (
        bot_data.get("notify_chat_id")
        or get_notify_chat_id(settings.database_path)
        or settings.notify_chat_id
    )
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
    chat = update.effective_chat
    if chat is not None and chat.type == "private":
        return True
    return _payment_chat_allowed(settings, bot_data, chat)


async def _require_payment_view(
    update: Update, settings: Settings, bot_data: dict
) -> bool:
    if _can_view_payments(update, settings, bot_data):
        return True
    message = update.effective_message
    chat = update.effective_chat
    if message is None:
        return False
    await message.reply_text(
        "Payment commands only work in the **notify / payment group** "
        "or in a private chat with the bot.",
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
    reply = _format_card_saved_reply(payment_id)
    try:
        from handlers.credo import apply_credo_usage_from_payment_out

        credo_note = apply_credo_usage_from_payment_out(
            settings,
            payment_id=payment_id,
            card_last4=card_last4,
            amount=pending.amount,
            telegram_user_id=pending.finisher_user_id,
            telegram_username=pending.finisher_username,
            display_name=pending.finisher_display_name,
        )
        if credo_note:
            reply = f"{reply}\n\n{credo_note}"
    except Exception:
        logger.exception("Credo auto-deduct failed for payment #%s", payment_id)
    await message.reply_text(reply, parse_mode="Markdown")
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
    await _pm_admin_today_payments(context.bot, settings, latest=record)
    try:
        from handlers.payment_reports import schedule_payment_report_refresh

        schedule_payment_report_refresh(context.bot, settings)
    except Exception:
        logger.exception("Payment report refresh failed")
    if settings.payments_onedrive_path:
        schedule_payments_excel_sync(settings)


async def _try_complete_pending_card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.effective_message
    user = update.effective_user
    body = _effective_message_text(message)
    if message is None or not body or message.reply_to_message is None:
        return False

    reply = message.reply_to_message
    if not reply.from_user or not reply.from_user.is_bot:
        return False

    card_map = _pending_card_map(context.bot_data)
    pending = card_map.pop((message.chat_id, reply.message_id), None)
    if pending is None and user is not None:
        last4_candidate = body.strip()
        if CARD_LAST4_PATTERN.fullmatch(last4_candidate):
            for key, candidate in list(card_map.items()):
                if (
                    key[0] == message.chat_id
                    and candidate.finisher_user_id == user.id
                ):
                    pending = card_map.pop(key)
                    break

    if pending is None:
        return False

    last4 = body.strip()
    if not CARD_LAST4_PATTERN.fullmatch(last4):
        card_map[(message.chat_id, reply.message_id)] = pending
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
            "Not confirmed. Reply to the warning with DELETE (all capitals) to confirm."
        )
        return True

    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return True

    if pending.keep_since_utc is None:
        cleared = clear_all_payments(settings.database_path)
        if cleared == 0:
            await message.reply_text("No payment records to clear.")
            return True
        await message.reply_text(
            f"Cleared {cleared} payment record(s). /payments will be empty until new "
            "outs are logged."
        )
    else:
        keep_since = pending.keep_since_utc
        cleared = clear_payments_before(settings.database_path, keep_since)
        if cleared == 0:
            await message.reply_text(
                f"No payments to delete before {_format_keep_from_label(keep_since)}."
            )
            return True
        kept_count, _ = get_payment_totals(settings.database_path, since=keep_since)
        await message.reply_text(
            f"Deleted {cleared} payment record(s) before "
            f"<b>{html.escape(_format_keep_from_label(keep_since))}</b>.\n"
            f"Kept <b>{kept_count}</b> on or after that date.",
            parse_mode="HTML",
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
    if chat.type != "private" and not _payment_chat_allowed(
        settings, context.bot_data, chat
    ):
        logger.info(
            "Ignored /out in chat %s (use DM or plain-text out in notify group)",
            chat.id,
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
    body = _effective_message_text(message)
    if not message or not user or not chat or not body:
        return

    from handlers.credo import is_add_card_flow_active

    if is_add_card_flow_active(context, user.id):
        return

    if await _try_complete_pending_card(update, context):
        return

    from handlers.expenses import try_complete_pending_expense

    if await try_complete_pending_expense(update, context):
        return

    if not _payment_chat_allowed(settings, context.bot_data, chat):
        text = _strip_leading_bot_mention(
            body, getattr(context.bot, "username", None)
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

    if await _try_complete_pending_clearpayments(update, context):
        return

    from handlers.panic import try_complete_pending_panic

    if await try_complete_pending_panic(update, context):
        return

    text = _strip_leading_bot_mention(
        body, getattr(context.bot, "username", None)
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


def _payment_page_slice(
    records: list[PaymentRecord], page: int, *, page_size: int = PAYMENTS_PAGE_SIZE
) -> tuple[list[PaymentRecord], int, int, str]:
    """Return (page_records, page_index, total_pages, page_info label)."""
    total = len(records)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page_index = max(0, min(page, total_pages - 1))
    start = page_index * page_size
    chunk = records[start : start + page_size]
    end = start + len(chunk)
    page_info = f"Page {page_index + 1}/{total_pages} · {start + 1}–{end} of {total}"
    return chunk, page_index, total_pages, page_info


def _payments_page_keyboard(
    *, scope: str, page: int, total_pages: int
) -> InlineKeyboardMarkup | None:
    if total_pages <= 1:
        return None
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(
            InlineKeyboardButton("◀ Prev", callback_data=f"paypage:{scope}:{page - 1}")
        )
    row.append(
        InlineKeyboardButton(
            f"{page + 1} / {total_pages}", callback_data="paypage:noop:0"
        )
    )
    if page < total_pages - 1:
        row.append(
            InlineKeyboardButton("Next ▶", callback_data=f"paypage:{scope}:{page + 1}")
        )
    return InlineKeyboardMarkup([row])


def _payments_summary_caption(
    *,
    since: datetime | None,
    include_admin: bool,
    total_pages: int = 1,
    page_index: int = 0,
    page_info: str = "",
) -> str | None:
    caption_parts = []
    if since is not None:
        caption_parts.append("This week’s payments")
        caption_parts.append("New week every Sunday · /alltimepayments for history")
    else:
        caption_parts.append("All payments")
    if page_info:
        caption_parts.append(page_info)
    elif total_pages > 1:
        caption_parts.append(
            f"Page {page_index + 1} of {total_pages} · tap buttons below"
        )
    if include_admin:
        caption_parts.append("Admin: /setcleared # · /setpayment # amount")
    return "\n".join(caption_parts)


def _edit_media_is_text_message(exc: BadRequest) -> bool:
    err = str(exc).lower()
    return any(
        token in err
        for token in (
            "there is no media",
            "message can't be edited",
            "message to edit not found",
            "wrong message type",
        )
    )


async def _send_payment_table_upload(
    *,
    bot,
    chat_id: int,
    caption: str,
    keyboard: InlineKeyboardMarkup | None,
    upload_fn,
    reply_message=None,
) -> None:
    upload = upload_fn()
    if reply_message is not None:
        await reply_message.reply_document(
            document=upload,
            caption=caption,
            reply_markup=keyboard,
        )
    else:
        await bot.send_document(
            chat_id=chat_id,
            document=upload_fn(),
            caption=caption,
            reply_markup=keyboard,
        )


async def _send_payment_table_photo(
    *,
    bot,
    chat_id: int,
    caption: str,
    keyboard: InlineKeyboardMarkup | None,
    upload_fn,
    reply_message=None,
) -> None:
    upload = upload_fn()
    if reply_message is not None:
        await reply_message.reply_photo(
            photo=upload,
            caption=caption,
            reply_markup=keyboard,
        )
    else:
        await bot.send_photo(
            chat_id=chat_id,
            photo=upload_fn(),
            caption=caption,
            reply_markup=keyboard,
        )


async def _deliver_payment_table_image(
    *,
    bot,
    chat_id: int,
    png: bytes,
    caption: str,
    keyboard: InlineKeyboardMarkup | None,
    edit_message=None,
    reply_message=None,
) -> None:
    """Upload table image — document first, then JPEG/PNG photo. Plain-text caption."""
    if edit_message is not None:
        media = InputMediaPhoto(
            media=payment_table_jpeg_input_file(png),
            caption=caption,
        )
        try:
            await bot.edit_message_media(
                chat_id=edit_message.chat_id,
                message_id=edit_message.message_id,
                media=media,
                reply_markup=keyboard,
            )
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            if not _edit_media_is_text_message(exc):
                logger.warning("Payment table page edit failed: %s", exc)
            else:
                logger.debug(
                    "Payment table edit failed for msg %s (%s); sending new upload",
                    edit_message.message_id,
                    exc,
                )

    senders = (
        (
            "document",
            lambda: _send_payment_table_upload(
                bot=bot,
                chat_id=chat_id,
                caption=caption,
                keyboard=keyboard,
                upload_fn=lambda: payment_table_input_file(
                    png, filename="payments-table.png"
                ),
                reply_message=reply_message,
            ),
        ),
        (
            "jpeg",
            lambda: _send_payment_table_photo(
                bot=bot,
                chat_id=chat_id,
                caption=caption,
                keyboard=keyboard,
                upload_fn=lambda: payment_table_jpeg_input_file(png),
                reply_message=reply_message,
            ),
        ),
        (
            "png",
            lambda: _send_payment_table_photo(
                bot=bot,
                chat_id=chat_id,
                caption=caption,
                keyboard=keyboard,
                upload_fn=lambda: payment_table_input_file(png),
                reply_message=reply_message,
            ),
        ),
    )

    last_error: Exception | None = None
    for label, sender in senders:
        try:
            await sender()
            return
        except TelegramError as exc:
            last_error = exc
            logger.warning("Payment table %s upload failed: %s", label, exc)

    if last_error is not None:
        raise last_error
    raise RuntimeError("Payment table upload failed with no error detail")


def _payments_message_html(caption: str, body_html: str) -> str:
    text = f"<b>{html.escape(caption)}</b>\n\n{body_html}"
    if len(text) <= 4096:
        return text
    budget = max(500, 4096 - len(caption) - 30)
    clipped_lines: list[str] = []
    used = 0
    for line in body_html.split("\n"):
        if used + len(line) + 1 > budget - 5:
            break
        clipped_lines.append(line)
        used += len(line) + 1
    clipped_lines.append("…")
    return f"<b>{html.escape(caption)}</b>\n\n" + "\n".join(clipped_lines)


async def _deliver_payments_text_table(
    *,
    bot,
    message,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
    edit_message=None,
) -> None:
    if edit_message is not None:
        try:
            await bot.edit_message_text(
                chat_id=edit_message.chat_id,
                message_id=edit_message.message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            if "there is no text" not in str(exc).lower():
                logger.debug("Payment table text edit failed: %s", exc)
    await message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def _send_payments_text_fallback(
    *,
    message,
    settings: Settings,
    records: list[PaymentRecord],
    since: datetime | None,
    page_index: int,
    page_info: str,
    include_admin: bool,
    keyboard: InlineKeyboardMarkup | None,
    page_records: list[PaymentRecord],
) -> None:
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

    status_html = render_payments_status_html(
        pending_amount=pending_amount,
        pending_count=pending_count,
        cleared_amount=cleared_amount,
        cleared_count=cleared_count,
        not_cleared_amount=not_cleared_amount,
        not_cleared_count=not_cleared_count,
    )
    body = render_payments_mobile_html(
        page_records,
        database_path=settings.database_path,
        total_amount=total_amount,
        total_count=total_count,
        lookup_records=lookup_records,
        status_html=status_html,
    )
    caption = _payments_summary_caption(
        since=since,
        include_admin=include_admin,
        total_pages=0,
        page_index=page_index,
        page_info=page_info,
    )
    text = _payments_message_html(caption, body)
    await _deliver_payments_text_table(
        bot=message.get_bot(),
        message=message,
        text=text,
        keyboard=keyboard,
    )


def _build_payments_summary_image(
    settings: Settings,
    *,
    since: datetime | None,
    period_label: str,
    all_records: list[PaymentRecord],
    page: int = 0,
) -> tuple[bytes, int, int]:
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
    title = ""
    total_label = "TOTAL"
    page_records, page_index, total_pages, page_info = _payment_page_slice(
        all_records, page
    )
    png = render_payments_table_png(
        page_records,
        database_path=settings.database_path,
        total_amount=total_amount,
        total_count=total_count,
        lookup_records=lookup_records,
        totals_records=all_records,
        title=title,
        subtitle="",
        status_totals=status_totals,
        live=False,
        full_excel=False,
        total_label=total_label,
        page_info=page_info,
    )
    return png, page_index, total_pages, page_info


async def _send_payments_summary(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    since: datetime | None,
    period_label: str,
    empty_text: str,
    page: int = 0,
    edit_message=None,
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
    scope = "week" if since is not None else "all"
    page_records, page_index, total_pages, page_info = _payment_page_slice(
        records, page
    )
    caption = _payments_summary_caption(
        since=since,
        include_admin=include_admin,
        total_pages=total_pages,
        page_index=page_index,
        page_info=page_info,
    )
    keyboard = _payments_page_keyboard(
        scope=scope, page=page_index, total_pages=total_pages
    )

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

    status_html = render_payments_status_html(
        pending_amount=pending_amount,
        pending_count=pending_count,
        cleared_amount=cleared_amount,
        cleared_count=cleared_count,
        not_cleared_amount=not_cleared_amount,
        not_cleared_count=not_cleared_count,
    )
    body = render_payments_mobile_html(
        page_records,
        database_path=settings.database_path,
        total_amount=total_amount,
        total_count=total_count,
        lookup_records=lookup_records,
        status_html=status_html,
    )
    text = _payments_message_html(caption, body)

    try:
        await _deliver_payments_text_table(
            bot=context.bot,
            message=message,
            text=text,
            keyboard=keyboard,
            edit_message=edit_message,
        )
    except Exception:
        logger.exception("Failed to send payments text table")
        await message.reply_text(
            "Could not load the payment table. Try again in a moment."
        )


async def payments_page_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "paypage":
        return
    if parts[1] == "noop":
        await query.answer()
        return

    settings: Settings = context.bot_data["settings"]
    if not await _require_payment_view(update, settings, context.bot_data):
        await query.answer("Not allowed.", show_alert=True)
        return

    try:
        page = int(parts[2])
    except ValueError:
        await query.answer()
        return

    scope = parts[1]
    if scope == "week":
        since, period_label = current_payment_week_start()
        empty_text = ""
    elif scope == "all":
        since = None
        period_label = "all time"
        empty_text = ""
    else:
        await query.answer()
        return

    await query.answer()
    await _send_payments_summary(
        update,
        context,
        since=since,
        period_label=period_label,
        empty_text=empty_text,
        page=page,
        edit_message=query.message,
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


def _parse_leaderboard_args(
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


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await _require_payment_view(update, settings, context.bot_data):
        return

    since, period_label, role_filter = _parse_leaderboard_args(context.args or [])
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
            f"💸 <b>Leaderboard</b> — {html.escape(period_label)}",
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
            "<i>/leaderboard · /leaderboard openers · /leaderboard closers · today · 7 · all</i>",
        ]
    )
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
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
leaderboard_conversation_fallback = _payment_command_conversation_fallback(
    leaderboard_command
)
out_conversation_fallback = _payment_command_conversation_fallback(out_command)


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
        lines.append("In groups: /payments, /out, /cc, /finished, /usingcc.")
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
                "On Render/cloud: set MS_GRAPH_CLIENT_ID, MS_GRAPH_CLIENT_SECRET, "
                "and complete Microsoft OAuth in Azure (redirect URI on this bot URL).\n\n"
                "On PC: add PAYMENTS_ONEDRIVE_PATH to .env (synced OneDrive folder)."
            )
        else:
            await message.reply_text(
                "Cloud Excel is connected — run /syncpayments to push to OneDrive."
            )
        return

    epoch = get_paidside_epoch(settings.database_path)
    if epoch is not None:
        intro = (
            "Syncing new outs to Excel (paid-side mode — only outs since "
            f"{epoch.astimezone(stats_timezone()).strftime('%d %b %Y %H:%M')})…"
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
        all_payments=False,
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
    await update.effective_message.reply_text(
        f"Today's summary:\n\n{text}"
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


def _parse_keep_from_date_arg(arg: str) -> datetime | None:
    """Parse a calendar date -> UTC start of that day in STATS_TIMEZONE."""
    text = arg.strip()
    tz = stats_timezone()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            local = datetime.strptime(text, fmt).replace(tzinfo=tz)
            return local.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _format_keep_from_label(since_utc: datetime) -> str:
    tz = stats_timezone()
    local = since_utc.astimezone(tz)
    return local.strftime("%d %b %Y")


async def clearalldata_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    args = context.args or []
    keep_since: datetime | None = None
    if args:
        keep_since = _parse_keep_from_date_arg(args[0])
        if keep_since is None:
            await message.reply_text(
                "Usage:\n"
                "• <code>/clearalldata</code> — delete every payment\n"
                "• <code>/clearalldata 2025-06-01</code> — delete older payments, "
                "keep from that date onward\n"
                "• <code>/clearalldata 01/06/2025</code> — same (DD/MM/YYYY)\n\n"
                "Reply <code>DELETE</code> to the confirmation prompt to proceed.",
                parse_mode="HTML",
            )
            return

    total_count, _ = get_payment_totals(settings.database_path)
    if total_count == 0:
        await message.reply_text("No payment records to clear.")
        return

    if keep_since is None:
        prompt_text = (
            f"⚠️ This will permanently delete all <b>{total_count}</b> payment record"
            f"{'' if total_count == 1 else 's'}.\n\n"
            "Tip: use <code>/clearalldata 2025-06-01</code> to keep payments from a "
            "date onward.\n\n"
            "Reply to this message with <code>DELETE</code> (all capitals) to confirm."
        )
    else:
        delete_count = count_payments_before(settings.database_path, keep_since)
        kept_count = total_count - delete_count
        keep_label = html.escape(_format_keep_from_label(keep_since))
        if delete_count == 0:
            await message.reply_text(
                f"No payments before {keep_label} to delete.\n"
                f"<b>{kept_count}</b> payment(s) on or after that date will stay.",
                parse_mode="HTML",
            )
            return
        prompt_text = (
            f"⚠️ Delete <b>{delete_count}</b> payment record"
            f"{'' if delete_count == 1 else 's'} "
            f"<b>before</b> {keep_label}.\n"
            f"Keep <b>{kept_count}</b> on or after {keep_label}.\n\n"
            "Reply to this message with <code>DELETE</code> (all capitals) to confirm."
        )

    prompt = await message.reply_text(prompt_text, parse_mode="HTML")
    _pending_clear_payments_map(context.bot_data)[
        (message.chat_id, prompt.message_id)
    ] = PendingClearPayments(admin_user_id=user.id, keep_since_utc=keep_since)


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
