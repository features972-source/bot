"""Log expenses in the expenses group (quick line or /expense wizard)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from config import Settings
from database import get_expense_logging_chat_id, list_links, record_expense
from handlers.admin_access import _resolve_target_user, _user_label
from money_format import format_amount, parse_expense_amount, parse_expense_line

logger = logging.getLogger(__name__)

PENDING_EXPENSE_KEY = "pending_expense"
STEP_WHO = "who"
STEP_AMOUNT = "amount"
STEP_WHERE = "where"


@dataclass
class PendingExpense:
    step: str
    chat_id: int
    subject_user_id: int | None = None
    subject_username: str | None = None
    subject_display_name: str | None = None
    amount: float | None = None


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


def _clear_pending_expense(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(PENDING_EXPENSE_KEY, None)


def _get_pending_expense(context: ContextTypes.DEFAULT_TYPE) -> PendingExpense | None:
    raw = context.user_data.get(PENDING_EXPENSE_KEY)
    if raw is None:
        return None
    if isinstance(raw, PendingExpense):
        return raw
    return None


def _set_pending_expense(context: ContextTypes.DEFAULT_TYPE, pending: PendingExpense) -> None:
    context.user_data[PENDING_EXPENSE_KEY] = pending


def _subject_from_user(user) -> tuple[int, str | None, str | None]:
    return user.id, getattr(user, "username", None), _display_name(user)


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
                    from handlers.admin_access import _MinimalUser

                    return _MinimalUser(
                        link.telegram_user_id,
                        username=link.telegram_username,
                        first_name=link.display_name or "",
                    )

    if args:
        return _resolve_target_user(update, args, database_path=settings.database_path)

    text = (message.text or "").strip()
    if text:
        return _resolve_target_user(update, [text], database_path=settings.database_path)

    return None


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
        _clear_pending_expense(context)
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
    _clear_pending_expense(context)
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

    from handlers.expense_reports import schedule_expense_report_refresh

    schedule_expense_report_refresh(context.bot, settings)

    subject = _user_label_from_pending(pending)
    await message.reply_text(
        f"✅ Logged **{format_amount(pending.amount)}** for **{subject}** → {where} (#{expense_id})",
        parse_mode="Markdown",
    )


def _user_label_from_pending(pending: PendingExpense) -> str:
    from handlers.admin_access import _MinimalUser

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
            subject_user_id=user_id,
            subject_username=username,
            subject_display_name=display_name,
        )
        _set_pending_expense(context, pending)
        await message.reply_text(
            f"🧾 Expense for **{_user_label(subject)}**\n\nHow much was it? (e.g. `132` or `£132`)",
            parse_mode="Markdown",
        )
        return

    _set_pending_expense(
        context,
        PendingExpense(step=STEP_WHO, chat_id=chat.id),
    )
    await message.reply_text(
        "🧾 **New expense**\n\n"
        "Whose expense was it?\n"
        "Reply to their message, tag `@username`, or type their name.",
        parse_mode="Markdown",
    )


async def expense_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = _get_pending_expense(context)
    if pending is None:
        return
    _clear_pending_expense(context)
    message = update.effective_message
    if message:
        await message.reply_text("Expense cancelled.")


async def _try_complete_pending_expense(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    pending = _get_pending_expense(context)
    if pending is None:
        return False

    settings: Settings = context.bot_data["settings"]
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or not message.text:
        return False
    if chat.id != pending.chat_id:
        return False
    if not _expense_chat_allowed(settings, chat):
        _clear_pending_expense(context)
        return False

    text = message.text.strip()

    if pending.step == STEP_WHO:
        subject = _resolve_expense_subject(update, settings)
        if subject is None:
            await message.reply_text(
                "Couldn't find that person. Reply to their message, use `@username`, "
                "or pick someone linked with /link."
            )
            return True
        user_id, username, display_name = _subject_from_user(subject)
        pending.subject_user_id = user_id
        pending.subject_username = username
        pending.subject_display_name = display_name
        pending.step = STEP_AMOUNT
        _set_pending_expense(context, pending)
        await message.reply_text(
            f"How much was it for **{_user_label(subject)}**? (e.g. `132` or `£132`)",
            parse_mode="Markdown",
        )
        return True

    if pending.step == STEP_AMOUNT:
        parsed = parse_expense_amount(text)
        if parsed is None:
            await message.reply_text(
                f"Send the amount only (e.g. `132` or `{format_amount(132)}`)."
            )
            return True
        amount, _ = parsed
        pending.amount = amount
        pending.step = STEP_WHERE
        _set_pending_expense(context, pending)
        await message.reply_text(
            f"**{format_amount(amount)}** — where was it to? (e.g. `Tesco`, `Uber`, `Office supplies`)",
            parse_mode="Markdown",
        )
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

    if await _try_complete_pending_expense(update, context):
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

    from handlers.expense_reports import schedule_expense_report_refresh

    schedule_expense_report_refresh(context.bot, settings)

    await message.reply_text(
        f"✅ Logged **{format_amount(amount)}** · {reason} (#{expense_id})",
        parse_mode="Markdown",
    )
