from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.db.enums import ActivityKind
from app.schemas.user import UserOut


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: ActivityKind
    payload: dict[str, Any]
    actor: UserOut | None
    created_at: datetime
