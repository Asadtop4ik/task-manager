"""Load the trusted catalog in either a checkout or the server's ops directory."""

from __future__ import annotations

import sys
from pathlib import Path

_source = Path(__file__).resolve().parents[1] / "backend" / "app" / "services"
if (_source / "agent_repos.py").is_file():
    sys.path.insert(0, str(_source))

# On netcup the same root-owned agent_repos.py is installed next to this file.
from agent_repos import (  # noqa: E402
    AgentRepository,
    QA_REPOSITORY,
    REPOSITORIES,
    public_catalog,
)


def public_projects(
    repositories: tuple[AgentRepository, ...] | None = None,
) -> tuple[AgentRepository, ...]:
    return public_catalog() if repositories is None else public_catalog(repositories)


def approved_pairs(
    repositories: tuple[AgentRepository, ...] | None = None,
) -> dict[str, str]:
    return {item.full_name: item.branch for item in public_projects(repositories)}


def intake_pairs(
    repositories: tuple[AgentRepository, ...] = REPOSITORIES,
) -> dict[str, str]:
    return {item.full_name: item.branch for item in repositories}


def discussion_pairs() -> dict[str, str]:
    """Include the owner-gated private QA repo for read-only discussions."""
    return intake_pairs((*REPOSITORIES, QA_REPOSITORY))
