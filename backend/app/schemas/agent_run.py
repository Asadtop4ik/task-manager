from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    task_id: int
    repo_full_name: str
    status: str
    github_run_url: str | None
    pr_url: str | None
    head_sha: str | None
    deployed_sha: str | None
    error: str | None
    attempts: int
    attempt_index: int
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    created_at: datetime
    finished_at: datetime | None


class AgentRunCallback(BaseModel):
    run_id: str
    status: str = Field(pattern=r"^(running|pr_ready|failed)$")
    github_run_url: str | None = None
    pr_url: str | None = None
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    error: str | None = Field(default=None, max_length=1000)
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class AgentDeployment(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    github_run_url: str = Field(
        pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+$"
    )


class AgentNotificationOut(BaseModel):
    run_id: str
    task_id: int
    title: str
    chat_id: int | None
    status: str
    pr_url: str | None
    github_run_url: str | None
    error: str | None
