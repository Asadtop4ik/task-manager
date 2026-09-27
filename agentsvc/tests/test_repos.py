from __future__ import annotations

import os
import stat
import subprocess
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_svc.log import Redactor
from agent_svc.repos import Catalog, MirrorManager, load_catalog, make_run_dir

APPROVED = {"owner/repo": "main"}

_FAKE_AGENT_REPOS = """
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentRepository:
    project_key: str
    full_name: str
    branch: str
    private: bool
    ci_jobs: tuple = ()
    pr_ci_jobs: tuple = ()
    pr_ci_workflow: str = ".github/workflows/ci.yml"
    images: tuple = ()
    qa_only: bool = False


REPOSITORIES = (
    AgentRepository("task-manager", "Owner/task-manager", "main", True, pr_ci_jobs=("gate",)),
    AgentRepository(
        "demo", "owner/demo", "main", False,
        ci_jobs=("ci",), pr_ci_jobs=("check",), images=(("demo", "ghcr.io/owner/demo"),),
    ),
)
QA_REPOSITORY = AgentRepository(
    "agent-qa", "Owner/agent-qa", "main", True, pr_ci_jobs=("PR CI",), qa_only=True
)


def validate_catalog() -> None:
    return None
"""


class LoadCatalogTests(unittest.TestCase):
    def test_builds_catalog_from_trusted_module(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "agent_repos.py").write_text(_FAKE_AGENT_REPOS)
            catalog = load_catalog(tmp)
            self.assertIsInstance(catalog, Catalog)
            self.assertEqual(len(catalog.repos), 3)
            self.assertEqual(
                catalog.approved_pairs(),
                {
                    "Owner/task-manager": "main",
                    "owner/demo": "main",
                    "Owner/agent-qa": "main",
                },
            )
            self.assertEqual(catalog.public_repos, ("owner/demo",))
            self.assertEqual(catalog.qa_repo, "Owner/agent-qa")
            self.assertEqual(catalog.dispatch_repo, "Owner/task-manager")
            demo = catalog.get("owner/demo")
            assert demo is not None
            self.assertEqual(demo.images, (("demo", "ghcr.io/owner/demo"),))

    def test_missing_file_raises(self) -> None:
        with TemporaryDirectory() as tmp, self.assertRaises(FileNotFoundError):
            load_catalog(tmp)

    def test_loads_the_real_trusted_catalog(self) -> None:
        trusted_dir = Path(__file__).resolve().parents[2] / "backend" / "app" / "services"
        catalog = load_catalog(trusted_dir)
        self.assertIn("Asadtop4ik/task-manager", catalog.approved_pairs())
        self.assertEqual(catalog.dispatch_repo, "Asadtop4ik/task-manager")
        self.assertIsNotNone(catalog.qa_repo)


def _init_source_repo(path: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    (path / "a.txt").write_text("hello")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--quiet", "-m", "init"], check=True, env=env
    )
    subprocess.run(["git", "-C", str(path), "branch", "-M", "main"], check=True)
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class MirrorManagerRealGitTests(unittest.TestCase):
    def test_fetch_returns_exact_sha_and_group_readable_files(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            expected_sha = _init_source_repo(source)
            mirrors_dir = Path(tmp) / "mirrors"
            manager = MirrorManager(
                mirrors_dir,
                lambda repo: "dummy-token",
                approved_branches=APPROVED,
                remote_url_for=lambda repo: str(source),
            )
            sha = manager.fetch("owner/repo", "main")
            self.assertEqual(sha, expected_sha)
            mirror_path = manager.mirror_path("owner/repo")
            self.assertTrue((mirror_path / "HEAD").is_file())
            # `& 0o777` would silently ignore the setgid bit; S_IMODE keeps it.
            self.assertEqual(stat.S_IMODE(mirrors_dir.stat().st_mode), 0o2750)
            self.assertEqual(stat.S_IMODE(mirror_path.stat().st_mode), 0o2750)
            for item in mirror_path.rglob("*"):
                mode = stat.S_IMODE(item.stat().st_mode)
                if item.is_dir():
                    self.assertEqual(mode, 0o2750, msg=str(item))
                else:
                    self.assertEqual(mode, 0o640, msg=str(item))

    def test_ensure_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            _init_source_repo(source)
            mirrors_dir = Path(tmp) / "mirrors"
            manager = MirrorManager(
                mirrors_dir,
                lambda repo: "tok",
                approved_branches=APPROVED,
                remote_url_for=lambda repo: str(source),
            )
            first = manager.ensure("owner/repo")
            second = manager.ensure("owner/repo")
            self.assertEqual(first, second)

    def test_invalid_repo_name_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = MirrorManager(Path(tmp), lambda repo: "tok", approved_branches=APPROVED)
            with self.assertRaises(ValueError):
                manager.mirror_path("no-slash-here")

    def test_fetch_refuses_an_unapproved_branch_without_running_git(self) -> None:
        with TemporaryDirectory() as tmp:
            runner_calls: list[list[str]] = []

            def spy_runner(args: list[str], **kwargs: Any) -> _FakeResult:
                runner_calls.append(list(args))
                return _FakeResult()

            manager = MirrorManager(
                Path(tmp) / "mirrors",
                lambda repo: "tok",
                approved_branches=APPROVED,
                command_runner=spy_runner,
            )
            with self.assertRaises(ValueError):
                manager.fetch("owner/repo", "some-random-branch")
            self.assertEqual(runner_calls, [])

    def test_fetch_refuses_an_unapproved_repository(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = MirrorManager(
                Path(tmp) / "mirrors", lambda repo: "tok", approved_branches=APPROVED
            )
            with self.assertRaises(ValueError):
                manager.fetch("someone-else/unapproved", "main")

    def test_fetch_accepts_an_agent_branch_matching_the_pattern(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            _init_source_repo(source)
            agent_branch = "codex/task-42-11111111-1111-1111-1111-111111111111"
            subprocess.run(
                ["git", "-C", str(source), "checkout", "-b", agent_branch], check=True
            )
            mirrors_dir = Path(tmp) / "mirrors"
            manager = MirrorManager(
                mirrors_dir,
                lambda repo: "tok",
                approved_branches=APPROVED,
                remote_url_for=lambda repo: str(source),
            )
            sha = manager.fetch("owner/repo", agent_branch)
            self.assertTrue(sha)

    def test_fetch_timeout_override_reaches_both_git_calls(self) -> None:
        # The chat lane clamps a fetch to whatever remains of its own job
        # budget (`ChatRun.remaining_s()`); omitted, the default stays in
        # effect (proven by the other tests in this class never passing one).
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            _init_source_repo(source)
            recorded: list[float | None] = []

            def spy_runner(args: list[str], **kwargs: Any) -> Any:
                if len(args) >= 2 and args[0] == "git" and args[1] in ("fetch", "rev-parse"):
                    recorded.append(kwargs.get("timeout"))
                return subprocess.run(args, **kwargs)

            manager = MirrorManager(
                Path(tmp) / "mirrors",
                lambda repo: "tok",
                approved_branches=APPROVED,
                remote_url_for=lambda repo: str(source),
                command_runner=spy_runner,
            )
            manager.fetch("owner/repo", "main", timeout=7.5)
            self.assertEqual(recorded, [7.5, 7.5])

    def test_concurrent_fetches_of_the_same_repo_are_serialized(self) -> None:
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            source.mkdir()
            _init_source_repo(source)
            mirrors_dir = Path(tmp) / "mirrors"
            active = [0]
            max_active = [0]
            active_lock = threading.Lock()

            def spying_run(args: list[str], **kwargs: Any) -> Any:
                is_fetch = len(args) >= 2 and args[0] == "git" and args[1] == "fetch"
                if is_fetch:
                    with active_lock:
                        active[0] += 1
                        max_active[0] = max(max_active[0], active[0])
                try:
                    if is_fetch:
                        time.sleep(0.15)
                    return subprocess.run(args, **kwargs)
                finally:
                    if is_fetch:
                        with active_lock:
                            active[0] -= 1

            manager = MirrorManager(
                mirrors_dir,
                lambda repo: "tok",
                approved_branches=APPROVED,
                remote_url_for=lambda repo: str(source),
                command_runner=spying_run,
            )
            manager.ensure("owner/repo")
            threads = [
                threading.Thread(target=manager.fetch, args=("owner/repo", "main"))
                for _ in range(3)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(max_active[0], 1)

    def test_different_repos_fetch_concurrently_without_waiting(self) -> None:
        with TemporaryDirectory() as tmp:
            sources = {}
            for name in ("a", "b"):
                source = Path(tmp) / f"source-{name}"
                source.mkdir()
                _init_source_repo(source)
                sources[f"owner/{name}"] = source
            mirrors_dir = Path(tmp) / "mirrors"
            manager = MirrorManager(
                mirrors_dir,
                lambda repo: "tok",
                approved_branches={"owner/a": "main", "owner/b": "main"},
                remote_url_for=lambda repo: str(sources[repo]),
            )
            results: dict[str, str] = {}

            def run(repo: str) -> None:
                results[repo] = manager.fetch(repo, "main")

            threads = [threading.Thread(target=run, args=(repo,)) for repo in sources]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(set(results), set(sources))


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeGitRunner:
    def __init__(self, responses: dict[str, _FakeResult] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = responses or {}

    def __call__(self, args: list[str], **kwargs: Any) -> _FakeResult:
        self.calls.append(
            {
                "args": list(args),
                "env": kwargs.get("env"),
                "cwd": kwargs.get("cwd"),
                "capture_output": kwargs.get("capture_output"),
            }
        )
        subcommand = args[1] if len(args) > 1 else ""
        result = self._responses.get(subcommand, _FakeResult())
        # Mimic `git init --bare <path>` actually creating the directory, since
        # `_set_group_readable` walks the real filesystem afterwards.
        if subcommand == "init" and result.returncode == 0:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
            (Path(args[-1]) / "HEAD").write_text("ref: refs/heads/main\n")
        return result


class MirrorManagerEnvIsolationTests(unittest.TestCase):
    def test_token_only_in_fetch_process_env_never_in_argv(self) -> None:
        with TemporaryDirectory() as tmp:
            runner = _FakeGitRunner({"rev-parse": _FakeResult(stdout="a" * 40 + "\n")})
            manager = MirrorManager(
                Path(tmp) / "mirrors",
                lambda repo: "the-secret-token",
                approved_branches=APPROVED,
                remote_url_for=lambda repo: "https://github.com/owner/repo.git",
                command_runner=runner,
            )
            sha = manager.fetch("owner/repo", "main")
            self.assertEqual(sha, "a" * 40)

            calls_by_subcommand = {call["args"][1]: call for call in runner.calls}
            self.assertIn("init", calls_by_subcommand)
            self.assertIn("fetch", calls_by_subcommand)
            self.assertIn("rev-parse", calls_by_subcommand)

            for call in runner.calls:
                for arg in call["args"]:
                    self.assertNotIn("the-secret-token", arg)

            fetch_env = calls_by_subcommand["fetch"]["env"]
            self.assertIn("GIT_CONFIG_KEY_0", fetch_env)
            self.assertIn("GIT_CONFIG_VALUE_0", fetch_env)
            self.assertNotIn("the-secret-token", fetch_env["GIT_CONFIG_VALUE_0"])

            for name in ("init", "rev-parse"):
                self.assertNotIn("GIT_CONFIG_KEY_0", calls_by_subcommand[name]["env"])

    def test_git_failure_raises_runtime_error_with_stderr(self) -> None:
        with TemporaryDirectory() as tmp:
            runner = _FakeGitRunner({"init": _FakeResult(returncode=1, stderr="boom")})
            manager = MirrorManager(
                Path(tmp) / "mirrors",
                lambda repo: "tok",
                approved_branches=APPROVED,
                command_runner=runner,
            )
            with self.assertRaises(RuntimeError) as ctx:
                manager.ensure("owner/repo")
            self.assertIn("boom", str(ctx.exception))

    def test_git_failure_stderr_is_redacted_and_captured(self) -> None:
        with TemporaryDirectory() as tmp:
            runner = _FakeGitRunner(
                {"init": _FakeResult(returncode=1, stderr="token super-secret-value leaked")}
            )
            manager = MirrorManager(
                Path(tmp) / "mirrors",
                lambda repo: "tok",
                approved_branches=APPROVED,
                command_runner=runner,
                redactor=Redactor(["super-secret-value"]),
            )
            with self.assertRaises(RuntimeError) as ctx:
                manager.ensure("owner/repo")
            self.assertNotIn("super-secret-value", str(ctx.exception))
            self.assertIn("[REDACTED]", str(ctx.exception))

    def test_git_stderr_is_captured_not_inherited(self) -> None:
        # Regression guard: `_git` must always pass `capture_output=True`, or
        # git's stderr would go straight to our own stderr (journald)
        # unredacted, and a failure's error message would be empty.
        with TemporaryDirectory() as tmp:
            runner = _FakeGitRunner({"init": _FakeResult(returncode=1, stderr="boom")})
            manager = MirrorManager(
                Path(tmp) / "mirrors",
                lambda repo: "tok",
                approved_branches=APPROVED,
                command_runner=runner,
            )
            with self.assertRaises(RuntimeError):
                manager.ensure("owner/repo")
            self.assertTrue(runner.calls)
            for call in runner.calls:
                self.assertIs(call["capture_output"], True)


RUN_ID = "11111111-1111-1111-1111-111111111111"


class MakeRunDirTests(unittest.TestCase):
    def test_creates_run_dir_and_images_with_explicit_modes(self) -> None:
        with TemporaryDirectory() as tmp:
            run_dir = make_run_dir(tmp, RUN_ID)
            self.assertEqual(run_dir, Path(tmp) / RUN_ID)
            self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o3770)
            images_dir = run_dir / "images"
            self.assertTrue(images_dir.is_dir())
            self.assertEqual(stat.S_IMODE(images_dir.stat().st_mode), 0o2750)

    def test_is_idempotent_and_re_applies_modes(self) -> None:
        with TemporaryDirectory() as tmp:
            first = make_run_dir(tmp, RUN_ID)
            first.chmod(0o700)  # simulate some other mode; must be reset
            second = make_run_dir(tmp, RUN_ID)
            self.assertEqual(first, second)
            self.assertEqual(stat.S_IMODE(second.stat().st_mode), 0o3770)

    def test_creates_missing_work_root(self) -> None:
        with TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "not-yet-created"
            run_dir = make_run_dir(work_root, RUN_ID)
            self.assertTrue(run_dir.is_dir())

    def test_rejects_invalid_run_id(self) -> None:
        with TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            make_run_dir(tmp, "not-a-run-id")

    def test_refuses_a_symlinked_run_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            work_root.mkdir()
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            (work_root / RUN_ID).symlink_to(elsewhere, target_is_directory=True)
            with self.assertRaises(ValueError):
                make_run_dir(work_root, RUN_ID)

    def test_refuses_a_symlinked_images_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            work_root = Path(tmp) / "work"
            run_dir = work_root / RUN_ID
            run_dir.mkdir(parents=True)
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            (run_dir / "images").symlink_to(elsewhere, target_is_directory=True)
            with self.assertRaises(ValueError):
                make_run_dir(work_root, RUN_ID)


if __name__ == "__main__":
    unittest.main()
