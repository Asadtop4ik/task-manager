"""The small, explicit set of repositories trusted for Codex dispatch."""

from dataclasses import dataclass

DISPATCH_REPOSITORY = "Asadtop4ik/task-manager"


@dataclass(frozen=True)
class AgentRepository:
    project_key: str
    full_name: str
    branch: str
    private: bool
    fast_enabled: bool = False


REPOSITORIES = (
    AgentRepository("task-manager", DISPATCH_REPOSITORY, "main", True, True),
    AgentRepository("qurbot", "muradjanov-dev/qurbot", "master", False),
    AgentRepository("kans-shop", "muradjanov-dev/kans-shop", "main", False),
    AgentRepository("ketoshop", "muradjanov-dev/ketoshop", "master", False),
)
PUBLIC_REPOSITORIES = frozenset(
    repository.full_name for repository in REPOSITORIES if not repository.private
)


def repository_for(
    project_key: str, full_name: str | None, branch: str | None
) -> AgentRepository | None:
    for repository in REPOSITORIES:
        if (
            project_key == repository.project_key
            and full_name == repository.full_name
            and branch == repository.branch
        ):
            return repository
    return None
