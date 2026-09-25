"""The API, intake, publisher and deploy monitor must share one allowlist."""

import sys
import unittest
from pathlib import Path

from agent_deploy_monitor import CI_TARGETS, TARGETS
from intake_worker import INTAKE_REPOSITORIES
from project_catalog import (
    approved_pairs,
    discussion_pairs,
    intake_pairs,
    public_projects,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from public_agent_task import APPROVED_REPOS  # noqa: E402

from agent_repos import (  # noqa: E402
    AgentRepository,
    REPOSITORIES,
    repository_for,
    validate_catalog,
)


class ProjectCatalogTests(unittest.TestCase):
    def test_all_existing_surfaces_use_the_same_approved_projects(self) -> None:
        self.assertEqual(INTAKE_REPOSITORIES, intake_pairs())
        self.assertEqual(APPROVED_REPOS, approved_pairs())
        self.assertEqual(set(TARGETS), set(APPROVED_REPOS))
        self.assertEqual(set(CI_TARGETS), {item.full_name for item in REPOSITORIES})
        for item in REPOSITORIES:
            self.assertEqual(CI_TARGETS[item.full_name], (item.branch, frozenset(item.pr_ci_jobs)))
        for item in public_projects():
            target = TARGETS[item.full_name]
            self.assertEqual(target.branch, item.branch)
            self.assertEqual(target.images, dict(item.images))
            self.assertEqual(target.ci_jobs, frozenset(item.ci_jobs))

    def test_new_catalog_entry_carries_all_onboarding_metadata(self) -> None:
        demo = AgentRepository(
            "demo", "example/demo", "main", False,
            ci_jobs=("ci / check",),
            pr_ci_jobs=("check",),
            images=(("demo-api", "ghcr.io/example/demo-api"),),
        )
        catalog = (*REPOSITORIES, demo)
        self.assertEqual(intake_pairs(catalog)[demo.full_name], "main")
        self.assertEqual(approved_pairs(catalog)[demo.full_name], "main")
        self.assertEqual(repository_for("demo", demo.full_name, "main", catalog), demo)
        self.assertIsNone(repository_for("demo", demo.full_name, "master", catalog))

    def test_private_qa_repo_is_available_to_discussions_only(self) -> None:
        self.assertNotIn("Asadtop4ik/agent-qa", intake_pairs())
        self.assertEqual(discussion_pairs()["Asadtop4ik/agent-qa"], "main")

    def test_public_project_without_deploy_evidence_fails_closed(self) -> None:
        incomplete = AgentRepository("bad", "example/bad", "main", False)
        with self.assertRaisesRegex(ValueError, "lacks deploy verification"):
            public_projects((*REPOSITORIES, incomplete))

    def test_duplicate_repository_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_catalog((*REPOSITORIES, REPOSITORIES[1]))
