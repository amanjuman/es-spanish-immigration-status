from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_token: str = ""
    telegram_chat_id: str = ""

    captcha_provider: str = "ocr"  # ocr | 2captcha | anticaptcha
    captcha_api_key: str = ""
    captcha_max_attempts: int = 5
    # Fall back to free OCR if the paid provider errors (e.g. empty balance).
    captcha_ocr_fallback: bool = True

    check_spacing_seconds: int = 60
    monitor_interval_seconds: int = 3600

    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = Path("./data")
    debug_screenshots: bool = False

    base_url: str = "https://infoext2.delegaciondelgobierno.gob.es/infoext2/"

    # ── Public-instance mode ──────────────────────────────────────
    # When true: the global monitor list requires ADMIN_TOKEN, monitors are
    # managed only through their secret /m/<token> links, rate limits and
    # caps apply, and the global TELEGRAM_CHAT_ID fallback receives no
    # monitor alerts (one-to-one privacy).
    public_mode: bool = False
    admin_token: str = ""
    # Initial value of invite-only mode; after first run it's toggled live
    # from the admin panel (persisted in the DB app_settings table).
    invite_only: bool = False
    # External URL of this instance (e.g. https://status.example.com) —
    # used to build absolute management links in bot messages.
    site_url: str = ""
    # Max active monitors; 0 = unlimited. The check queue fits ~60 visits/h,
    # so a public instance should cap around 40.
    max_monitors: int = 0
    # On-demand checks of the same expediente within this window return the
    # cached result instead of using a queue slot.
    ondemand_cache_ttl: int = 900
    # Per-IP limits (enforced only in public mode).
    rate_limit_checks_per_hour: int = 6
    rate_limit_monitors_per_day: int = 5
    # Honour X-Forwarded-For (set true only behind a trusted reverse proxy).
    trust_proxy: bool = False
    # Cloudflare Turnstile (optional bot protection for the public forms).
    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""
    # Source networks that skip the Turnstile check (trusted LAN / VPN).
    # Comma-separated CIDRs. Turnstile's own dashboard only accepts hostnames,
    # so IP-range exemptions are enforced here instead.
    turnstile_exempt_cidrs: str = "127.0.0.0/8,192.168.0.0/16,100.64.0.0/10"
    # Auto-purge (public mode): resolved monitors this long after their
    # fecha de resolución appeared; unclaimed = never got any subscriber.
    purge_resolved_after_days: int = 30
    purge_unclaimed_after_days: int = 7

    @property
    def db_path(self) -> Path:
        return self.data_dir / "notifier.sqlite3"

    @property
    def debug_dir(self) -> Path:
        return self.data_dir / "debug"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.debug_dir.mkdir(parents=True, exist_ok=True)
