"""No dotenv autoloading: mock configuration never reads a model credential."""

import ipaddress
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROGRAM_BASE_URL = "https://shared1.multitool.works:4000/v1"
OFFICIAL_BASE_URL = "https://foundation-models.api.cloud.ru/v1"
ALLOWED_MODEL_BASE_URLS = {PROGRAM_BASE_URL, OFFICIAL_BASE_URL}
LLM_READ_TIMEOUT_SECONDS = 90
LLM_READ_TIMEOUT_RANGE = (30, 240)
RESPONSE_BYTE_LIMIT = 256000


def stream_deadline_seconds(read_timeout: int) -> int:
    """Total wall-clock limit for a streamed answer: twice the read timeout, at most 300 s."""
    return min(read_timeout * 2, 300)


def parse_seconds_list(name: str, value: str) -> tuple[int, ...]:
    """Comma separated pauses in seconds, for example 60,300."""
    try:
        return tuple(int(part) for part in value.split(",") if part.strip())
    except ValueError:
        raise ValueError(f"{name} must be a comma separated list of whole seconds") from None


def read_secret_file(path: str, *, maximum: int = 16384) -> str:
    value = Path(path).read_text(encoding="utf-8")
    if len(value) > maximum:
        raise ValueError("Secret file is too large")
    value = value.strip("\r\n")
    if not value or "\x00" in value:
        raise ValueError("Secret file is empty or invalid")
    return value


def parse_decimal(name: str, value: str) -> Decimal:
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        raise ValueError(f"{name} must be a decimal number") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"{name} must not be negative")
    return number


def check_fallback_url(value: str) -> None:
    """Токен посредника уходит по этому адресу. По http можно только внутри закрытой сети."""
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.query or parts.fragment:
        raise ValueError("NOTES_FALLBACK_BASE_URL must be a plain http(s) URL")
    if parts.scheme == "http":
        try:
            private = ipaddress.ip_address(parts.hostname).is_private
        except ValueError:
            private = False
        if not private:
            raise ValueError("NOTES_FALLBACK_BASE_URL over http must point to a private IP address")


DEFAULT_ADMIN_NETWORKS = "127.0.0.1/32,::1/128,10.77.0.0/24"


def parse_networks(value: str):
    """Comma separated CIDRs. A catch-all network would defeat the private-network rule."""
    networks = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        network = ipaddress.ip_network(part, strict=False)
        if network.prefixlen == 0:
            raise ValueError("NOTES_ADMIN_ALLOWED_NETWORKS must not contain a catch-all network")
        networks.append(network)
    if not networks:
        raise ValueError("NOTES_ADMIN_ALLOWED_NETWORKS must list at least one network")
    return tuple(networks)


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./data/notes.db"
    provider: Literal["mock", "cloudru"] = "mock"
    auto_worker: bool = True
    secure_cookies: bool = False
    public_origin: str = "http://127.0.0.1:8000"
    allow_registration: bool = True
    allow_live_requests: bool = False
    cloudru_model: str = ""
    cloudru_base_url: str = PROGRAM_BASE_URL
    live_call_limit: int = 0
    live_user_call_limit: int = 0
    max_pending_per_user: int = 10
    daily_unit_limit: int = 30
    # Новый аккаунт моложе суток тратит ИИ осторожнее. 0 отключает этот потолок.
    new_account_unit_limit: int = 10
    registrations_per_ip_per_day: int = 20
    api_docs_enabled: bool = False
    text_unit_cost: int = 1
    audio_unit_base: int = 2
    audio_unit_per_minute: int = 1
    limit_timezone: str = "Europe/Moscow"
    session_seconds: int = 86400
    lease_seconds: int = 120
    # Ждём ответ модели не дольше этого времени на одно чтение. Потоковый режим включают только после ручной проверки.
    llm_read_timeout_seconds: int = LLM_READ_TIMEOUT_SECONDS
    llm_stream: bool = False
    # Автоповторы ИИ. Подробности в docs/ai-reliability-v1.md. Повторы делает только воркер.
    ai_retries_enabled: bool = True
    # Сколько секунд ждать перед каждым повтором. Число пауз равно числу повторов.
    retry_clear_delays: tuple[int, ...] = (60, 300)
    retry_ambiguous_delays: tuple[int, ...] = (120,)
    retry_bad_output_delays: tuple[int, ...] = (60,)
    retry_jitter: float = 0.2
    retry_after_max_seconds: int = 900
    retry_user_per_hour: int = 10
    retry_global_per_hour: int = 60
    # Предохранитель шлюза. Столько сбоев (разных записей) за окно останавливают все повторы на паузу.
    retry_breaker_failures: int = 5
    retry_breaker_window_seconds: int = 300
    retry_breaker_pause_seconds: int = 600
    delivery_lease_seconds: int = 60
    # Резервный ИИ через посредника в Хельсинки. Подробности в docs/ai-failover-v1.md. По умолчанию выключен.
    fallback_enabled: bool = False
    fallback_base_url: str = ""
    fallback_token: str = field(default="", repr=False)
    fallback_model: str = "gpt-4o"
    fallback_stt_model: str = "whisper-1"
    # Потолок расхода на резерв за календарный день в часовом поясе лимитов. Доллары США.
    fallback_daily_usd: Decimal = Decimal("10")
    # Цены для оценки расхода. Владелец сверяет их с прайсом OpenAI. Доллары за миллион токенов и за минуту речи.
    fallback_price_input_per_m: Decimal = Decimal("2.50")
    fallback_price_output_per_m: Decimal = Decimal("10.00")
    fallback_price_stt_per_minute: Decimal = Decimal("0.006")
    # Пока основной шлюз упал, раз в столько секунд одно настоящее задание проверяет, не ожил ли он.
    fallback_probe_seconds: int = 300
    # Столько сбоев шлюза подряд за окно переводят основной сервис в состояние «упал».
    fallback_failures_to_down: int = 2
    fallback_failure_window_seconds: int = 300
    # История версий заметки. Подробности в docs/note-revisions-v1.md.
    # Правки текста в паузе до этого срока попадают в одну версию. 0 отключает объединение.
    revision_coalesce_seconds: int = 600
    # Одна сессия правок не длится дольше этого времени.
    revision_session_max_seconds: int = 3600
    # Сколько версий хранить у одной заметки. Первая версия не удаляется. 0 отключает чистку.
    revision_keep: int = 50
    # WebSocket для входа и привязки Telegram. Подробности в docs/telegram-live-v1.md. Лимиты считаются на процесс.
    ws_poll_seconds: float = 1.0
    ws_max_seconds: float = 360.0
    ws_per_ip: int = 3
    ws_per_account: int = 3
    ws_handshakes_per_minute: int = 20
    error_log_file: str = "data/errors.log"
    audio_storage_path: str = "data/audio"
    audio_ffmpeg_path: str = "ffmpeg"
    audio_ffprobe_path: str = "ffprobe"
    analytics_pseudonym_key: str = field(default="", repr=False)
    internal_api_token: str = field(default="", repr=False)
    admin_allowed_networks: str = DEFAULT_ADMIN_NETWORKS
    admin_cookie_secure: bool = True
    # Шифрование содержимого на диске. Подробности в docs/encryption-v1.md. Файлы ключей читает app.crypto.
    data_encryption: str = "off"
    data_key_file: str = ""
    data_old_key_files: str = ""

    telegram_bot_username: str = "beresta_ru_bot"
    # Mail stays off until the owner configures an SMTP account. See docs/auth-telegram-v1.md.
    mail_enabled: bool = False
    email_registration_enabled: bool = False
    # smtp_bz sends over HTTPS (SMTP ports are closed at the VPS provider); smtp is plain STARTTLS.
    mail_transport: Literal["smtp_bz", "smtp"] = "smtp_bz"
    smtp_bz_api_key_file: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password_file: str = ""
    mail_from: str = ""

    def __post_init__(self):
        parse_networks(self.admin_allowed_networks)
        if self.data_encryption not in {"off", "required"}:
            raise ValueError("NOTES_DATA_ENCRYPTION must be off or required")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", self.telegram_bot_username):
            raise ValueError("NOTES_TELEGRAM_BOT_USERNAME must be a Telegram bot username without @")
        if self.mail_transport not in {"smtp_bz", "smtp"}:
            raise ValueError("NOTES_MAIL_TRANSPORT must be smtp_bz or smtp")
        if self.mail_enabled and not self.mail_from:
            raise ValueError("Mail needs NOTES_MAIL_FROM")
        if self.mail_enabled and self.mail_transport == "smtp" and not self.smtp_host:
            raise ValueError("SMTP mail needs NOTES_SMTP_HOST")
        if self.mail_enabled and self.mail_transport == "smtp_bz" and not self.smtp_bz_api_key_file:
            raise ValueError("SMTP.BZ mail needs NOTES_SMTP_BZ_API_KEY_FILE")
        if not 1 <= self.smtp_port <= 65535:
            raise ValueError("NOTES_SMTP_PORT must be a valid port")
        if self.analytics_pseudonym_key and len(self.analytics_pseudonym_key) < 32:
            raise ValueError("Analytics pseudonym key must contain at least 32 characters")
        if not 30 <= self.delivery_lease_seconds <= 300:
            raise ValueError("Delivery lease must be between 30 and 300 seconds")
        if self.internal_api_token and len(self.internal_api_token) < 32:
            raise ValueError("Internal API secret must contain at least 32 characters")
        if min(self.new_account_unit_limit, self.registrations_per_ip_per_day) < 0:
            raise ValueError("New account unit limit and registrations per day must not be negative")
        if min(self.daily_unit_limit, self.text_unit_cost, self.audio_unit_base, self.audio_unit_per_minute) < 0:
            raise ValueError("Daily unit limit and unit costs must not be negative")
        for name, delays in (
            ("NOTES_RETRY_CLEAR_DELAYS", self.retry_clear_delays),
            ("NOTES_RETRY_AMBIGUOUS_DELAYS", self.retry_ambiguous_delays),
            ("NOTES_RETRY_BAD_OUTPUT_DELAYS", self.retry_bad_output_delays),
        ):
            if len(delays) > 3 or any(not 1 <= value <= 3600 for value in delays):
                raise ValueError(f"{name} must hold up to 3 pauses of 1 to 3600 seconds")
        if not 0 <= self.retry_jitter <= 0.5:
            raise ValueError("NOTES_RETRY_JITTER_PERCENT must be between 0 and 50")
        if not 0 <= self.retry_after_max_seconds <= 3600:
            raise ValueError("NOTES_RETRY_AFTER_MAX_SECONDS must be between 0 and 3600")
        if not 0 <= self.revision_coalesce_seconds <= 86400 or not 60 <= self.revision_session_max_seconds <= 86400:
            raise ValueError("Revision coalesce and session seconds are out of range")
        if self.revision_keep != 0 and not 5 <= self.revision_keep <= 1000:
            raise ValueError("NOTES_REVISION_KEEP must be 0 or between 5 and 1000")
        if self.fallback_enabled:
            check_fallback_url(self.fallback_base_url)
            if len(self.fallback_token) < 32:
                raise ValueError("Fallback relay token must contain at least 32 characters")
            if not self.fallback_model or not self.fallback_stt_model:
                raise ValueError("Fallback needs a chat model and a speech model")
        if min(
            self.fallback_daily_usd, self.fallback_price_input_per_m, self.fallback_price_output_per_m,
            self.fallback_price_stt_per_minute,
        ) < 0:
            raise ValueError("Fallback cap and prices must not be negative")
        if (
            not 30 <= self.fallback_probe_seconds <= 3600
            or not 1 <= self.fallback_failures_to_down <= 10
            or not 30 <= self.fallback_failure_window_seconds <= 3600
        ):
            raise ValueError("Fallback probe, failure count or failure window is out of range")
        if min(self.retry_user_per_hour, self.retry_global_per_hour) < 0:
            raise ValueError("Retry caps must not be negative")
        if self.retry_breaker_failures < 1 or not (
            1 <= self.retry_breaker_window_seconds <= 3600 and 1 <= self.retry_breaker_pause_seconds <= 7200
        ):
            raise ValueError("Retry breaker needs at least 1 failure, a window up to 3600 s and a pause up to 7200 s")
        try:
            ZoneInfo(self.limit_timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("NOTES_LIMIT_TIMEZONE must be a known IANA time zone") from None
        if self.provider not in {"mock", "cloudru"}:
            raise ValueError("NOTES_PROVIDER must be mock or cloudru")
        low, high = LLM_READ_TIMEOUT_RANGE
        if not low <= self.llm_read_timeout_seconds <= high:
            raise ValueError(f"NOTES_LLM_READ_TIMEOUT_SECONDS must be between {low} and {high}")
        longest_call = (
            stream_deadline_seconds(self.llm_read_timeout_seconds)
            if self.llm_stream else self.llm_read_timeout_seconds
        )
        if self.lease_seconds < longest_call + 30:
            raise ValueError(
                f"Worker lease must exceed the provider timeout by 30 seconds (needs at least {longest_call + 30})"
            )
        if self.provider == "cloudru" and (
            not self.cloudru_model
            or self.live_call_limit <= 0
            or self.live_user_call_limit <= 0
        ):
            raise ValueError("Live mode requires model and positive call limits")
        if self.provider == "cloudru" and self.cloudru_base_url.rstrip("/") not in ALLOWED_MODEL_BASE_URLS:
            raise ValueError("Model base URL must be an approved HTTPS endpoint")

    @classmethod
    def from_env(cls):
        def flag(name, default):
            value = os.environ.get(name, str(default)).lower()
            if value not in {"true", "false"}:
                raise ValueError(f"{name} must be true or false")
            return value == "true"

        database_url = os.environ.get("NOTES_DATABASE_URL")
        password_file = os.environ.get("NOTES_DATABASE_PASSWORD_FILE")
        if database_url and password_file:
            raise ValueError("Use either NOTES_DATABASE_URL or NOTES_DATABASE_PASSWORD_FILE")
        if password_file:
            password = quote_plus(read_secret_file(password_file, maximum=4096))
            user = quote_plus(os.environ.get("NOTES_DATABASE_USER", "notes"))
            host = os.environ.get("NOTES_DATABASE_HOST", "db")
            port = int(os.environ.get("NOTES_DATABASE_PORT", "5432"))
            name = quote_plus(os.environ.get("NOTES_DATABASE_NAME", "notes"))
            database_url = f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"

        internal_token_file = os.environ.get("NOTES_INTERNAL_API_TOKEN_FILE")
        internal_token = os.environ.get("NOTES_INTERNAL_API_TOKEN", "")
        if internal_token_file and internal_token:
            raise ValueError("Use either NOTES_INTERNAL_API_TOKEN or NOTES_INTERNAL_API_TOKEN_FILE")

        analytics_file = os.environ.get("NOTES_ANALYTICS_PSEUDONYM_KEY_FILE")
        analytics_key = os.environ.get("NOTES_ANALYTICS_PSEUDONYM_KEY", "")
        if analytics_file and analytics_key:
            raise ValueError("Use either NOTES_ANALYTICS_PSEUDONYM_KEY or NOTES_ANALYTICS_PSEUDONYM_KEY_FILE")

        fallback_token_file = os.environ.get("NOTES_FALLBACK_TOKEN_FILE")
        fallback_token = os.environ.get("NOTES_FALLBACK_TOKEN", "")
        if fallback_token_file and fallback_token:
            raise ValueError("Use either NOTES_FALLBACK_TOKEN or NOTES_FALLBACK_TOKEN_FILE")
        fallback_enabled = flag("NOTES_FALLBACK_ENABLED", False)
        if fallback_enabled and fallback_token_file:
            fallback_token = read_secret_file(fallback_token_file, maximum=4096)

        llm_read_timeout = int(os.environ.get("NOTES_LLM_READ_TIMEOUT_SECONDS", str(LLM_READ_TIMEOUT_SECONDS)))
        llm_stream = flag("NOTES_LLM_STREAM", False)
        # The lease follows the longest possible model call, so a slow answer is never cut off by the worker.
        derived_lease = max(
            cls.lease_seconds,
            (stream_deadline_seconds(llm_read_timeout) if llm_stream else llm_read_timeout) + 30,
        )

        return cls(
            fallback_enabled=fallback_enabled,
            fallback_base_url=os.environ.get("NOTES_FALLBACK_BASE_URL", "").rstrip("/"),
            fallback_token=fallback_token,
            fallback_model=os.environ.get("NOTES_FALLBACK_MODEL", cls.fallback_model),
            fallback_stt_model=os.environ.get("NOTES_FALLBACK_STT_MODEL", cls.fallback_stt_model),
            fallback_daily_usd=parse_decimal("NOTES_FALLBACK_DAILY_USD", os.environ.get("NOTES_FALLBACK_DAILY_USD", "10")),
            fallback_price_input_per_m=parse_decimal(
                "NOTES_FALLBACK_PRICE_INPUT_PER_M", os.environ.get("NOTES_FALLBACK_PRICE_INPUT_PER_M", "2.50")),
            fallback_price_output_per_m=parse_decimal(
                "NOTES_FALLBACK_PRICE_OUTPUT_PER_M", os.environ.get("NOTES_FALLBACK_PRICE_OUTPUT_PER_M", "10.00")),
            fallback_price_stt_per_minute=parse_decimal(
                "NOTES_FALLBACK_PRICE_STT_PER_MINUTE", os.environ.get("NOTES_FALLBACK_PRICE_STT_PER_MINUTE", "0.006")),
            fallback_probe_seconds=int(os.environ.get("NOTES_FALLBACK_PROBE_SECONDS", "300")),
            fallback_failures_to_down=int(os.environ.get("NOTES_FALLBACK_FAILURES_TO_DOWN", "2")),
            fallback_failure_window_seconds=int(os.environ.get("NOTES_FALLBACK_FAILURE_WINDOW_SECONDS", "300")),
            llm_read_timeout_seconds=llm_read_timeout,
            llm_stream=llm_stream,
            lease_seconds=derived_lease,
            analytics_pseudonym_key=(read_secret_file(analytics_file, maximum=4096) if analytics_file else analytics_key),
            internal_api_token=(
                read_secret_file(internal_token_file, maximum=4096) if internal_token_file else internal_token
            ),
            database_url=database_url or cls.database_url,
            provider=os.environ.get("NOTES_PROVIDER", "mock"),
            auto_worker=flag("NOTES_AUTO_WORKER", True),
            secure_cookies=flag("NOTES_SECURE_COOKIES", False),
            admin_allowed_networks=os.environ.get("NOTES_ADMIN_ALLOWED_NETWORKS", DEFAULT_ADMIN_NETWORKS),
            admin_cookie_secure=flag("NOTES_ADMIN_COOKIE_SECURE", True),
            data_encryption=os.environ.get("NOTES_DATA_ENCRYPTION", cls.data_encryption),
            data_key_file=os.environ.get("NOTES_DATA_KEY_FILE", ""),
            data_old_key_files=os.environ.get("NOTES_DATA_OLD_KEY_FILES", ""),
            public_origin=os.environ.get("NOTES_PUBLIC_ORIGIN", cls.public_origin).rstrip("/"),
            allow_registration=flag("NOTES_ALLOW_REGISTRATION", True),
            allow_live_requests=flag("NOTES_ALLOW_LIVE_REQUESTS", False),
            cloudru_model=os.environ.get("NOTES_CLOUDRU_MODEL", ""),
            cloudru_base_url=os.environ.get("NOTES_CLOUDRU_BASE_URL", PROGRAM_BASE_URL),
            live_call_limit=int(os.environ.get("NOTES_LIVE_CALL_LIMIT", "0")),
            live_user_call_limit=int(os.environ.get("NOTES_LIVE_USER_CALL_LIMIT", "0")),
            daily_unit_limit=int(os.environ.get("NOTES_DAILY_UNIT_LIMIT", str(cls.daily_unit_limit))),
            new_account_unit_limit=int(
                os.environ.get("NOTES_NEW_ACCOUNT_UNIT_LIMIT", str(cls.new_account_unit_limit))
            ),
            registrations_per_ip_per_day=int(
                os.environ.get("NOTES_REGISTRATIONS_PER_IP_PER_DAY", str(cls.registrations_per_ip_per_day))
            ),
            api_docs_enabled=flag("NOTES_API_DOCS_ENABLED", False),
            text_unit_cost=int(os.environ.get("NOTES_TEXT_UNIT_COST", str(cls.text_unit_cost))),
            audio_unit_base=int(os.environ.get("NOTES_AUDIO_UNIT_BASE", str(cls.audio_unit_base))),
            audio_unit_per_minute=int(os.environ.get("NOTES_AUDIO_UNIT_PER_MINUTE", str(cls.audio_unit_per_minute))),
            limit_timezone=os.environ.get("NOTES_LIMIT_TIMEZONE", cls.limit_timezone),
            error_log_file=os.environ.get("NOTES_ERROR_LOG_FILE", "data/errors.log"),
            ai_retries_enabled=flag("NOTES_AI_RETRIES_ENABLED", True),
            retry_clear_delays=parse_seconds_list(
                "NOTES_RETRY_CLEAR_DELAYS", os.environ.get("NOTES_RETRY_CLEAR_DELAYS", "60,300")),
            retry_ambiguous_delays=parse_seconds_list(
                "NOTES_RETRY_AMBIGUOUS_DELAYS", os.environ.get("NOTES_RETRY_AMBIGUOUS_DELAYS", "120")),
            retry_bad_output_delays=parse_seconds_list(
                "NOTES_RETRY_BAD_OUTPUT_DELAYS", os.environ.get("NOTES_RETRY_BAD_OUTPUT_DELAYS", "60")),
            retry_jitter=int(os.environ.get("NOTES_RETRY_JITTER_PERCENT", "20")) / 100,
            retry_after_max_seconds=int(os.environ.get("NOTES_RETRY_AFTER_MAX_SECONDS", "900")),
            revision_coalesce_seconds=int(os.environ.get("NOTES_REVISION_COALESCE_SECONDS", "600")),
            revision_session_max_seconds=int(os.environ.get("NOTES_REVISION_SESSION_MAX_SECONDS", "3600")),
            revision_keep=int(os.environ.get("NOTES_REVISION_KEEP", "50")),
            retry_user_per_hour=int(os.environ.get("NOTES_RETRY_USER_PER_HOUR", "10")),
            retry_global_per_hour=int(os.environ.get("NOTES_RETRY_GLOBAL_PER_HOUR", "60")),
            retry_breaker_failures=int(os.environ.get("NOTES_RETRY_BREAKER_FAILURES", "5")),
            retry_breaker_window_seconds=int(os.environ.get("NOTES_RETRY_BREAKER_WINDOW_SECONDS", "300")),
            retry_breaker_pause_seconds=int(os.environ.get("NOTES_RETRY_BREAKER_PAUSE_SECONDS", "600")),
            delivery_lease_seconds=int(os.environ.get("NOTES_DELIVERY_LEASE_SECONDS", "60")),
            audio_storage_path=os.environ.get("NOTES_AUDIO_STORAGE_PATH", cls.audio_storage_path),
            audio_ffmpeg_path=os.environ.get("NOTES_AUDIO_FFMPEG_PATH", cls.audio_ffmpeg_path),
            audio_ffprobe_path=os.environ.get("NOTES_AUDIO_FFPROBE_PATH", cls.audio_ffprobe_path),
            telegram_bot_username=os.environ.get("NOTES_TELEGRAM_BOT_USERNAME", cls.telegram_bot_username),
            mail_enabled=flag("NOTES_MAIL_ENABLED", False),
            email_registration_enabled=flag("NOTES_EMAIL_REGISTRATION_ENABLED", False),
            mail_transport=os.environ.get("NOTES_MAIL_TRANSPORT", "smtp_bz"),
            smtp_bz_api_key_file=os.environ.get("NOTES_SMTP_BZ_API_KEY_FILE", ""),
            smtp_host=os.environ.get("NOTES_SMTP_HOST", ""),
            smtp_port=int(os.environ.get("NOTES_SMTP_PORT", "587")),
            smtp_user=os.environ.get("NOTES_SMTP_USER", ""),
            smtp_password_file=os.environ.get("NOTES_SMTP_PASSWORD_FILE", ""),
            mail_from=os.environ.get("NOTES_MAIL_FROM", ""),
        )
