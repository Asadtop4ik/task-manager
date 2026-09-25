from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    task_id: int
    repo_full_name: str
    status: str
    ci_status: str | None
    ci_verified_sha: str | None
    ci_url: str | None
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
    pr_opened_at: datetime | None
    pr_ready_at: datetime | None
    owner_notice_chat_id: int | None
    owner_notice_message_id: int | None
    qa_ready_url: str | None
    qa_ready_sha: str | None
    qa_ready_at: datetime | None
    review_status: str | None
    review_sha: str | None
    review_summary: str | None
    review_findings: list[dict[str, object]] | None
    merged_at: datetime | None
    deployed_at: datetime | None


class AgentRunCallback(BaseModel):
    run_id: str
    status: str = Field(
        pattern=r"^(running|validating|publishing|deploying|pr_opened|failed)$"
    )
    github_run_url: str | None = None
    pr_url: str | None = None
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    error: str | None = Field(default=None, max_length=1000)
    failure_phase: Literal["implement", "publish"] | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class AgentDeployment(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    github_run_url: str = Field(
        pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+$"
    )


class AgentQaDeployment(AgentDeployment):
    ready_url: str = Field(min_length=1, max_length=300)
    ready_status: Literal["ready"]
    ready_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class AgentMerge(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class AgentReleaseRequest(BaseModel):
    expected_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    action_id: UUID


class AgentCorrectionRequest(AgentReleaseRequest):
    instruction: str = Field(min_length=1, max_length=4000)


class AgentReviewFinding(BaseModel):
    severity: Literal["P1", "P2", "P3"]
    title: str = Field(min_length=1, max_length=240)
    evidence: str = Field(min_length=1, max_length=2000)
    file: str | None = Field(default=None, max_length=500)
    line: int | None = Field(default=None, ge=1)


class AgentReviewResult(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    state: Literal["clean", "findings", "error"] | None = None
    summary: str = Field(max_length=2000)
    findings: list[AgentReviewFinding] = Field(max_length=40)


class AgentReviewOut(BaseModel):
    state: Literal["pending", "clean", "findings", "stale", "error"]
    reviewed_head_sha: str | None
    summary: str | None
    findings: list[AgentReviewFinding]


class AgentCiEvidenceOut(BaseModel):
    state: str | None
    verified_head_sha: str | None
    url: str | None


class AgentActionAvailability(BaseModel):
    available: bool


class AgentRunDetailOut(BaseModel):
    run_id: str
    status: str
    summary: str
    impact: str
    head_sha: str | None
    ci_evidence: AgentCiEvidenceOut
    review: AgentReviewOut
    actions: dict[str, AgentActionAvailability]


class AgentActionOut(BaseModel):
    action_id: UUID
    status: Literal["accepted", "in_progress", "completed", "rejected"]
    run_id: str
    head_sha: str | None
    message: str | None = None


class AgentActionResult(BaseModel):
    action_id: UUID
    status: Literal["completed", "rejected"]
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    merge_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    message: str | None = Field(default=None, max_length=1000)


class AgentActionDetailOut(BaseModel):
    action_id: UUID
    kind: Literal["merge", "correction"]
    status: Literal["accepted", "in_progress", "completed", "rejected"]
    request: dict[str, object]
    result: dict[str, object] | None


class AgentCiResult(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    conclusion: Literal["pending", "success", "failure"]
    github_run_url: str | None = None


class AgentCiPending(BaseModel):
    id: int
    run_id: str
    repo_full_name: str
    base_branch: str
    pr_url: str
    head_sha: str
    status: Literal["pr_opened", "pr_ready"]
    ci_status: str | None
    ci_verified_sha: str | None
    ci_url: str | None


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
    ci_status: str | None
    ci_url: str | None
    mode: str
    pr_url: str | None
    github_run_url: str | None
    head_sha: str | None
    merged_sha: str | None
    deployed_sha: str | None
    telegram_message_id: int | None
    error: str | None
    owner_chat_id: int | None = None
    owner_notice_chat_id: int | None = None
    owner_notice_message_id: int | None = None
    owner_controls_available: bool = False
    summary: str = ""
    impact: str = ""
    review: AgentReviewOut | None = None
    ci_evidence: AgentCiEvidenceOut | None = None
    actions: dict[str, AgentActionAvailability] | None = None


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


class AgentEventOut(BaseModel):
    id: int
    flow: Literal["coding", "intake", "discussion"]
    source_id: int
    task_id: int | None
    project_id: int | None
    status: str
    phase: str | None
    error: str | None
    github_run_url: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    created_at: datetime
