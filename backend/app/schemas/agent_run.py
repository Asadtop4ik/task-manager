from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    task_id: int
    repo_full_name: str
    status: str
    mode: str
    github_run_url: str | None
    pr_url: str | None
    head_sha: str | None
    merged_sha: str | None
    deployed_sha: str | None
    error: str | None
    attempts: int
    attempt_index: int
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    created_at: datetime
    finished_at: datetime | None
    runner_started_at: datetime | None
    pr_ready_at: datetime | None
    merged_at: datetime | None
    deployed_at: datetime | None


class AgentRunCallback(BaseModel):
    run_id: str
    status: str = Field(pattern=r"^(running|validating|publishing|deploying|pr_ready|failed)$")
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


class AgentMerge(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class ExternalAgentPending(BaseModel):
    id: int
    run_id: str
    repo_full_name: str
    base_branch: str
    pr_url: str
    status: Literal["pr_ready", "merged"]
    merged_sha: str | None
    notified: bool


class AgentRunStart(BaseModel):
    mode: Literal["pr", "fast"] = "pr"


class AgentImageOut(BaseModel):
    id: int
    mime: Literal["image/jpeg", "image/png", "image/webp"]
    size: int | None


class AgentNotificationOut(BaseModel):
    run_id: str
    task_id: int
    title: str
    repo_full_name: str
    chat_id: int | None
    status: str
    mode: str
    pr_url: str | None
    github_run_url: str | None
    head_sha: str | None
    merged_sha: str | None
    deployed_sha: str | None
    telegram_message_id: int | None
    error: str | None


class AgentNoticeAck(BaseModel):
    message_id: int | None = Field(default=None, ge=1)


class MetricDuration(BaseModel):
    samples: int
    p50_seconds: int | None
    p90_seconds: int | None


class AgentMetricsOut(BaseModel):
    since: datetime
    target_tasks: int
    sampled_runs: int
    enough_data: bool
    deployed: int
    failed_attempts: int
    cancelled_attempts: int
    retried: int
    queue: MetricDuration
    implementation: MetricDuration
    human_review: MetricDuration
    end_to_end: MetricDuration
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
