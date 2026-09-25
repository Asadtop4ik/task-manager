"""Bot and trusted worker contracts for private project conversations."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.agent_intake import IntakeImage


class DiscussionStart(BaseModel):
    project_id: int
    chat_id: int


class DiscussionMessage(BaseModel):
    text: str = Field(default="", max_length=4000)
    images: list[IntakeImage] = Field(default_factory=list, max_length=3)


class DiscussionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    chat_id: int
    status: Literal["idle", "queued", "running", "failed"]
    messages: list[dict[str, Any]]
    error: str | None
    revision: int
    updated_at: datetime


class DiscussionWork(BaseModel):
    id: int
    revision: int
    lease_id: str
    repo_full_name: str
    base_branch: str
    project_key: str
    diagnostics_enabled: bool = False
    thread_id: str | None
    text: str
    images: list[IntakeImage]


class DiscussionResult(BaseModel):
    revision: int
    lease_id: str
    thread_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")
    response: str | None = Field(default=None, max_length=4000)
    error: str | None = Field(default=None, max_length=500)


class DiscussionNotice(BaseModel):
    id: int
    revision: int
    chat_id: int
    status: Literal["idle", "failed"]
    response: str | None
    error: str | None
