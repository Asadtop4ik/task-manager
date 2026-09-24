from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

_PLACEHOLDERS = frozenset({"", "change-me", "changeme", "placeholder", "ci-secret"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    environment: str = Field(default="development", alias="ENVIRONMENT")
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    bot_username: str = Field(default="", alias="BOT_USERNAME")
    # Empty disables webhook registration, which is how local development and CI
    # run this image without ever touching Telegram.
    public_url: str = Field(default="", alias="PUBLIC_URL")
    webhook_secret: str = Field(default="change-me", alias="WEBHOOK_SECRET")
    # The API is reached over the internal `stack` network, never through Caddy.
    api_base_url: str = Field(default="http://task-api:8000", alias="API_BASE_URL")
    service_token: str = Field(default="change-me", alias="SERVICE_TOKEN")
    redis_url: str = Field(alias="REDIS_URL")
    timezone: str = Field(default="Asia/Tashkent", alias="TIMEZONE")
    agent_intake_enabled: bool = Field(default=False, alias="AGENT_INTAKE_ENABLED")

    @model_validator(mode="after")
    def _no_placeholder_secrets_in_production(self) -> "Settings":
        if self.environment != "production":
            return self
        weak = [
            name
            for name, value in (
                ("BOT_TOKEN", self.bot_token),
                ("WEBHOOK_SECRET", self.webhook_secret),
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
    def webhook_path(self) -> str:
        return "/webhook/telegram"


@lru_cache
def get_settings() -> Settings:
    # REDIS_URL is provided through the environment at runtime.
    return Settings()  # type: ignore[call-arg,unused-ignore]


settings = get_settings()
