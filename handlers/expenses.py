"""Log expenses in the expenses group (quick line or /expense wizard)."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from config import Settings
from database import (
    ExpenseRecord,
    delete_expense,
    get_expense_by_id,
    get_expense_by_message,
    get_expense_logging_chat_id,
    list_links,
    list_recent_expenses,
    record_expense,
)
from handlers.admin_access import USERNAME_TOKEN, _MinimalUser, _resolve_target_user, _user_label, require_admin
from handlers.payment_table import format_payment_date
from money_format import format_amount, parse_expense_amount, parse_expense_line

logger = logging.getLogger(__name__)

PENDING_EXPENSES_KEY = "pending_expenses"
STEP_WHO = "who"
STEP_AMOUNT = "amount"
STEP_WHERE = "where"


@dataclass
class PendingExpense:
    step: str
    chat_id: int
    user_id: int
    subject_user_id: int | None = None
    subject_username: str | None = None
    subject_display_name: str | None = None
    amount: float | None = None
    cleanup_message_ids: list[int] = field(default_factory=list)


def build_expense_message_handlers() -> list:
    return [
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            expense_message,
            block=False,
        ),
    ]


def build_expense_command_handlers() -> list:
    return [
        CommandHandler("expense", expense_command, block=False),
        CommandHandler("removeexpense", removeexpense_command, block=False),
        CommandHandler("cancel", expense_cancel_command, block=False),
    ]


def _expense_chat_allowed(settings: Settings, chat) -> bool:
    if chat is None:
        return False
    expense_chat_id = get_expense_logging_chat_id(settings.database_path)
    if expense_chat_id is None:
        return False
    return chat.id == expense_chat_id


def _display_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    return name or "Unknown"


def _pending_expense_map(bot_data: dict) -> dict[tuple[int, int], PendingExpense]:
    return bot_data.setdefault(PENDING_EXPENSES_KEY, {})


def _pending_key(chat_id: int, user_id: int) -> tuple[int, int]:
    return (chat_id, user_id)


def _clear_pending_expense(context: ContextTypes.DEFAULT_TYPE, *, chat_id: int, user_id: int) -> None:
    _pending_expense_map(context.bot_data).pop(_pending_key(chat_id, user_id), None)


def _get_pending_expense(
    context: ContextTypes.DEFAULT_TYPE, *, chat_id: int, user_id: int
) -> PendingExpense | None:
    return _pending_expense_map(context.bot_data).get(_pending_key(chat_id, user_id))


def _set_pending_expense(context: ContextTypes.DEFAULT_TYPE, pending: PendingExpense) -> None:
    _pending_expense_map(context.bot_data)[
        _pending_key(pending.chat_id, pending.user_id)
    ] = pending


def _track_cleanup(pending: PendingExpense, *message_ids: int | None) -> None:
    seen = set(pending.cleanup_message_ids)
    for message_id in message_ids:
        if message_id is None or message_id in seen:
            continue
        pending.cleanup_message_ids.append(message_id)
        seen.add(message_id)


async def _delete_messages(bot, chat_id: int, message_ids: list[int]) -> None:
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except BadRequest:
            pass
        except Exception:
            logger.warning("Could not delete message %s in chat %s", message_id, chat_id)


async def _cleanup_wizard_messages(
    bot, pending: PendingExpense, *, extra_message_ids: list[int] | None = None
) -> None:
    ids = list(pending.cleanup_message_ids)
    if extra_message_ids:
        ids.extend(extra_message_ids)
    if ids:
        await _delete_messages(bot, pending.chat_id, ids)


def _subject_from_user(user) -> tuple[int, str | None, str | None]:
    return user.id, getattr(user, "username", None), _display_name(user)


def _link_to_user(link) -> _MinimalUser:
    return _MinimalUser(
        link.telegram_user_id,
        username=link.telegram_username,
        first_name=link.display_name or "",
    )


def _resolve_by_name_or_username(text: str, database_path: str):
    query = text.strip()
    if not query:
        return None

    if query.startswith("@"):
        match = USERNAME_TOKEN.match(query)
        if match:
            username = match.group(1).lower()
            for link in list_links(database_path):
                if (
                    link.telegram_username
                    and link.telegram_username.lower().lstrip("@") == username
                ):
                    return _link_to_user(link)
            from database import list_bot_admins, list_credo_whitelist

            for entry in list_bot_admins(database_path):
                if (
                    entry.telegram_username
                    and entry.telegram_username.lower().lstrip("@") == username
                ):
                    return _MinimalUser(
                        entry.telegram_user_id,
                        username=entry.telegram_username,
                        first_name=entry.display_name or "",
                    )
            for entry in list_credo_whitelist(database_path):
                if (
                    entry.telegram_username
                    and entry.telegram_username.lower().lstrip("@") == username
                ):
                    return _MinimalUser(
                        entry.telegram_user_id,
                        username=entry.telegram_username,
                        first_name=entry.display_name or "",
                    )
        return None

    lowered = query.lower()
    exact: list = []
    partial: list = []
    for link in list_links(database_path):
        display = (link.display_name or "").strip()
        username = (link.telegram_username or "").lstrip("@")
        if display.lower() == lowered or username.lower() == lowered:
            exact.append(link)
            continue
        if display and lowered in display.lower():
            partial.append(link)
        elif username and lowered in username.lower():
            partial.append(link)

    if len(exact) == 1:
        return _link_to_user(exact[0])
    if len(exact) > 1:
        return None
    if len(partial) == 1:
        return _link_to_user(partial[0])
    return None


def _resolve_expense_subject(
    update: Update, settings: Settings, *, args: list[str] | None = None
):
    message = update.effective_message
    if not message:
        return None

    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        if not user.is_bot:
            return user

    for entity in message.entities or []:
        if entity.type == "text_mention" and entity.user and not entity.user.is_bot:
            return entity.user

    if message.text and message.entities:
        for entity in message.entities:
            if entity.type != "mention":
                continue
            username = message.text[entity.offset : entity.offset + entity.length].lstrip(
                "@"
            )
            for link in list_links(settings.database_path):
                if (
                    link.telegram_username
                    and link.telegram_username.lower().lstrip("@") == username.lower()
                ):
                    return _link_to_user(link)

    if args:
        resolved = _resolve_target_user(update, args, database_path=settings.database_path)
        if resolved is not None:
            return resolved

    text = (message.text or "").strip()
    if not text:
        return None

    resolved = _resolve_target_user(update, [text], database_path=settings.database_path)
    if resolved is not None:
        return resolved

    return _resolve_by_name_or_username(text, settings.database_path)


async def _finalize_expense(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingExpense,
    *,
    where: str,
) -> None:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    if not message or not user or pending.subject_user_id is None or pending.amount is None:
        _clear_pending_expense(context, chat_id=pending.chat_id, user_id=pending.user_id)
        return

    where = where.strip()
    if not where:
        await message.reply_text("Where was it to? Send a short description (e.g. `Tesco`).")
        return

    raw_text = (
        f"/expense · {_user_label_from_pending(pending)} · "
        f"{format_amount(pending.amount)} · {where}"
    )
    expense_id = record_expense(
        settings.database_path,
        telegram_user_id=pending.subject_user_id,
        telegram_username=pending.subject_username,
        display_name=pending.subject_display_name,
        amount=pending.amount,
        raw_text=raw_text,
        reason=where,
        chat_id=pending.chat_id,
        telegram_message_id=message.message_id,
    )
    _clear_pending_expense(context, chat_id=pending.chat_id, user_id=pending.user_id)
    if expense_id is None:
        await message.reply_text("Could not save that expense (duplicate message?).")
        return

    logger.info(
        "expense logged chat=%s by=%s subject=%s amount=%s where=%r id=%s",
        pending.chat_id,
        user.id,
        pending.subject_user_id,
        pending.amount,
        where,
        expense_id,
    )

    await _cleanup_wizard_messages(
        context.bot,
        pending,
        extra_message_ids=[message.message_id],
    )

    from handlers.expense_reports import refresh_expense_report

    posted = await refresh_expense_report(
        context.bot, settings, chat_id=pending.chat_id
    )
    if not posted:
        await context.bot.send_message(
            chat_id=pending.chat_id,
            text=(
                "Expense saved but the table could not be posted. "
                "Ask an admin to run /setnotifyexpenses and ensure the bot can send photos."
            ),
        )


def _user_label_from_pending(pending: PendingExpense) -> str:
    user = _MinimalUser(
        pending.subject_user_id or 0,
        username=pending.subject_username,
        first_name=pending.subject_display_name or "",
    )
    return _user_label(user)


async def expense_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return

    if not _expense_chat_allowed(settings, chat):
        expense_chat_id = get_expense_logging_chat_id(settings.database_path)
        if expense_chat_id is None:
            await message.reply_text(
                "Expenses are not set up yet. An admin runs **/setnotifyexpenses** in the expenses group first.",
                parse_mode="Markdown",
            )
        else:
            await message.reply_text(
                "Run **/expense** in the expenses group (where **/setnotifyexpenses** was configured).",
                parse_mode="Markdown",
            )
        return

    subject = _resolve_expense_subject(update, settings, args=context.args or [])
    if subject is not None:
        user_id, username, display_name = _subject_from_user(subject)
        pending = PendingExpense(
            step=STEP_AMOUNT,
            chat_id=chat.id,
            user_id=user.id,
            subject_user_id=user_id,
            subject_username=username,
            subject_display_name=display_name,
            cleanup_message_ids=[message.message_id],
        )
        _set_pending_expense(context, pending)
        prompt = await message.reply_text(
            f"🧾 Expense for **{_user_label(subject)}**\n\nHow much was it? (e.g. `132` or `£132`)",
            parse_mode="Markdown",
        )
        _track_cleanup(pending, prompt.message_id)
        _set_pending_expense(context, pending)
        return

    pending = PendingExpense(step=STEP_WHO, chat_id=chat.id, user_id=user.id)
    _track_cleanup(pending, message.message_id)
    _set_pending_expense(context, pending)
    prompt = await message.reply_text(
        "🧾 **New expense**\n\n"
        "Whose expense was it?\n"
        "Reply to **their** message, tag `@username`, or type their linked name.",
        parse_mode="Markdown",
    )
    _track_cleanup(pending, prompt.message_id)
    _set_pending_expense(context, pending)


async def expense_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    pending = _get_pending_expense(context, chat_id=chat.id, user_id=user.id)
    if pending is None:
        return
    await _cleanup_wizard_messages(context.bot, pending, extra_message_ids=[message.message_id])
    _clear_pending_expense(context, chat_id=chat.id, user_id=user.id)


async def try_complete_pending_expense(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or not message.text:
        return False

    pending = _get_pending_expense(context, chat_id=chat.id, user_id=user.id)
    if pending is None:
        return False

    settings: Settings = context.bot_data["settings"]
    if chat.id != pending.chat_id:
        return False
    if not _expense_chat_allowed(settings, chat):
        _clear_pending_expense(context, chat_id=chat.id, user_id=user.id)
        return False

    text = message.text.strip()
    _track_cleanup(pending, message.message_id)

    if pending.step == STEP_WHO:
        subject = _resolve_expense_subject(update, settings)
        if subject is None:
            prompt = await message.reply_text(
                "Couldn't find that person.\n\n"
                "• Reply to **their** message (not the bot's)\n"
                "• Or tag `@username`\n"
                "• Or type their linked name from /links"
            )
            _track_cleanup(pending, prompt.message_id)
            _set_pending_expense(context, pending)
            return True
        user_id, username, display_name = _subject_from_user(subject)
        pending.subject_user_id = user_id
        pending.subject_username = username
        pending.subject_display_name = display_name
        pending.step = STEP_AMOUNT
        prompt = await message.reply_text(
            f"How much was it for **{_user_label(subject)}**? (e.g. `132` or `£132`)",
            parse_mode="Markdown",
        )
        _track_cleanup(pending, prompt.message_id)
        _set_pending_expense(context, pending)
        return True

    if pending.step == STEP_AMOUNT:
        parsed = parse_expense_amount(text)
        if parsed is None:
            prompt = await message.reply_text(
                f"Send the amount only (e.g. `132` or `{format_amount(132)}`)."
            )
            _track_cleanup(pending, prompt.message_id)
            _set_pending_expense(context, pending)
            return True
        amount, _ = parsed
        pending.amount = amount
        pending.step = STEP_WHERE
        prompt = await message.reply_text(
            f"**{format_amount(amount)}** — where was it to? (e.g. `Tesco`, `Uber`, `Office supplies`)",
            parse_mode="Markdown",
        )
        _track_cleanup(pending, prompt.message_id)
        _set_pending_expense(context, pending)
        return True

    if pending.step == STEP_WHERE:
        await _finalize_expense(update, context, pending, where=text)
        return True

    return False


async def expense_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or not message.text:
        return

    from handlers.credo import is_add_card_flow_active

    if is_add_card_flow_active(context, user.id):
        return

    if await try_complete_pending_expense(update, context):
        return

    if not _expense_chat_allowed(settings, chat):
        return

    from handlers.payments import _payment_chat_allowed, looks_like_payment_out

    if _payment_chat_allowed(settings, context.bot_data, chat) and looks_like_payment_out(
        message.text, getattr(context.bot, "username", None)
    ):
        return

    parsed = parse_expense_line(message.text)
    if parsed is None:
        return

    amount, reason, raw_text = parsed
    expense_id = record_expense(
        settings.database_path,
        telegram_user_id=user.id,
        telegram_username=user.username,
        display_name=_display_name(user),
        amount=amount,
        raw_text=raw_text,
        reason=reason,
        chat_id=chat.id,
        telegram_message_id=message.message_id,
    )
    if expense_id is None:
        return

    logger.info(
        "expense logged chat=%s user=%s amount=%s reason=%r id=%s",
        chat.id,
        user.id,
        amount,
        reason,
        expense_id,
    )

    from handlers.expense_reports import refresh_expense_report

    await _delete_messages(context.bot, chat.id, [message.message_id])
    await refresh_expense_report(context.bot, settings, chat_id=chat.id)


def _parse_expense_id_arg(raw: str) -> int | None:
    try:
        return int(raw.lstrip("#"))
    except ValueError:
        return None


def _resolve_expense_from_reply(database_path: str, message) -> ExpenseRecord | None:
    if message is None or message.reply_to_message is None:
        return None
    chat_id = message.chat_id
    current = message.reply_to_message
    for _ in range(6):
        if current is None:
            break
        record = get_expense_by_message(
            database_path,
            chat_id=chat_id,
            telegram_message_id=current.message_id,
        )
        if record is not None:
            return record
        current = current.reply_to_message
    return None


def _format_expense_block(record: ExpenseRecord) -> str:
    user = _MinimalUser(
        record.telegram_user_id,
        username=record.telegram_username,
        first_name=record.display_name or "",
    )
    when = format_payment_date(record.created_at, compact=True)
    return (
        f"#{record.id} · {_user_label(user)} · {format_amount(record.amount)} · "
        f"{html.escape(record.reason)} ({html.escape(when)})"
    )


async def removeexpense_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings, deny_message="Admin only."):
        return

    message = update.effective_message
    if message is None:
        return

    expense_id: int | None = None
    if context.args:
        expense_id = _parse_expense_id_arg(context.args[0])
    if expense_id is None:
        record = _resolve_expense_from_reply(settings.database_path, message)
        if record is not None:
            expense_id = record.id

    if expense_id is None:
        records = list_recent_expenses(settings.database_path, limit=10)
        if not records:
            await message.reply_text("No expenses logged yet.")
            return
        blocks = [_format_expense_block(record) for record in records]
        await message.reply_text(
            "<b>Remove an expense</b>\n\n"
            "• Reply to the expense message with /removeexpense\n"
            "• Or: /removeexpense &lt;#&gt; (see # on the expense table)\n\n"
            f"<b>Recent expenses</b>\n\n"
            f"{'\n\n'.join(blocks)}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    record = get_expense_by_id(settings.database_path, expense_id)
    if record is None:
        await message.reply_text(f"No expense with #{expense_id}.")
        return

    amount = format_amount(record.amount)
    if not delete_expense(settings.database_path, expense_id):
        await message.reply_text(f"Could not remove expense #{expense_id}.")
        return

    await message.reply_text(
        f"🗑 Removed {html.escape(amount)} "
        f"(#{expense_id}) — {html.escape(record.reason)}.",
        parse_mode="HTML",
    )

    from handlers.expense_reports import refresh_expense_report

    await refresh_expense_report(context.bot, settings)
