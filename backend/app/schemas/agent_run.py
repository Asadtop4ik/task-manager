import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Same format as `scripts/agent_ops_policy.py` (WP-D0) and agent-svc's own
# `ops_requests.py` — the backend never imports either, but the shapes must
# match exactly since all three independently re-validate the same JSON.
#
# Always `re.fullmatch(..., re.ASCII)` through a `@field_validator`, never a
# pydantic `Field(pattern=...)`: pydantic-core's `pattern` constraint uses
# search semantics under an anchor that, unlike Python's own `re.fullmatch`,
# can still accept a trailing newline.
OPS_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{1,63}", re.ASCII)
# Printable ASCII allow-list: blocks whitespace (incl. newline), NUL, U+2028,
# quotes, `$` (compose interpolation), `#`, `=`, `\`, and (with the separate
# `://` check below) URLs.
OPS_VALUE_RE = re.compile(r"[A-Za-z0-9_.,:@/+-]{1,256}", re.ASCII)
# A docker-compose service name: lowercase ASCII, matching the allowlist's
# own `services`/`containers` entries.
_RESTART_SERVICE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}", re.ASCII)
# C0/C1 controls (incl. NUL) plus the two Unicode line/paragraph separators —
# none of these are in `OPS_VALUE_RE`'s allow-list, but `reason`/`policy_reason`
# are free text, not fullmatch-validated, so they get a strip instead.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def _clean_text(value: str, max_length: int) -> str:
    return _CONTROL_CHARS_RE.sub("", value).strip()[:max_length]


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: str
    task_id: int
    repo_full_name: str
    base_branch: str
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
    qa_deploy_dispatch_status: str | None
    qa_deploy_dispatch_error: str | None
    qa_deploy_dispatched_at: datetime | None
    review_status: str | None
    review_sha: str | None
    review_summary: str | None
    review_findings: list[dict[str, object]] | None
    merged_at: datetime | None
    deployed_at: datetime | None
    executor: str


class AgentOpsProposal(BaseModel):
    """One entry of `AgentRunCallback.ops_requests` — already scored by
    agent-svc's own trusted `validate()` against the root-owned allowlist.
    The backend re-validates the key/value shape itself rather than trusting
    that scoring; only `policy`/`policy_reason`/`restart_services` are taken
    on faith from agent-svc (they never reach the applying root helper,
    which recomputes everything from its own allowlist copy)."""

    kind: Literal["env_set"]
    key: str
    op: Literal["replace", "list_add", "list_remove"]
    value: str
    reason: str = Field(max_length=300)
    policy: Literal["allowed", "denied"]
    policy_reason: str = Field(default="", max_length=200)
    restart_services: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        if not OPS_KEY_RE.fullmatch(value):
            raise ValueError("key must match [A-Z][A-Z0-9_]{1,63}")
        return value

    @field_validator("value")
    @classmethod
    def _check_value(cls, value: str) -> str:
        if not OPS_VALUE_RE.fullmatch(value) or "://" in value:
            raise ValueError(
                "value must match [A-Za-z0-9_.,:@/+-]{1,256} and contain no '://'"
            )
        return value

    @field_validator("reason", mode="before")
    @classmethod
    def _clean_reason(cls, value: str) -> str:
        return _clean_text(value, 300) if isinstance(value, str) else value

    @field_validator("policy_reason", mode="before")
    @classmethod
    def _clean_policy_reason(cls, value: str) -> str:
        return _clean_text(value, 200) if isinstance(value, str) else value

    @field_validator("restart_services")
    @classmethod
    def _check_restart_services(cls, value: list[str]) -> list[str]:
        for item in value:
            if not _RESTART_SERVICE_RE.fullmatch(item):
                raise ValueError("restart_services items must match [a-z0-9][a-z0-9_.-]{0,62}")
        return value


class AgentOpsRequestOut(BaseModel):
    """Owner-only view of one stored proposal — includes the value, but never
    the ops-apply lease (`lease_id`/`lease_until` are svc-only, see
    `AgentOpsWorkOut`). Never reused for a task-visible payload; `AgentRunOut`
    above has none of this table's columns."""

    id: int
    request_uuid: UUID
    run_id: str
    position: int
    project_key: str
    kind: str
    key: str
    op: str
    value: str
    reason: str | None
    restart_services: list[str] | None
    request_hash: str
    status: str
    policy_reason: str | None
    decided_by_user_id: int | None
    decided_at: datetime | None
    attempts: int
    applied_at: datetime | None
    result: dict[str, object] | None
    created_at: datetime
    updated_at: datetime


class AgentRunCallback(BaseModel):
    run_id: str
    status: str = Field(
        pattern=r"^(running|validating|publishing|deploying|pr_opened|ops_pending|failed)$"
    )
    github_run_url: str | None = None
    pr_url: str | None = None
    head_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    error: str | None = Field(default=None, max_length=1000)
    failure_phase: Literal["implement", "publish"] | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    ops_requests: list[AgentOpsProposal] = Field(default_factory=list, max_length=3)
    ops_note: str | None = Field(default=None, max_length=200)


class AgentDeployment(BaseModel):
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    github_run_url: str = Field(
        pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+$"
    )


class AgentQaDeployment(AgentDeployment):
    ready_url: str = Field(min_length=1, max_length=300)
    ready_status: Literal["ready"]
    ready_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class AgentQaDeploymentFailure(BaseModel):
    action_id: UUID
    expected_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    merge_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    github_run_url: str = Field(
        pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+$"
    )
    failure_code: Literal["image_pull_failed", "deploy_failed", "readiness_failed"]


class AgentQaDeploymentAuthorization(BaseModel):
    action_id: UUID
    expected_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    merge_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class AgentQaDeploymentAuthorizationOut(BaseModel):
    authorized: Literal[True]
    run_id: str
    repo_full_name: str
    expected_head_sha: str
    merge_sha: str


class AgentQaDeployDispatchResult(BaseModel):
    action_id: UUID
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    status: Literal["dispatched", "failed"]
    message: str | None = Field(default=None, max_length=1000)


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
    state: Literal["clean", "advisory", "findings", "error"] | None = None
    summary: str = Field(max_length=2000)
    findings: list[AgentReviewFinding] = Field(max_length=40)


class AgentReviewOut(BaseModel):
    state: Literal["pending", "clean", "advisory", "findings", "stale", "error"]
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
    task_id: int
    title: str | None
    repo_full_name: str
    status: str
    summary: str
    impact: str
    head_sha: str | None
    merged_sha: str | None
    deployed_sha: str | None
    github_run_url: str | None
    error: str | None
    qa_ready_url: str | None
    qa_ready_sha: str | None
    ci_evidence: AgentCiEvidenceOut
    review: AgentReviewOut
    actions: dict[str, AgentActionAvailability]
    ops_requests: list[AgentOpsRequestOut] = Field(default_factory=list)
    ops_note: str | None = None


class AgentActionOut(BaseModel):
    action_id: UUID
    status: Literal["accepted", "in_progress", "completed", "rejected", "retryable"]
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
    status: Literal["accepted", "in_progress", "completed", "rejected", "retryable"]
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
    qa_ready_url: str | None = None
    qa_ready_sha: str | None = None
    qa_deploy_dispatch_status: str | None = None
    qa_deploy_dispatch_error: str | None = None
    executor: str = "github"
    # Owner card only — includes values. The legacy task-origin card (which
    # may be a group chat) must use `ops_pending_count` instead.
    ops_requests: list[AgentOpsRequestOut] = Field(default_factory=list)
    ops_controls_available: bool = False
    ops_pending_count: int = 0
    ops_note: str | None = None


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


class AgentLeaseRequest(BaseModel):
    lane: Literal["code"] = "code"


class AgentWorkOut(BaseModel):
    run_id: str
    kind: Literal["implement", "review", "correction"]
    lease_id: str
    lease_until: datetime
    attempts: int
    attempt_index: int
    task_id: int
    task_revision: str
    repo_full_name: str
    base_branch: str
    mode: str
    title: str
    description: str
    image_count: int
    complexity: Literal["simple", "complex"] | None
    relevant_files: list[str]
    branch: str | None
    pr_url: str | None
    pr_number: int | None
    head_sha: str | None
    action_id: str | None
    instruction: str | None
    expected_head_sha: str | None


class AgentLeaseHeartbeat(BaseModel):
    lease_id: str


class AgentLeaseHeartbeatOut(BaseModel):
    lease_until: datetime


AgentStage = Literal[
    "leased",
    "workspace_ready",
    "codex_started",
    "codex_finished",
    "patch_validated",
    "preflight_passed",
    "branch_pushed",
    "review_started",
    "correction_pushed",
    "retrying",
]


class AgentStageReport(BaseModel):
    lease_id: str
    stage: AgentStage
    error: str | None = Field(default=None, max_length=1000)


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
