from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, ConversationHandler

from config import Settings
from handlers.chat_scope import GROUP_ONLY, PM_ONLY, reject_group_command
from database import (
    ExtensionLink,
    link_extension,
    list_links,
    set_notify_chat_id,
    unlink_by_telegram_user_id,
    unlink_extension,
)
from handlers.admin_access import (
    build_admin_access_handlers,
    is_bot_admin,
    require_admin,
)
from handlers.call_stats import build_call_stats_handlers
from handlers.chat_blacklist import build_chat_blacklist_handlers
from handlers.credo import build_credo_handlers
from handlers.payments import (
    build_payment_command_handlers,
    build_payment_message_handlers,
)
from handlers.panic import build_panic_handlers
from handlers.payment_reports import build_payment_report_handlers
from handlers.premium_access import build_premium_access_handlers


def build_bot_handlers() -> list:
    return [
        *build_panic_handlers(),
        *build_payment_command_handlers(),
        # Payment text outs before credo — credo reply guard must not block ON CALL replies.
        *build_payment_message_handlers(),
        *build_credo_handlers(),
        *build_payment_report_handlers(),
        *build_call_stats_handlers(),
        *build_chat_blacklist_handlers(),
        CommandHandler("start", start_command),
        CommandHandler("help", help_command, filters=PM_ONLY),
        CommandHandler("link", link_command, filters=PM_ONLY),
        CommandHandler("unlink", unlink_command, filters=PM_ONLY),
        CommandHandler("links", links_command, filters=PM_ONLY),
        CommandHandler("users", users_command, filters=PM_ONLY),
        CommandHandler("setnotify", set_notify_command, filters=GROUP_ONLY),
        *build_admin_access_handlers(),
        *build_premium_access_handlers(),
    ]


def _format_help_text(
    *,
    admin: bool,
    credo: bool,
    bot_name: str,
    mailer_name: str,
    onedrive: bool,
) -> str:
    if admin:
        sync_line = (
            "/syncpayments — export to OneDrive Excel\n"
            "/paidside — clear Excel and track new outs only\n"
            if onedrive
            else ""
        )
        return (
            f"📱 <b>{bot_name} — commands</b>\n\n"
            "<b>💸 Payments</b>\n"
            "/payments — this week's payments (resets Sunday)\n"
            "/alltimepayments — all-time totals (/alltime works too)\n"
            "/out — log payment in DM (reply + /out 5182)\n"
            "/setcleared — mark cleared (reply to out, or use # from /todaypayments)\n"
            "/setnotcleared — mark not cleared\n"
            "/setpayment # amount — fix amount · /removepayment # — remove\n"
            f"{sync_line}"
            "/todaypayments — today's summary to your DM\n"
            "/clearpayments — wipe all (reply DELETE to confirm)\n"
            "/panic — wipe everything (reply PANIC to confirm)\n"
            "/myid — your Telegram user id\n\n"
            "<b>📞 Calls</b>\n"
            "/stats — call leaderboard (today · 7 · 30 · all)\n"
            "/missedcalls — download missed calls CSV (today · 7 · all)\n\n"
            "<b>🔗 Extensions</b>\n"
            "/link — link extension (reply to user) · /unlink · /links · /users\n"
            "/setnotify — set announcement group\n"
            "/setnotifypayments — live payment list in a group (pick Q1/Q2, auto-updates)\n\n"
            "<b>👑 Admins</b>\n"
            "/admin · /addadmin · /removeadmin — manage bot admins\n\n"
            "<b>🚫 Blacklist</b>\n"
            "/blacklist @user reason · /unblacklist @user · /blacklisted\n\n"
            "<b>💳 Credos</b>\n"
            "/blastmode — turn card picking on/off for the team\n"
            "/blastmode off · /blastmodeoff — lock /cc\n"
            "/cc · /creditcard · /credo · /credos — only when blast mode is on\n"
            "/addcredouser · /removecredouser · /credousers\n"
            "/addcredo · /removecredo · /listcredocards\n"
            "/addpremium · /removepremium · /premiumusers\n"
            f"/mail — {mailer_name} in DM · /maildone to stop\n"
            "/maillogs — recent /mail audit trail (admin)\n\n"
            "<i>Tip: in the group, reply to notes or ON CALL with 5182 out. "
            "Other commands — DM the bot.</i>"
        )

    if credo:
        return (
            f"💳 <b>Credo & {mailer_name}</b>\n\n"
            "/cc — view cards & pick one when blast mode is on (group or DM)\n"
            "(also /creditcard, /credo, /credos)\n"
            "/finished — when done · /mail still works\n"
            f"/mail — open {mailer_name} via the bot (DM only)\n"
            "/maildone — end mailer session\n"
            "/cancel — cancel an in-progress flow"
        )

    return (
        f"📱 <b>{bot_name}</b>\n\n"
        "Call announcements run in your team group. Bot commands are DM-only "
        f"(except /cc when blast mode is on).\n\n"
        f"<b>📧 {mailer_name}</b>\n"
        f"/mail — open {mailer_name} in this DM (joins queue if busy)\n"
        "/maildone — end session or leave queue"
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
    await message.reply_text(
        _format_help_text(
            admin=admin,
            credo=credo,
            bot_name=settings.bot_display_name,
            mailer_name=settings.mailer_display_name,
            onedrive=bool(settings.payments_onedrive_path),
        ),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        from handlers.credo import CREDO_START_ARGS, credos_start_resume

        if context.args[0] in CREDO_START_ARGS:
            await credos_start_resume(update, context)
            return

    chat = update.effective_chat
    if chat is not None and chat.type in ("group", "supergroup"):
        if await reject_group_command(update):
            return

    settings: Settings = context.bot_data["settings"]
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    bot_name = settings.bot_display_name
    admin = is_bot_admin(settings, settings.database_path, user.id)
    if admin:
        text = (
            f"📱 <b>{bot_name}</b>\n\n"
            "Call announcements post to your group automatically.\n\n"
            "Send /help for the full command list."
        )
    else:
        from handlers.credo import is_credo_allowed

        if is_credo_allowed(settings, settings.database_path, user.id):
            text = "💳 <b>Credos</b>\n\nSend /cc (or /credos) to view cards and capacity, or /help for commands."
        else:
            text = (
                f"📱 <b>{bot_name}</b>\n\n"
                "This bot handles call announcements and payments in your team group.\n\n"
                f"DM /mail to use {settings.mailer_display_name}, or /help for commands."
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
