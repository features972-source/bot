import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent


def _resolve_env_path() -> Path:
    explicit = os.getenv("BOT_ENV_FILE", "").strip()
    if explicit:
        path = Path(explicit)
        return path if path.is_absolute() else _ROOT / path
    return _ROOT / ".env"


def _ensure_env_loaded() -> None:
    load_dotenv(_resolve_env_path())


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_chat_id: int | None
    notify_chat_id: int | None
    copy_to_chat_id: int | None
    webhook_host: str
    webhook_port: int
    webhook_secret: str
    database_path: str
    threex_fqdn: str
    threex_client_id: str
    threex_api_key: str
    threex_admin_ext: str
    listen_public_url: str
    listen_chat_id: int | None
    transcript_enabled: bool
    credo_whitelist_user_ids: frozenset[int]
    credo_credit_card_names: tuple[str, ...]
    payments_onedrive_path: str | None
    payments_onedrive_worksheet: str
    excel_web_url: str | None
    ms_graph_client_id: str | None
    ms_graph_client_secret: str | None
    bot_display_name: str
    telethon_api_id: int | None
    telethon_api_hash: str | None
    telethon_session_path: str
    mailer_bot_username: str
    mailer_display_name: str
    q1_premium_user_ids: frozenset[int]
    ready_check_hour: int | None
    ready_check_enabled: bool
    currency_symbol: str

    @property
    def threex_enabled(self) -> bool:
        return bool(self.threex_fqdn and self.threex_client_id and self.threex_api_key)

    @property
    def mailer_bridge_enabled(self) -> bool:
        return bool(self.telethon_api_id and self.telethon_api_hash)


def load_settings() -> Settings:
    _ensure_env_loaded()
    env_path = _resolve_env_path()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            f"BOT_TOKEN is not set in {env_path}. "
            "Create a new bot via @BotFather and add the token."
        )

    admin_raw = os.getenv("ADMIN_CHAT_ID", "").strip()
    admin_chat_id = int(admin_raw) if admin_raw else None

    notify_raw = os.getenv("NOTIFY_CHAT_ID", "").strip()
    notify_chat_id = int(notify_raw) if notify_raw else None

    copy_raw = os.getenv("COPY_TO_CHAT_ID", "").strip()
    copy_to_chat_id = int(copy_raw) if copy_raw else None

    listen_raw = os.getenv("LISTEN_CHAT_ID", "").strip()
    listen_chat_id = int(listen_raw) if listen_raw else None

    credo_ids: set[int] = set()
    credo_raw = os.getenv("CREDO_WHITELIST_USER_IDS", "").strip()
    if credo_raw:
        for part in credo_raw.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                credo_ids.add(int(part))

    card_names: list[str] = []
    cards_raw = os.getenv("CREDO_CREDIT_CARDS", "").strip()
    if cards_raw:
        for part in cards_raw.replace("|", ",").split(","):
            name = part.strip()
            if name:
                card_names.append(name)

    secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if not secret:
        raise RuntimeError(f"WEBHOOK_SECRET is not set in {env_path}.")

    onedrive_raw = os.getenv("PAYMENTS_ONEDRIVE_PATH", "").strip()
    payments_onedrive_path = None
    if onedrive_raw:
        payments_onedrive_path = str(Path(onedrive_raw).expanduser().resolve())

    worksheet = os.getenv("PAYMENTS_ONEDRIVE_WORKSHEET", "Payments Automatic").strip()
    payments_onedrive_worksheet = worksheet or "Payments Automatic"

    excel_web_raw = os.getenv("EXCEL_WEB_URL", "").strip()
    excel_web_url = excel_web_raw or None

    ms_graph_client_id = os.getenv("MS_GRAPH_CLIENT_ID", "").strip() or None
    ms_graph_client_secret = os.getenv("MS_GRAPH_CLIENT_SECRET", "").strip() or None

    display_name = os.getenv("BOT_DISPLAY_NAME", "Call Manager").strip()
    bot_display_name = display_name or "Call Manager"

    telethon_api_id: int | None = None
    api_id_raw = os.getenv("TELETHON_API_ID", "").strip()
    if api_id_raw.isdigit():
        telethon_api_id = int(api_id_raw)

    telethon_api_hash = os.getenv("TELETHON_API_HASH", "").strip() or None
    db_stem = Path(os.getenv("DATABASE_PATH", "links.db")).stem
    telethon_session_path = str(_ROOT / f"mailer-{db_stem}")

    mailer_bot_username = (
        os.getenv("MAILER_BOT_USERNAME", "RvssianMailBot").strip() or "RvssianMailBot"
    )
    mailer_display = os.getenv("MAILER_DISPLAY_NAME", "Q1 Mailer").strip()
    mailer_display_name = mailer_display or "Q1 Mailer"

    premium_ids: set[int] = set()
    premium_raw = os.getenv("Q1_PREMIUM_USER_IDS", "").strip()
    if premium_raw:
        for part in premium_raw.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                premium_ids.add(int(part))

    ready_check_hour: int | None = None
    ready_hour_raw = os.getenv("READY_CHECK_HOUR", "9").strip()
    if ready_hour_raw.lower() not in {"", "off", "none", "disabled"}:
        if ready_hour_raw.isdigit():
            hour = int(ready_hour_raw)
            if 0 <= hour <= 23:
                ready_check_hour = hour
    ready_check_enabled = os.getenv("READY_CHECK_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
    }

    currency_raw = os.getenv("PAYMENT_CURRENCY_SYMBOL", "£").strip()
    currency_symbol = currency_raw or "£"

    return Settings(
        bot_token=token,
        admin_chat_id=admin_chat_id,
        notify_chat_id=notify_chat_id,
        copy_to_chat_id=copy_to_chat_id,
        webhook_host=os.getenv("WEBHOOK_HOST", "0.0.0.0"),
        webhook_port=int(os.getenv("WEBHOOK_PORT", "8080")),
        webhook_secret=secret,
        database_path=os.getenv("DATABASE_PATH", "links.db"),
        threex_fqdn=os.getenv("THREECX_FQDN", "").strip(),
        threex_client_id=os.getenv("THREECX_CLIENT_ID", "").strip(),
        threex_api_key=os.getenv("THREECX_API_KEY", "").strip(),
        threex_admin_ext=os.getenv("THREECX_ADMIN_EXT", "").strip(),
        listen_public_url=os.getenv("LISTEN_PUBLIC_URL", "").strip(),
        listen_chat_id=listen_chat_id,
        transcript_enabled=os.getenv("TRANSCRIPT_ENABLED", "true").strip().lower()
        in {"1", "true", "yes"},
        credo_whitelist_user_ids=frozenset(credo_ids),
        credo_credit_card_names=tuple(card_names),
        payments_onedrive_path=payments_onedrive_path,
        payments_onedrive_worksheet=payments_onedrive_worksheet,
        excel_web_url=excel_web_url,
        ms_graph_client_id=ms_graph_client_id,
        ms_graph_client_secret=ms_graph_client_secret,
        bot_display_name=bot_display_name,
        telethon_api_id=telethon_api_id,
        telethon_api_hash=telethon_api_hash,
        telethon_session_path=telethon_session_path,
        mailer_bot_username=mailer_bot_username,
        mailer_display_name=mailer_display_name,
        q1_premium_user_ids=frozenset(premium_ids),
        ready_check_hour=ready_check_hour,
        ready_check_enabled=ready_check_enabled,
        currency_symbol=currency_symbol,
    )
