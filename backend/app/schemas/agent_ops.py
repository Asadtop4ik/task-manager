"""The `/agent-ops/*` lane: leasing, applying and deciding one proposal.

`AgentOpsProposal` (what a local-executor callback attaches to a run) and
`AgentOpsRequestOut` (the stored row, owner-only, with its value) live in
`app.schemas.agent_run` — `AgentRunCallback`, `AgentRunDetailOut` and
`AgentNotificationOut` all reference them, and this module needs
`AgentRunDetailOut` itself for `AgentOpsDecisionOut`, so importing the other
direction here avoids a cycle.
"""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.agent_run import AgentOpsRequestOut, AgentRunDetailOut

_HEX64_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)


class AgentOpsDecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    request_hash: str
    action_id: UUID

    @field_validator("request_hash")
    @classmethod
    def _check_hash(cls, value: str) -> str:
        # Never `Field(pattern=...)` here — see the module note in
        # `app.schemas.agent_run` on why a validator calls `.fullmatch()` itself.
        if not _HEX64_RE.fullmatch(value):
            raise ValueError("must be 64 lowercase hex characters")
        return value


class AgentOpsDecisionOut(BaseModel):
    ops_request: AgentOpsRequestOut
    run: AgentRunDetailOut


class AgentOpsLeaseRequest(BaseModel):
    """No body fields today; kept as a model so the endpoint has one place to
    grow request-side options without an ad hoc empty-body special case."""


class AgentOpsWorkOut(BaseModel):
    ops_id: int
    request_uuid: UUID
    run_id: str
    lease_id: str
    lease_until: datetime
    project_key: str
    repo_full_name: str
    kind: str
    key: str
    op: str
    value: str
    request_hash: str
    deployed_sha: str | None
    attempts: int


# The applying root helper's fixed result vocabulary (see the spec's
# `env_apply.py` exit/result codes) plus two agent-svc-lane-only codes for
# when the helper never even reported back (`timeout`, `no_result`).
_APPLIED_CODES = frozenset({"applied", "already_applied"})
_RETRY_CODES = frozenset({"busy", "timeout", "no_result"})
_FAILED_CODES = frozenset(
    {"bad_request", "refused", "precondition", "failed_rolled_back", "failed_rollback_failed"}
)
OpsResultCode = Literal[
    "applied",
    "already_applied",
    "bad_request",
    "refused",
    "precondition",
    "failed_rolled_back",
    "failed_rollback_failed",
    "busy",
    "timeout",
    "no_result",
]


class AgentOpsResultIn(BaseModel):
    status: Literal["applied", "failed", "retry"]
    code: OpsResultCode
    message: str | None = Field(default=None, max_length=300)
    rolled_back: bool | None = None
    restarted: bool | None = None
    exit: int | None = Field(default=None, ge=0, le=255)
    image_tag: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _check_status_code_consistency(self) -> "AgentOpsResultIn":
        expected = {"applied": _APPLIED_CODES, "retry": _RETRY_CODES, "failed": _FAILED_CODES}[
            self.status
        ]
        if self.code not in expected:
            raise ValueError(f"status {self.status!r} does not accept code {self.code!r}")
        return self
