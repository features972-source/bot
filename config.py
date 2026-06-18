import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def _resolve_env_path() -> Path:
    explicit = os.getenv("BOT_ENV_FILE", "").strip()
    if explicit:
        path = Path(explicit)
        return path if path.is_absolute() else _ROOT / path
    return _ROOT / ".env"


def _ensure_env_loaded() -> None:
    load_dotenv(_resolve_env_path())


def _cloud_deployed() -> bool:
    if os.getenv("CLOUD_DEPLOYED", "").strip().lower() in {"1", "true", "yes"}:
        return True
    if os.getenv("RENDER", "").strip().lower() in {"true", "1", "yes"}:
        return True
    return bool(os.getenv("RENDER_EXTERNAL_URL", "").strip())


def _data_dir() -> Path | None:
    raw = os.getenv("DATA_DIR", "").strip()
    if not raw:
        return None
    return Path(raw)


def _path_is_writable(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK)


def _remap_under_root(path: str, old_root: Path, new_root: Path) -> str:
    normalized = path.replace("\\", "/")
    old = old_root.as_posix().rstrip("/")
    new = new_root.as_posix().rstrip("/")
    if normalized == old:
        return new
    prefix = f"{old}/"
    if normalized.startswith(prefix):
        return str(new_root / normalized[len(prefix) :])
    return path


def _prepare_data_directory(requested: Path) -> Path:
    """Use Render's /data disk when mounted; otherwise fall back under the app dir."""
    if requested.as_posix() == "/data":
        if _path_is_writable(requested):
            (requested / "exports").mkdir(parents=True, exist_ok=True)
            return requested
        fallback = _ROOT / "data"
        fallback.mkdir(parents=True, exist_ok=True)
        (fallback / "exports").mkdir(parents=True, exist_ok=True)
        logger.warning(
            "Cannot write to /data — add a Render persistent disk mounted at /data. "
            "Using %s until then (data is lost on redeploy).",
            fallback,
        )
        return fallback

    requested.mkdir(parents=True, exist_ok=True)
    (requested / "exports").mkdir(parents=True, exist_ok=True)
    return requested


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
    ms_graph_redirect_uri: str
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
    data_dir: str | None
    cloud_deployed: bool
    persistent_data: bool
    skip_instance_lock: bool
    public_base_url: str | None

    @property
    def threex_enabled(self) -> bool:
        return bool(self.threex_fqdn and self.threex_client_id and self.threex_api_key)

    @property
    def mailer_bridge_enabled(self) -> bool:
        return bool(self.telethon_api_id and self.telethon_api_hash)


def load_settings(env_prefix: str = "", *, optional: bool = False) -> Settings | None:
    if env_prefix and not env_prefix.endswith("_"):
        env_prefix = f"{env_prefix}_"

    _ensure_env_loaded()
    env_path = _resolve_env_path()

    def getenv(key: str, default: str = "", *, shared: bool = False) -> str:
        if env_prefix and not shared:
            return os.getenv(f"{env_prefix}{key}", default).strip()
        return os.getenv(key, default).strip()

    token = getenv("BOT_TOKEN")
    if not token:
        if optional:
            return None
        label = env_prefix.rstrip("_") or "primary"
        raise RuntimeError(
            f"BOT_TOKEN is not set for {label} bot ({env_path}). "
            "Add BOT_TOKEN or BOT2_BOT_TOKEN on Render."
        )

    admin_raw = getenv("ADMIN_CHAT_ID")
    admin_chat_id = int(admin_raw) if admin_raw else None

    notify_raw = getenv("NOTIFY_CHAT_ID")
    notify_chat_id = int(notify_raw) if notify_raw else None

    copy_raw = getenv("COPY_TO_CHAT_ID")
    copy_to_chat_id = int(copy_raw) if copy_raw else None

    listen_raw = getenv("LISTEN_CHAT_ID")
    listen_chat_id = int(listen_raw) if listen_raw else None

    credo_ids: set[int] = set()
    credo_raw = getenv("CREDO_WHITELIST_USER_IDS")
    if credo_raw:
        for part in credo_raw.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                credo_ids.add(int(part))

    card_names: list[str] = []
    cards_raw = getenv("CREDO_CREDIT_CARDS")
    if cards_raw:
        for part in cards_raw.replace("|", ",").split(","):
            name = part.strip()
            if name:
                card_names.append(name)

    secret = getenv("WEBHOOK_SECRET")
    if not secret:
        if optional:
            return None
        raise RuntimeError(f"WEBHOOK_SECRET is not set for {env_prefix or 'primary'} bot.")

    requested_data_dir = _data_dir()
    data_dir_path: Path | None = None
    data_dir: str | None = None
    cloud_deployed = _cloud_deployed()
    public_base_url = os.getenv("RENDER_EXTERNAL_URL", "").strip() or None

    database_path_raw = getenv("DATABASE_PATH")
    if requested_data_dir is None and database_path_raw.replace("\\", "/").startswith("/data/"):
        requested_data_dir = Path("/data")

    if requested_data_dir is not None:
        data_dir_path = _prepare_data_directory(requested_data_dir)
        data_dir = str(data_dir_path)

    onedrive_raw = getenv("PAYMENTS_ONEDRIVE_PATH")
    payments_onedrive_path = None
    if onedrive_raw:
        payments_onedrive_path = str(Path(onedrive_raw).expanduser().resolve())
    elif data_dir_path is not None:
        export_name = "q2.xlsx" if env_prefix.startswith("BOT2") else "q1.xlsx"
        payments_onedrive_path = str(data_dir_path / "exports" / export_name)

    worksheet = getenv("PAYMENTS_ONEDRIVE_WORKSHEET", "Payments Automatic")
    payments_onedrive_worksheet = worksheet or "Payments Automatic"

    excel_web_raw = getenv("EXCEL_WEB_URL")
    excel_web_url = excel_web_raw or None

    ms_graph_client_id = getenv("MS_GRAPH_CLIENT_ID") or None
    ms_graph_client_secret = getenv("MS_GRAPH_CLIENT_SECRET") or None

    display_name = getenv("BOT_DISPLAY_NAME", "Call Manager")
    bot_display_name = display_name or "Call Manager"

    telethon_api_id: int | None = None
    api_id_raw = getenv("TELETHON_API_ID")
    if api_id_raw.isdigit():
        telethon_api_id = int(api_id_raw)

    telethon_api_hash = getenv("TELETHON_API_HASH") or None
    db_default = (
        str(data_dir_path / ("links-bot2.db" if env_prefix.startswith("BOT2") else "links.db"))
        if data_dir_path is not None
        else ("links-bot2.db" if env_prefix.startswith("BOT2") else "links.db")
    )
    database_path = database_path_raw or db_default
    if (
        requested_data_dir is not None
        and data_dir_path is not None
        and data_dir_path != requested_data_dir
    ):
        database_path = _remap_under_root(
            database_path, requested_data_dir, data_dir_path
        )
        if onedrive_raw:
            payments_onedrive_path = _remap_under_root(
                payments_onedrive_path or "", requested_data_dir, data_dir_path
            )
        elif payments_onedrive_path:
            payments_onedrive_path = _remap_under_root(
                payments_onedrive_path, requested_data_dir, data_dir_path
            )
    if payments_onedrive_path is None and data_dir_path is not None:
        export_name = "q2.xlsx" if env_prefix.startswith("BOT2") else "q1.xlsx"
        payments_onedrive_path = str(data_dir_path / "exports" / export_name)
    db_stem = Path(database_path).stem
    if data_dir_path is not None:
        telethon_session_path = str(data_dir_path / f"mailer-{db_stem}")
    else:
        telethon_session_path = str(_ROOT / f"mailer-{db_stem}")

    mailer_bot_username = getenv("MAILER_BOT_USERNAME", "RvssianMailBot") or "RvssianMailBot"
    mailer_display = getenv("MAILER_DISPLAY_NAME", "Q1 Mailer")
    mailer_display_name = mailer_display or "Q1 Mailer"

    premium_ids: set[int] = set()
    premium_raw = getenv("Q1_PREMIUM_USER_IDS")
    if premium_raw:
        for part in premium_raw.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                premium_ids.add(int(part))

    ready_check_hour: int | None = None
    ready_hour_raw = getenv("READY_CHECK_HOUR", "9")
    if ready_hour_raw.lower() not in {"", "off", "none", "disabled"}:
        if ready_hour_raw.isdigit():
            hour = int(ready_hour_raw)
            if 0 <= hour <= 23:
                ready_check_hour = hour
    ready_check_enabled = getenv("READY_CHECK_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
    }

    currency_raw = getenv("PAYMENT_CURRENCY_SYMBOL", "£")
    currency_symbol = currency_raw or "£"

    ms_graph_redirect_uri = getenv("MS_GRAPH_REDIRECT_URI")
    if not ms_graph_redirect_uri and public_base_url:
        ms_graph_redirect_uri = f"{public_base_url.rstrip('/')}/oauth/msgraph/callback"
    elif not ms_graph_redirect_uri:
        ms_graph_redirect_uri = "http://localhost:53682/"

    listen_public_url = getenv("LISTEN_PUBLIC_URL")
    if not listen_public_url and public_base_url:
        listen_public_url = public_base_url.rstrip("/")

    skip_instance_lock = getenv("SKIP_INSTANCE_LOCK").lower() in {
        "1",
        "true",
        "yes",
    } or cloud_deployed

    persistent_data = database_path.replace("\\", "/").startswith("/data/")

    webhook_port_raw = os.getenv("PORT", "").strip() or getenv("WEBHOOK_PORT", "8080")

    return Settings(
        bot_token=token,
        admin_chat_id=admin_chat_id,
        notify_chat_id=notify_chat_id,
        copy_to_chat_id=copy_to_chat_id,
        webhook_host=getenv("WEBHOOK_HOST", "0.0.0.0", shared=True) or "0.0.0.0",
        webhook_port=int(webhook_port_raw),
        webhook_secret=secret,
        database_path=database_path,
        threex_fqdn=getenv("THREECX_FQDN"),
        threex_client_id=getenv("THREECX_CLIENT_ID"),
        threex_api_key=getenv("THREECX_API_KEY"),
        threex_admin_ext=getenv("THREECX_ADMIN_EXT"),
        listen_public_url=listen_public_url,
        listen_chat_id=listen_chat_id,
        transcript_enabled=getenv("TRANSCRIPT_ENABLED", "true").lower()
        in {"1", "true", "yes"},
        credo_whitelist_user_ids=frozenset(credo_ids),
        credo_credit_card_names=tuple(card_names),
        payments_onedrive_path=payments_onedrive_path,
        payments_onedrive_worksheet=payments_onedrive_worksheet,
        excel_web_url=excel_web_url,
        ms_graph_client_id=ms_graph_client_id,
        ms_graph_client_secret=ms_graph_client_secret,
        ms_graph_redirect_uri=ms_graph_redirect_uri,
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
        data_dir=data_dir,
        cloud_deployed=cloud_deployed,
        persistent_data=persistent_data,
        skip_instance_lock=skip_instance_lock,
        public_base_url=public_base_url,
    )
