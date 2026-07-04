from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from config import Settings
from database import (
    ExtensionLink,
    link_extension,
    list_links,
    clear_expense_report_message_id,
    get_expense_report_chat_id,
    set_expense_logging_chat_id,
    set_expense_report_chat_id,
    set_notify_chat_id,
    unlink_by_telegram_user_id,
    unlink_extension,
)
from handlers.admin_access import (
    build_admin_access_handlers,
    is_bot_admin,
    is_primary_admin,
    require_admin,
)
from handlers.call_stats import build_call_stats_handlers
from handlers.chat_blacklist import build_chat_blacklist_handlers
from handlers.credo import build_credo_handlers
from handlers.expense_reports import build_expense_report_handlers
from handlers.expenses import build_expense_command_handlers, build_expense_message_handlers
from handlers.payments import (
    build_payment_command_handlers,
)
from handlers.panic import build_panic_handlers
from handlers.payment_reports import build_payment_report_handlers
from handlers.premium_access import build_premium_access_handlers
from handlers.nemesis import build_nemesis_handlers
from handlers.profit_export import build_profit_export_handlers
from handlers.remind import build_remind_handlers
from handlers.mypay import build_mypay_handlers
from handlers.blast import build_blast_handlers
from handlers.attendance import build_attendance_handlers
from handlers.pay_buttons import build_pay_buttons_handlers
from handlers.payout import build_payout_handlers
from handlers.admin_payments import build_admin_payments_handlers


def build_credo_bot_handlers() -> list:
    """Handlers for the credo-only bot (cc / card management, no payments or calls)."""
    return [
        *build_credo_handlers(credo_only=True),
        CommandHandler("start", start_command),
        CommandHandler("help", help_command),
        *build_admin_access_handlers(),
    ]


async def _delete_pin_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Silently delete the 'pinned a message' service message Telegram sends to the group."""
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass


def build_bot_handlers() -> list:
    return [
        *build_panic_handlers(),
        *build_payment_command_handlers(),
        *build_profit_export_handlers(),
        *build_expense_command_handlers(),
        # Expense wizard before credo — catch step replies in shared groups.
        *build_expense_message_handlers(),
        *build_credo_handlers(),
        *build_expense_report_handlers(),
        *build_payment_report_handlers(),
        *build_call_stats_handlers(),
        *build_chat_blacklist_handlers(),
        CommandHandler("start", start_command),
        CommandHandler("help", help_command),
        CommandHandler("link", link_command),
        CommandHandler("unlink", unlink_command),
        CommandHandler("links", links_command),
        CommandHandler("users", users_command),
        CommandHandler("setnotify", set_notify_command),
        CommandHandler("setnotifyexpenses", set_notify_expenses_command),
        *build_admin_access_handlers(),
        *build_premium_access_handlers(),
        *build_nemesis_handlers(),
        *build_remind_handlers(),
        *build_mypay_handlers(),
        *build_blast_handlers(),
        *build_attendance_handlers(),
        *build_pay_buttons_handlers(),
        *build_payout_handlers(),
        *build_admin_payments_handlers(),
        MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, _delete_pin_service_message),
    ]


def _format_credo_only_help_text(*, admin: bool, credo: bool, bot_name: str, primary: bool = False) -> str:
    if primary:
        return (
            f"💳 <b>{bot_name} — owner</b>\n\n"
            "<b>Subscription</b>\n"
            "/genkey — create a 4-week license key\n"
            "Share key → they send /start and paste it\n"
            "/redeemkey &lt;key&gt; — extend bot 4 weeks only\n"
            "/subscription · /keys — status &amp; unused keys\n\n"
            "<b>Cards</b>\n"
            "/cc — view cards &amp; pick one\n"
            "/addcredo · /listcredocards · /setlimit · /removecredo\n"
            "/addcredouser · /removecredouser · /credousers"
        )

    if admin:
        return (
            f"💳 <b>{bot_name} — credo commands</b>\n\n"
            "<b>Cards</b>\n"
            "/cc — view cards & pick one (group or DM)\n"
            "(also /creditcard, /credo, /credos)\n"
            "/activeccs · /usingcc — see which cards are in use\n"
            "/finished — end your active card session\n"
            "/cancel — cancel an in-progress flow\n\n"
            "<b>Admin</b> (expires with your key)\n"
            "/addcredo — add a card (DM wizard)\n"
            "/listcredocards — list all cards\n"
            "/setlimit — set amount left (e.g. /setlimit Lloyds #2 10000)\n"
            "/removecredo — remove a card\n"
            "/addcredouser · /removecredouser · /credousers\n"
            "/subscription — your admin &amp; bot expiry"
        )

    if credo:
        return (
            f"💳 <b>{bot_name}</b>\n\n"
            "/cc — view cards & pick one (group or DM)\n"
            "(also /creditcard, /credo, /credos)\n"
            "/activeccs · /usingcc — see which cards are in use\n"
            "/finished — when done (works in group or DM)\n"
            "/cancel — cancel an in-progress flow"
        )

    return (
        f"💳 <b>{bot_name}</b>\n\n"
        "The bot is not active yet.\n\n"
        "Send /start and enter a license key, or ask the owner for one."
    )


def _format_help_text(
    *,
    admin: bool,
    credo: bool,
    bot_name: str,
    mailer_name: str,
    onedrive: bool,
) -> str:
    if admin:
        return (
            f"<blockquote expandable>📱 <b>{bot_name} — commands</b>\n\n"
            "💸 <b>PAYMENTS</b>\n"
            "▪️ <b>/payments</b> — this week's payments (resets Sunday)\n"
            "▪️ <b>/adminpayments</b> — full list: every payment with starter, finisher, card, status\n"
            "▪️ <b>/paybuttons</b> — tap to mark each payment cleared/not cleared\n"
            "▪️ <b>/payout</b> — who you owe (cleared payments only, auto-updates for new payments)\n"
            "▪️ <b>/setpaid</b> @user — mark agent as paid, removes from /payout\n"
            "▪️ <b>/paylog</b> — history of who you've paid\n"
            "▪️ <b>/alltimepayments</b> — all-time totals (/alltime works too)\n"
            "▪️ <b>/leaderboard</b> — opener & closer rankings (today · 7 · all)\n"
            "▪️ <b>/nemesis</b> @user — challenge someone (they tap Yes to start)\n"
            "▪️ <b>/out</b> — log payment (reply + /out 5182)\n"
            "▪️ <b>/setcleared</b> — mark cleared (reply to out)\n"
            "▪️ <b>/setnotcleared</b> — mark not cleared\n"
            "▪️ <b>/setpayment</b> # amount — fix amount · /removepayment # — remove\n"
            "▪️ <b>/clearpayments</b> — wipe all (reply DELETE to confirm)\n"
            "▪️ <b>/clearalldata</b> — wipe payments (optional keep-from date)\n"
            "▪️ <b>/panic</b> — wipe everything (reply PANIC to confirm)\n"
            "▪️ <b>/myid</b> — your Telegram user id\n\n"
            "📞 <b>CALLS</b>\n"
            "▪️ <b>/attendance</b> — linked agents & call counts\n"
            "▪️ <b>/resetattendance</b> — reset attendance counts\n"
            "▪️ <b>/missedcalls</b> — download missed calls CSV (today · 7 · all)\n\n"
            "🔗 <b>EXTENSIONS</b>\n"
            "▪️ <b>/link</b> — link extension (reply to user) · /unlink · /links · /users\n"
            "▪️ <b>/clearlinks</b> — unlink all agents\n"
            "▪️ <b>/setnotify</b> — set announcement group\n"
            "▪️ <b>/setnotifypayments</b> — live payment list in a group\n"
            "▪️ <b>/setnotifyexpenses</b> — set expenses logging group\n"
            "▪️ <b>/setexpenses</b> — live expense table in a group\n"
            "▪️ <b>/expense</b> — log an expense step-by-step (who · amount · where)\n\n"
            "👑 <b>ADMINS</b>\n"
            "▪️ <b>/admin</b> · <b>/addadmin</b> · <b>/removeadmin</b> — manage bot admins\n\n"
            "💳 <b>CREDOS</b>\n"
            "▪️ <b>/cc</b> · <b>/creditcard</b> · <b>/credo</b> · <b>/credos</b>\n"
            "▪️ <b>/activeccs</b> · <b>/usingcc</b> — see which cards are currently in use\n"
            "▪️ <b>/addcredouser</b> · <b>/removecredouser</b> · <b>/credousers</b>\n"
            "▪️ <b>/addcredo</b> · <b>/removecredo</b> · <b>/listcredocards</b> · <b>/setlimit</b>\n"
            f"▪️ <b>/mail</b> — {mailer_name} · /maildone to stop\n\n"
            "📣 <b>BROADCAST</b>\n"
            "▪️ <b>/blast</b> [message] — pin message in group\n"
            "▪️ <b>/content</b> — show current blast content\n"
            "▪️ <b>/remind</b> [time] [note] — set a personal reminder\n\n"
            "🔔 <i>Tip: reply to notes or ON CALL with 5182 out, or use /payments and /out.</i></blockquote>"
        )

    if credo:
        return (
            f"<blockquote>💳 <b>Credo & {mailer_name}</b>\n\n"
            "▪️ <b>/cc</b> — view cards & pick one (group or DM)\n"
            "(also /creditcard, /credo, /credos)\n"
            "▪️ <b>/activeccs</b> · <b>/usingcc</b> — see which cards are in use\n"
            "▪️ <b>/finished</b> — when done (works in group or DM)\n\n"
            f"▪️ <b>/mail</b> — open {mailer_name} (multi-step flow works best in DM)\n"
            "▪️ <b>/maildone</b> — end mailer session\n"
            "▪️ <b>/cancel</b> — cancel an in-progress flow</blockquote>"
        )

    return (
        f"<blockquote>📱 <b>{bot_name}</b>\n\n"
        "Call announcements and bot commands run in your team group.\n\n"
        f"📧 <b>{mailer_name}</b>\n"
        f"▪️ <b>/mail</b> — open {mailer_name} (multi-step flow works best in DM)\n"
        "▪️ <b>/maildone</b> — end session or leave queue</blockquote>"
    )


async def _send_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers.credo import is_credo_allowed

    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    admin = is_bot_admin(settings, settings.database_path, user.id)
    credo = is_credo_allowed(settings, settings.database_path, user.id)
    if settings.credo_only_mode:
        text = _format_credo_only_help_text(
            admin=admin,
            credo=credo,
            bot_name=settings.bot_display_name,
            primary=is_primary_admin(settings, user.id),
        )
    else:
        text = _format_help_text(
            admin=admin,
            credo=credo,
            bot_name=settings.bot_display_name,
            mailer_name=settings.mailer_display_name,
            onedrive=bool(settings.payments_onedrive_path),
        )
    await message.reply_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        from handlers.credo import CREDO_START_ARGS, credos_start_resume

        if context.args[0] in CREDO_START_ARGS:
            await credos_start_resume(update, context)
            return

    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    bot_name = settings.bot_display_name
    admin = is_bot_admin(settings, settings.database_path, user.id)
    if settings.credo_only_mode:
        from handlers.credo import is_credo_allowed
        from handlers.credo_subscription import prompt_for_license_key

        if is_primary_admin(settings, user.id):
            text = (
                f"💳 <b>{bot_name}</b>\n\n"
                "Credo card bot — manage cards and whitelist.\n\n"
                "Send /help for the full command list."
            )
        elif admin:
            text = (
                f"💳 <b>{bot_name}</b>\n\n"
                "You're an admin.\n\n"
                "Send /help for commands or /cc to view cards."
            )
        elif is_credo_allowed(settings, settings.database_path, user.id):
            text = (
                "💳 <b>Credos</b>\n\n"
                "Send /cc (or /credos) to view cards and capacity, or /help for commands."
            )
        else:
            await prompt_for_license_key(update, context)
            return
    elif admin:
        text = (
            f"<blockquote>📱 <b>{bot_name}</b>\n\n"
            "Call announcements post to your group automatically.\n\n"
            "Send /help for the full command list.</blockquote>"
        )
    else:
        from handlers.credo import is_credo_allowed

        if is_credo_allowed(settings, settings.database_path, user.id):
            text = "<blockquote>💳 <b>Credos</b>\n\nSend /cc (or /credos) to view cards and capacity, or /help for commands.</blockquote>"
        else:
            text = (
                f"<blockquote>📱 <b>{bot_name}</b>\n\n"
                "This bot handles call announcements and payments in your team group.\n\n"
                f"DM /mail to use {settings.mailer_display_name}, or /help for commands.</blockquote>"
            )

    await message.reply_text(text, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_help(update, context)


async def help_conversation_fallback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data.clear()
    await help_command(update, context)
    return ConversationHandler.END


async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    if not context.args or len(context.args) != 1:
        await update.effective_message.reply_text(
            "Reply to someone's message with:\n/link 101"
        )
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.effective_message.reply_text("Reply to the Telegram user you want to link.")
        return

    extension = context.args[0].strip()
    target = update.message.reply_to_message.from_user

    link_extension(
        settings.database_path,
        extension=extension,
        telegram_user_id=target.id,
        telegram_username=target.username,
        display_name=_display_name(target),
    )

    label = f"@{target.username}" if target.username else _display_name(target)
    await update.effective_message.reply_text(
        f"Linked 3CX extension {extension} to {label}."
    )


async def unlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    if context.args and len(context.args) == 1:
        extension = context.args[0].strip()
        if unlink_extension(settings.database_path, extension):
            await update.effective_message.reply_text(f"Removed link for extension {extension}.")
        else:
            await update.effective_message.reply_text(f"No link found for extension {extension}.")
        return

    reply = update.message.reply_to_message if update.message else None
    if reply and reply.from_user:
        removed = unlink_by_telegram_user_id(settings.database_path, reply.from_user.id)
        if removed is None:
            await update.effective_message.reply_text("No link found for that user.")
            return
        await update.effective_message.reply_text(
            f"Removed {_link_label(removed)} (ext {removed.extension})."
        )
        return

    await update.effective_message.reply_text(
        "Usage: /unlink 101\nOr reply to a user's message with /unlink"
    )


async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    links = list_links(settings.database_path)
    if not links:
        await update.effective_message.reply_text("No extensions linked yet.")
        return

    lines = []
    for item in links:
        if item.telegram_username:
            label = f"@{item.telegram_username}"
        elif item.display_name:
            label = item.display_name
        else:
            label = str(item.telegram_user_id)
        lines.append(f"{item.extension} → {label}")

    await update.effective_message.reply_text("Linked extensions:\n" + "\n".join(lines))


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    links = list_links(settings.database_path)
    if not links:
        await update.effective_message.reply_text("No extensions linked yet.")
        return

    lines = []
    for item in links:
        username = f"@{item.telegram_username}" if item.telegram_username else "—"
        display = item.display_name or "—"
        lines.append(
            f"{item.extension} · {username} · {display} · id {item.telegram_user_id}"
        )

    await update.effective_message.reply_text(
        "Linked users:\n"
        + "\n".join(lines)
        + "\n\nRemove: /unlink 101 or reply to a user with /unlink"
    )


async def set_notify_expenses_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(
            "Run this command inside your expenses group."
        )
        return

    set_expense_logging_chat_id(settings.database_path, chat.id)
    if get_expense_report_chat_id(settings.database_path) is None:
        set_expense_report_chat_id(settings.database_path, chat.id)
        clear_expense_report_message_id(settings.database_path)
    from handlers.admin_access import sync_bot_command_menu
    from handlers.expense_reports import refresh_expense_report

    await sync_bot_command_menu(context.bot, settings)
    await refresh_expense_report(context.bot, settings, chat_id=chat.id)
    await update.effective_message.reply_text(
        f"**Expense logging** will use this group.\n"
        f"Chat id: `{chat.id}`\n\n"
        "Use **/expense** or post lines like `£132 Tesco` here.\n"
        "The expense table will post and update in this group.",
        parse_mode="Markdown",
    )


async def set_notify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not await require_admin(update, settings):
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text("Run this command inside your announcement group.")
        return

    set_notify_chat_id(settings.database_path, chat.id)
    context.bot_data["notify_chat_id"] = chat.id
    from handlers.admin_access import sync_bot_command_menu

    await sync_bot_command_menu(context.bot, settings)
    await update.effective_message.reply_text(
        f"Announcements and **payment logging** will use this group.\n"
        f"Chat id: `{chat.id}`\n\n"
        "Saved to the database — survives restarts and redeploys.",
        parse_mode="Markdown",
    )


def _display_name(user) -> str:
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(part for part in parts if part).strip()
    return name or "Unknown"


def _link_label(link: ExtensionLink) -> str:
    if link.telegram_username:
        return f"@{link.telegram_username}"
    if link.display_name:
        return link.display_name
    return str(link.telegram_user_id)
