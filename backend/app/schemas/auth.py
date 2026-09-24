from typing import Any

from pydantic import BaseModel, Field


class TelegramWidgetLogin(BaseModel):
    """Exactly the fields Telegram's Login Widget posts back.

    `model_config` is not `extra="ignore"` on purpose: the hash covers every field
    Telegram sent, so silently dropping an unknown one would break verification.
    """

    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None
    auth_date: int
    hash: str

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class MiniAppLogin(BaseModel):
    init_data: str = Field(min_length=1)


class MagicLinkRedeem(BaseModel):
    token: str = Field(min_length=20, max_length=128)


class MagicLinkOut(BaseModel):
    url: str
    expires_in: int


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthConfig(BaseModel):
    """Public, unauthenticated: a bot username is not a secret."""

    bot_username: str
    login_enabled: bool
