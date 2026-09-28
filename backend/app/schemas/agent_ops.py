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

from pydantic import BaseModel, Field, field_validator

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


class AgentOpsResultIn(BaseModel):
    status: Literal["applied", "failed", "retry"]
    code: str = Field(min_length=1, max_length=40)
    message: str | None = Field(default=None, max_length=300)
    rolled_back: bool | None = None
    image_tag: str | None = Field(default=None, max_length=100)
