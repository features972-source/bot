"""Admin call control panel with reply + inline buttons."""



from __future__ import annotations



import logging



from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest

from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters



from config import Settings
from database import list_links
from handlers.admin_access import is_bot_admin, require_admin
from call_end import mark_telegram_hangup

from handlers.listen_live import start_live_listen_with_fallback

from threex_api import (
    ActiveCall,
    ThreeCXApiError,
    admin_extension,
    format_active_calls,
    get_client,
    list_all_active_calls,
)



logger = logging.getLogger(__name__)



ADMIN_KEYBOARD = ReplyKeyboardMarkup(

    [[KeyboardButton("📋 Active calls")]],

    resize_keyboard=True,

)



CALLBACK_REFRESH = "ref"

CALLBACK_HANGUP = "h"

CALLBACK_LISTEN = "l"

CALLBACK_TRANSFER_MENU = "tx"

CALLBACK_TRANSFER_DO = "txd"

CALLBACK_LISTEN_PHONE = "lp"





def build_admin_handlers() -> list:

    return [

        CommandHandler("panel", panel_command),

        CallbackQueryHandler(admin_callback, pattern=r"^(ref|h|l|tx|txd|lp):"),

        MessageHandler(filters.Regex(r"^📋 Active calls$"), active_calls_button),

    ]





async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    settings: Settings = context.bot_data["settings"]

    if not await require_admin(update, settings):

        return

    await update.effective_message.reply_text(

        "🎛️ Admin control panel\n\n"

        "Tap **📋 Active calls** — then **🔴 Listen live** or **📞 Listen on phone**.",

        parse_mode="Markdown",

        reply_markup=ADMIN_KEYBOARD,

    )





async def active_calls_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    settings: Settings = context.bot_data["settings"]

    if not await require_admin(update, settings):

        return

    await _send_active_calls_panel(update, context)





async def send_active_calls_for_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    await _send_active_calls_panel(update, context)





async def _send_active_calls_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    settings: Settings = context.bot_data["settings"]

    if not await _require_3cx(update, settings):

        return



    links = list_links(settings.database_path)

    if not links:

        await update.effective_message.reply_text("No extensions linked yet.")

        return



    client = get_client(context.bot_data, settings)

    extensions = [link.extension for link in links]



    try:

        active_calls = await list_all_active_calls(client, settings, extensions)

    except ThreeCXApiError as exc:

        await update.effective_message.reply_text(f"3CX API error: {exc}")

        return



    text = format_active_calls(active_calls)

    markup = _active_calls_keyboard(active_calls, links)



    await update.effective_message.reply_text(

        text + "\n\n👇 Tap a button below to act on a call.",

        reply_markup=markup,

    )





def _active_calls_keyboard(

    active_calls: list[ActiveCall],

    links: list[ExtensionLink],

) -> InlineKeyboardMarkup:

    rows: list[list[InlineKeyboardButton]] = []



    for call in active_calls:

        label = call.extension

        if call.link and call.link.telegram_username:

            label = f"{call.extension} @{call.link.telegram_username}"

        rows.append(

            [

                InlineKeyboardButton(

                    f"⛔ Hang up {label}",

                    callback_data=f"{CALLBACK_HANGUP}:{call.extension}:{call.participant_id}",

                ),

            ]

        )

        rows.append(

            [

                InlineKeyboardButton(

                    f"🔴 Listen live {label}",

                    callback_data=f"{CALLBACK_LISTEN}:{call.extension}:{call.participant_id}",

                ),

                InlineKeyboardButton(

                    f"↪ Transfer {label}",

                    callback_data=f"{CALLBACK_TRANSFER_MENU}:{call.extension}:{call.participant_id}",

                ),

            ]

        )



    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"{CALLBACK_REFRESH}:")])

    return InlineKeyboardMarkup(rows)





def _transfer_target_keyboard(

    from_ext: str,

    participant_id: int,

    links: list[ExtensionLink],

) -> InlineKeyboardMarkup:

    rows: list[list[InlineKeyboardButton]] = []

    for link in links:

        if link.extension == from_ext:

            continue

        label = link.extension

        if link.telegram_username:

            label = f"{link.extension} @{link.telegram_username}"

        rows.append(

            [

                InlineKeyboardButton(

                    f"→ {label}",

                    callback_data=(

                        f"{CALLBACK_TRANSFER_DO}:{from_ext}:{participant_id}:{link.extension}"

                    ),

                )

            ]

        )

    rows.append([InlineKeyboardButton("« Back", callback_data=f"{CALLBACK_REFRESH}:")])

    return InlineKeyboardMarkup(rows)





async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:

    query = update.callback_query

    if query is None:

        return



    settings: Settings = context.bot_data["settings"]

    if not is_bot_admin(settings, settings.database_path, query.from_user.id):

        await query.answer("You are not authorized.", show_alert=True)

        return



    data = query.data or ""

    parts = data.split(":")

    action = parts[0]



    if action == CALLBACK_REFRESH:

        await query.answer()

        await _refresh_panel(query, context)

        return



    if not settings.threex_enabled:

        await query.answer()

        await query.edit_message_text("3CX Call Control is not configured.")

        return



    client = get_client(context.bot_data, settings)



    if action == CALLBACK_HANGUP and len(parts) == 3:

        await query.answer()

        ext, pid = parts[1], int(parts[2])

        try:
            actor = query.from_user
            if actor:
                label = (
                    f"@{actor.username}" if actor.username else actor.first_name or str(actor.id)
                )
                mark_telegram_hangup(context.bot_data, ext, label=label)

            await client.drop_participant(ext, pid)

            await query.edit_message_text(f"✅ 📴 Hung up call on extension {ext}.")

        except ThreeCXApiError as exc:

            await query.edit_message_text(f"Hangup failed: {exc}")

        return



    if action == CALLBACK_LISTEN and len(parts) == 3:

        ext, pid = parts[1], int(parts[2])

        await query.answer("Opening listen…")

        caller_name = ""

        caller_number = ""

        try:

            participant = await client.get_participant(ext, pid)

            if participant:

                caller_name = str(participant.get("party_caller_name") or "")

                caller_number = str(participant.get("party_caller_id") or "")

        except ThreeCXApiError:

            pass

        message, markup = await start_live_listen_with_fallback(

            context.bot_data,

            settings,

            context.bot,

            extension=ext,

            participant_id=pid,

            caller_name=caller_name,

            caller_number=caller_number,

        )

        try:

            await query.edit_message_text(

                message,

                parse_mode=ParseMode.HTML,

                reply_markup=markup,

                disable_web_page_preview=True,

            )

        except BadRequest as exc:

            logger.warning("Could not edit listen message, sending new: %s", exc)

            await query.message.reply_text(

                message,

                parse_mode=ParseMode.HTML,

                reply_markup=markup,

                disable_web_page_preview=True,

            )

        return



    if action == CALLBACK_LISTEN_PHONE and len(parts) == 3:

        ext, pid = parts[1], int(parts[2])

        await query.answer("Ringing listen extension…")

        admin_ext = admin_extension(settings)

        try:

            await client.listen_participant(ext, pid, admin_ext)

            await query.message.reply_text(

                f"📞 Answer extension <b>{admin_ext}</b> on your 3CX app to listen live.",

                parse_mode=ParseMode.HTML,

            )

        except ThreeCXApiError as exc:

            await query.message.reply_text(f"Phone listen failed: {exc}")

        return



    await query.answer()



    if action == CALLBACK_TRANSFER_MENU and len(parts) == 3:

        from_ext, pid = parts[1], int(parts[2])

        links = list_links(settings.database_path)

        targets = [link for link in links if link.extension != from_ext]

        if not targets:

            await query.edit_message_text(

                f"No other linked extensions to transfer ext {from_ext} to."

            )

            return

        await query.edit_message_text(

            f"Transfer call on ext **{from_ext}** to:",

            parse_mode="Markdown",

            reply_markup=_transfer_target_keyboard(from_ext, pid, links),

        )

        return



    if action == CALLBACK_TRANSFER_DO and len(parts) == 4:

        from_ext, pid, to_ext = parts[1], int(parts[2]), parts[3]

        try:

            await client.transfer_participant(from_ext, pid, to_ext)

            await query.edit_message_text(

                f"✅ 🔀 Transferring call from ext {from_ext} to ext {to_ext}."

            )

        except ThreeCXApiError as exc:

            await query.edit_message_text(f"Transfer failed: {exc}")

        return





async def _refresh_panel(query, context: ContextTypes.DEFAULT_TYPE) -> None:

    settings: Settings = context.bot_data["settings"]

    links = list_links(settings.database_path)

    if not links:

        await query.edit_message_text("No extensions linked yet.")

        return



    client = get_client(context.bot_data, settings)

    extensions = [link.extension for link in links]



    try:

        active_calls = await list_all_active_calls(client, settings, extensions)

    except ThreeCXApiError as exc:

        await query.edit_message_text(f"3CX API error: {exc}")

        return



    text = format_active_calls(active_calls) + "\n\nTap a button below to act on a call."

    await query.edit_message_text(

        text,

        reply_markup=_active_calls_keyboard(active_calls, links),

    )





async def _require_3cx(update: Update, settings: Settings) -> bool:

    if settings.threex_enabled:

        return True

    await update.effective_message.reply_text(

        "3CX Call Control is not configured. Set THREECX_FQDN, THREECX_CLIENT_ID, "

        "and THREECX_API_KEY in .env."

    )

    return False





def admin_reply_keyboard(settings: Settings, user_id: int) -> ReplyKeyboardMarkup | None:

    if is_bot_admin(settings, settings.database_path, user_id):

        return ADMIN_KEYBOARD

    return None


