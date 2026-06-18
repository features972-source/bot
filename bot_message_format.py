"""Wrap outgoing Telegram bot text in bold by default."""

from __future__ import annotations

from functools import wraps
from typing import Any

_PATCHED = False


def bold_text(text: str | None, parse_mode: str | None) -> tuple[str | None, str | None]:
    if text is None:
        return text, parse_mode
    raw = str(text)
    if not raw.strip():
        return text, parse_mode

    pm = (parse_mode or "").upper()
    if "MARKDOWN" in pm:
        stripped = raw.strip()
        if stripped.startswith("**") and stripped.endswith("**"):
            return raw, parse_mode
        return f"**{raw}**", parse_mode

    stripped = raw.strip()
    if stripped.startswith("<b>") and stripped.endswith("</b>"):
        return raw, parse_mode or "HTML"
    return f"<b>{raw}</b>", parse_mode or "HTML"


def _bold_caption_media(media: Any) -> Any:
    caption = getattr(media, "caption", None)
    if not caption:
        return media
    cap, pm = bold_text(caption, getattr(media, "parse_mode", None))
    media_type = type(media)
    try:
        return media_type(
            media=media.media,
            caption=cap,
            parse_mode=pm,
            **{
                key: getattr(media, key)
                for key in ("has_spoiler", "show_caption_above_media")
                if hasattr(media, key)
            },
        )
    except TypeError:
        return media_type(media=media.media, caption=cap, parse_mode=pm)


def apply_bot_bold_patch() -> None:
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    from telegram import Bot, Message

    orig_reply_text = Message.reply_text

    @wraps(orig_reply_text)
    async def reply_text_bold(self, text, *args, parse_mode=None, **kwargs):
        text, parse_mode = bold_text(text, parse_mode)
        return await orig_reply_text(self, text, *args, parse_mode=parse_mode, **kwargs)

    Message.reply_text = reply_text_bold

    orig_edit_text = Message.edit_text

    @wraps(orig_edit_text)
    async def edit_text_bold(self, text, *args, parse_mode=None, **kwargs):
        text, parse_mode = bold_text(text, parse_mode)
        return await orig_edit_text(self, text, *args, parse_mode=parse_mode, **kwargs)

    Message.edit_text = edit_text_bold

    orig_reply_photo = Message.reply_photo

    @wraps(orig_reply_photo)
    async def reply_photo_bold(self, photo, *args, caption=None, parse_mode=None, **kwargs):
        if caption is not None:
            caption, parse_mode = bold_text(caption, parse_mode)
        return await orig_reply_photo(
            self, photo, *args, caption=caption, parse_mode=parse_mode, **kwargs
        )

    Message.reply_photo = reply_photo_bold

    orig_send_message = Bot.send_message

    @wraps(orig_send_message)
    async def send_message_bold(self, chat_id, text, *args, parse_mode=None, **kwargs):
        text, parse_mode = bold_text(text, parse_mode)
        return await orig_send_message(
            self, chat_id, text, *args, parse_mode=parse_mode, **kwargs
        )

    Bot.send_message = send_message_bold

    orig_edit_message_text = Bot.edit_message_text

    @wraps(orig_edit_message_text)
    async def edit_message_text_bold(self, text, *args, parse_mode=None, **kwargs):
        text, parse_mode = bold_text(text, parse_mode)
        return await orig_edit_message_text(
            self, text, *args, parse_mode=parse_mode, **kwargs
        )

    Bot.edit_message_text = edit_message_text_bold

    orig_send_photo = Bot.send_photo

    @wraps(orig_send_photo)
    async def send_photo_bold(self, chat_id, photo, *args, caption=None, parse_mode=None, **kwargs):
        if caption is not None:
            caption, parse_mode = bold_text(caption, parse_mode)
        return await orig_send_photo(
            self,
            chat_id,
            photo,
            *args,
            caption=caption,
            parse_mode=parse_mode,
            **kwargs,
        )

    Bot.send_photo = send_photo_bold

    orig_edit_message_media = Bot.edit_message_media

    @wraps(orig_edit_message_media)
    async def edit_message_media_bold(self, chat_id, message_id, media, *args, **kwargs):
        media = _bold_caption_media(media)
        return await orig_edit_message_media(
            self, chat_id, message_id, media, *args, **kwargs
        )

    Bot.edit_message_media = edit_message_media_bold
