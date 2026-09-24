from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.task import TaskOut

IntakeMode = Literal["pr", "fast"]
IntakeStatus = Literal[
    "queued", "analyzing", "needs_answers", "ready", "failed", "confirmed", "cancelled"
]


class IntakeImage(BaseModel):
    file_id: str = Field(min_length=1, max_length=255)
    mime: Literal["image/jpeg", "image/png", "image/webp"]
    size: int | None = Field(default=None, ge=0, le=20 * 1024 * 1024)


class IntakeCreate(BaseModel):
    project_id: int
    text: str = Field(min_length=1, max_length=12000)
    mode: IntakeMode = "pr"
    chat_id: int
    images: list[IntakeImage] = Field(default_factory=list, max_length=3)

    @field_validator("text")
    @classmethod
    def _nonblank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task text cannot be blank")
        return value


class IntakeBrief(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    goal: str = Field(min_length=1, max_length=800)
    acceptance: list[Annotated[str, Field(min_length=1, max_length=250)]] = Field(
        min_length=1, max_length=5
    )
    assumptions: list[Annotated[str, Field(min_length=1, max_length=150)]] = Field(
        default_factory=list, max_length=5
    )


class IntakeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    chat_id: int
    text: str
    mode: IntakeMode
    status: IntakeStatus
    images: list[IntakeImage]
    questions: list[str]
    brief: dict
    error: str | None
    revision: int
    task_id: int | None
    expires_at: datetime


class IntakeWorkOut(BaseModel):
    id: int
    revision: int
    lease_id: str
    text: str
    answer_text: str | None
    mode: IntakeMode
    images: list[IntakeImage]
    repo_full_name: str
    base_branch: str
    analysis_rounds: int


class IntakeResult(BaseModel):
    revision: int
    lease_id: str
    status: Literal["ready", "needs_answers", "failed"]
    questions: list[str] = Field(default_factory=list, max_length=3)
    brief: IntakeBrief | None = None
    error: str | None = Field(default=None, max_length=900)

    @model_validator(mode="after")
    def _matching_content(self) -> "IntakeResult":
        if self.status == "ready" and self.brief is None:
            raise ValueError("a ready intake needs a brief")
        if self.status == "needs_answers" and not self.questions:
            raise ValueError("questions are required")
        if self.status == "failed" and not self.error:
            raise ValueError("a failed intake needs an error")
        return self


class IntakeText(BaseModel):
    text: str = Field(min_length=1, max_length=5000)

    @field_validator("text")
    @classmethod
    def _nonblank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("answer cannot be blank")
        return value


class IntakeConfirm(BaseModel):
    fallback_pr: bool = False


class IntakeConfirmedOut(BaseModel):
    task: TaskOut
    mode: IntakeMode
    created: bool


class IntakeNotificationOut(BaseModel):
    id: int
    revision: int
    status: Literal["ready", "needs_answers", "failed"]
    chat_id: int
    title: str
    mode: IntakeMode
    questions: list[str]
    brief: dict
    error: str | None


class IntakeNotified(BaseModel):
    revision: int
    message_id: int
