"""Versioned, trusted project catalog for Codex dispatch and deploy checks.

The API image carries this module. The private GitHub workflow imports it from
the trusted checkout; the server-side intake and monitor services use a
root-owned copy of this same file. Project DB settings still have to match an
entry here before dispatch. Tokens and enablement flags remain separate gates.
"""

from dataclasses import dataclass

DISPATCH_REPOSITORY = "Asadtop4ik/task-manager"


@dataclass(frozen=True)
class AgentRepository:
    project_key: str
    full_name: str
    branch: str
    private: bool
    fast_enabled: bool = False
    # Public-repo deploy verification. Keep these empty for private projects.
    ci_jobs: tuple[str, ...] = ()
    # Job names in the pull_request CI workflow, before a PR is announced.
    pr_ci_jobs: tuple[str, ...] = ()
    # Workflow file that owns those jobs.
    pr_ci_workflow: str = ".github/workflows/ci.yml"
    images: tuple[tuple[str, str], ...] = ()
    qa_only: bool = False


REPOSITORIES = (
    AgentRepository(
        "task-manager", DISPATCH_REPOSITORY, "main", True, True, pr_ci_jobs=("gate",)
    ),
    AgentRepository(
        "qurbot",
        "muradjanov-dev/qurbot",
        "master",
        False,
        ci_jobs=("ci / check",),
        pr_ci_jobs=("check",),
        images=(
            ("qurbot-web", "ghcr.io/muradjanov-dev/qurbot"),
            ("qurbot-worker", "ghcr.io/muradjanov-dev/qurbot"),
        ),
    ),
    AgentRepository(
        "kans-shop",
        "muradjanov-dev/kans-shop",
        "main",
        False,
        ci_jobs=("ci / backend", "ci / frontend"),
        pr_ci_jobs=("backend", "frontend"),
        images=(
            ("kans-api", "ghcr.io/muradjanov-dev/kans-shop-api"),
            ("kans-frontend", "ghcr.io/muradjanov-dev/kans-shop-frontend"),
        ),
    ),
    AgentRepository(
        "ketoshop",
        "muradjanov-dev/ketoshop",
        "master",
        False,
        ci_jobs=("ci / check",),
        pr_ci_jobs=("check",),
        images=(("ketoshop", "ghcr.io/muradjanov-dev/ketoshop"),),
    ),
)
QA_REPOSITORY = AgentRepository(
    "agent-qa",
    "Asadtop4ik/agent-qa",
    "main",
    True,
    pr_ci_jobs=("PR CI",),
    pr_ci_workflow=".github/workflows/agent-qa.yml",
    qa_only=True,
)
PUBLIC_REPOSITORIES = frozenset(
    repository.full_name for repository in REPOSITORIES if not repository.private
)


def repository_for(
    project_key: str,
    full_name: str | None,
    branch: str | None,
    repositories: tuple[AgentRepository, ...] = REPOSITORIES,
    *,
    include_qa: bool = False,
) -> AgentRepository | None:
    candidates = repositories + ((QA_REPOSITORY,) if include_qa else ())
    for repository in candidates:
        if (
            project_key == repository.project_key
            and full_name == repository.full_name
            and branch == repository.branch
        ):
            return repository
    return None


def public_catalog(
    repositories: tuple[AgentRepository, ...] = REPOSITORIES,
) -> tuple[AgentRepository, ...]:
    """Only explicitly approved public projects, all with verifiable deploys."""
    public = tuple(repository for repository in repositories if not repository.private)
    for repository in public:
        if not repository.ci_jobs or not repository.images:
            raise ValueError(
                f"public project lacks deploy verification: {repository.full_name}"
            )
    return public


def validate_catalog(
    repositories: tuple[AgentRepository, ...] = REPOSITORIES,
) -> None:
    keys = [item.project_key for item in repositories]
    names = [item.full_name for item in repositories]
    if len(keys) != len(set(keys)) or len(names) != len(set(names)):
        raise ValueError("duplicate agent project or repository")
    if any(not item.pr_ci_jobs for item in repositories):
        raise ValueError("agent project lacks PR CI verification")
    public_catalog(repositories)


validate_catalog()
