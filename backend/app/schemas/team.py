from datetime import datetime

from pydantic import BaseModel, Field


class InviteOut(BaseModel):
    url: str
    expires_at: datetime


class JoinRequestCreate(BaseModel):
    invite_token: str = Field(min_length=20, max_length=64)
    telegram_id: int = Field(gt=0)
    first_name: str = Field(min_length=1, max_length=255)
    last_name: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, max_length=64)


class JoinRequestOut(BaseModel):
    id: int | None
    telegram_id: int
    full_name: str
    username: str | None
    status: str
    notify_owner: bool = False
    owner_chat_id: int | None = None


class JoinDecision(BaseModel):
    project_ids: list[int] = Field(min_length=1)


class CodexAccessUpdate(BaseModel):
    enabled: bool
