from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.enums import UserRole


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_id: int
    username: str | None
    full_name: str
    role: UserRole
    lang: str
    tz: str
    is_active: bool
    can_use_codex: bool
    is_owner: bool = False
    created_at: datetime


class UserUpdate(BaseModel):
    role: UserRole | None = None
    lang: str | None = None
    tz: str | None = None
    is_active: bool | None = None
