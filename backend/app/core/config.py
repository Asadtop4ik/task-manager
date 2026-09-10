from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

# Values that are fine locally and must never reach production. A bot token or JWT
# secret that is still one of these means the env file was not filled in, and the
# container should die at boot rather than run with a guessable secret.
_PLACEHOLDERS = frozenset(
    {
        "",
        "change-me",
        "changeme",
        "placeholder",
        "secret",
        "dev",
        "test",
        "ci-secret",
        "ci-jwt-secret",
        "123456:CI-TOKEN",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Runtime ---
    environment: str = Field(default="development", alias="ENVIRONMENT")
    debug: bool = Field(default=False, alias="DEBUG")
    timezone: str = Field(default="Asia/Tashkent", alias="TIMEZONE")
    default_language: str = Field(default="uz", alias="DEFAULT_LANGUAGE")

    # --- Database ---
    # Async URL for the app, sync URL for Alembic. Two drivers on purpose: asyncpg
    # cannot run Alembic's synchronous migration context.
    database_url: str = Field(alias="DATABASE_URL")
    database_url_sync: str = Field(alias="DATABASE_URL_SYNC")

    # --- Redis ---
    redis_url: str = Field(alias="REDIS_URL")

    # --- Telegram ---
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    bot_username: str = Field(default="", alias="BOT_USERNAME")

    # --- Auth ---
    jwt_secret: str = Field(default="change-me", alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    jwt_access_ttl_minutes: int = Field(default=15, alias="JWT_ACCESS_TTL_MINUTES")
    jwt_refresh_ttl_days: int = Field(default=30, alias="JWT_REFRESH_TTL_DAYS")
    # Shared secret the bot presents to the API. The bot is a first-party client, not
    # a user, so it gets its own credential rather than borrowing someone's JWT.
    service_token: str = Field(default="change-me", alias="SERVICE_TOKEN")
    # Bootstrap admins: these telegram ids are approved on first login instead of
    # landing in the pending queue. Without at least one, nobody can approve anybody.
    admin_telegram_ids: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")

    # --- Web ---
    public_url: str = Field(default="http://localhost:5173", alias="PUBLIC_URL")

    @field_validator("environment")
    @classmethod
    def _known_environment(cls, value: str) -> str:
        allowed = {"development", "test", "production"}
        if value not in allowed:
            raise ValueError(f"ENVIRONMENT must be one of {sorted(allowed)}, got {value!r}")
        return value

    @model_validator(mode="after")
    def _no_placeholder_secrets_in_production(self) -> "Settings":
        if self.environment != "production":
            return self
        weak = [
            name
            for name, value in (
                ("BOT_TOKEN", self.bot_token),
                ("JWT_SECRET", self.jwt_secret),
                ("SERVICE_TOKEN", self.service_token),
            )
            if value.strip() in _PLACEHOLDERS
        ]
        if weak:
            raise ValueError(
                f"placeholder value(s) for {', '.join(weak)} in production — "
                "fill in /srv/stack/env/task-manager.env"
            )
        return self

    @property
    def admin_ids(self) -> set[int]:
        return {int(part) for part in self.admin_telegram_ids.replace(",", " ").split()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
