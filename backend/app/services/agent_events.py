"""Append only small lifecycle facts; never persist prompts or Codex transcripts here."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentEvent, AgentIntake, AgentRun, ProjectDiscussion


def record(
    session: AsyncSession,
    subject: AgentRun | AgentIntake | ProjectDiscussion,
    *,
    status: str | None = None,
    phase: str | None = None,
    error: str | None = None,
    github_run_url: str | None = None,
) -> None:
    if isinstance(subject, AgentRun):
        identity = {"agent_run_id": subject.id}
    elif isinstance(subject, AgentIntake):
        identity = {"agent_intake_id": subject.id}
    elif isinstance(subject, ProjectDiscussion):
        identity = {"project_discussion_id": subject.id}
    else:
        raise TypeError("unsupported agent event subject")
    if next(iter(identity.values())) is None:
        raise ValueError("agent event subject must be flushed before recording")
    session.add(
        AgentEvent(
            **identity,
            status=status or subject.status,
            phase=phase,
            error=(error[:500] if error else None),
            github_run_url=github_run_url,
        )
    )
