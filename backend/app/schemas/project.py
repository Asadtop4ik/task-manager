from pydantic import BaseModel, ConfigDict, Field

from app.schemas.user import UserOut


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    key: str
    name: str
    color: str
    is_archived: bool


class ProjectCreate(BaseModel):
    key: str = Field(min_length=2, max_length=32, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=120)
    color: str = Field(default="#64748b", max_length=16)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    color: str | None = Field(default=None, max_length=16)
    is_archived: bool | None = None


class MemberOut(BaseModel):
    user: UserOut
    role_in_project: str
