"""Tests for `libexec/codex_child.py`.

Runs the child both in-process (for pure validation/command-building logic,
patching module constants directly) and as a real subprocess against
`fake_codex.py` in test mode (`AGENT_CHILD_TEST_MODE=1` plus
`AGENT_CHILD_TEST_*` overrides), which exercises the full protocol: stdin
JSON in, JSON/JSONL out, real process spawn/kill.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
LIBEXEC_DIR = TESTS_DIR.parent / "libexec"
if str(LIBEXEC_DIR) not in sys.path:
    sys.path.insert(0, str(LIBEXEC_DIR))

import codex_child  # noqa: E402
import image_state  # noqa: E402

FAKE_CODEX = TESTS_DIR / "fake_codex.py"
PYTHON_BIN = sys.executable
CHILD_SCRIPT = LIBEXEC_DIR / "codex_child.py"
GIT_AUTHOR_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}


def new_run_id() -> str:
    return str(uuid.uuid4())


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **GIT_AUTHOR_ENV}
    return subprocess.run(
        args, cwd=str(cwd), env=env, check=True, capture_output=True, text=True
    )


def build_mirror(mirrors_dir: Path, repo: str, files: dict[str, str]) -> tuple[str, str]:
    """Seed a small repo with `files`, commit, and clone it bare into
    `mirrors_dir` under the name codex_child expects for `repo`. Returns
    (mirror_path, base_sha)."""
    seed = mirrors_dir / f"_seed_{repo.replace('/', '__')}"
    seed.mkdir(parents=True)
    for relpath, content in files.items():
        path = seed / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _run_git(["git", "init", "--quiet"], cwd=seed)
    _run_git(["git", "add", "-A"], cwd=seed)
    _run_git(["git", "commit", "--quiet", "-m", "seed"], cwd=seed)
    sha = _run_git(["git", "rev-parse", "HEAD"], cwd=seed).stdout.strip()
    mirror_path = mirrors_dir / (repo.replace("/", "__") + ".git")
    _run_git(
        ["git", "clone", "--quiet", "--bare", str(seed), str(mirror_path)], cwd=mirrors_dir
    )
    shutil.rmtree(seed)
    return str(mirror_path), sha


class ChildProcessTestCase(unittest.TestCase):
    """Base class wiring a scratch work_root/mirrors_dir and test-mode env."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agent-svc-child-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.work_root = self.root / "work"
        self.mirrors_dir = self.root / "mirrors"
        self.codex_home_code = self.root / "codex-home-code"
        self.codex_home_chat = self.root / "codex-home-chat"
        for path in (
            self.work_root,
            self.mirrors_dir,
            self.codex_home_code,
            self.codex_home_chat,
        ):
            path.mkdir(parents=True)

    def child_env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": "/usr/bin:/bin",
            "AGENT_CHILD_TEST_MODE": "1",
            "AGENT_CHILD_TEST_WORK_ROOT": str(self.work_root),
            "AGENT_CHILD_TEST_MIRRORS_DIR": str(self.mirrors_dir),
            "AGENT_CHILD_TEST_CODEX_BINARY": str(FAKE_CODEX),
            "AGENT_CHILD_TEST_CODEX_HOME_CODE": str(self.codex_home_code),
            "AGENT_CHILD_TEST_CODEX_HOME_CHAT": str(self.codex_home_chat),
            "AGENT_CHILD_TEST_HOME": str(self.root),
            "AGENT_CHILD_TEST_TREE_KILL_GRACE_S": "1",
        }
        env.update(extra)
        return env

    def make_run_dir(self, run_id: str) -> Path:
        run_dir = self.work_root / run_id
        run_dir.mkdir(parents=True)
        return run_dir

    def run_child(
        self,
        subcommand: str,
        request: dict,
        *,
        env_extra: dict[str, str] | None = None,
        timeout: float = 30,
        popen: bool = False,
    ):
        argv = [PYTHON_BIN, str(CHILD_SCRIPT), subcommand]
        env = self.child_env(**(env_extra or {}))
        if popen:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            assert process.stdin is not None
            process.stdin.write(json.dumps(request))
            process.stdin.close()
            return process
        return subprocess.run(
            argv,
            input=json.dumps(request).encode(),
            capture_output=True,
            env=env,
            timeout=timeout,
        )

    def base_exec_request(self, run_id: str, **overrides) -> dict:
        request = {
            "run_id": run_id,
            "lane": "code",
            "cwd": "empty",
            "model": "gpt-6-luna",
            "effort": "high",
            "sandbox": "workspace-write",
            "multi_agent": False,
            "prompt": "hello",
            "images": [],
            "output_schema": None,
            "timeout_s": 20,
            "idle_timeout_s": 10,
        }
        request.update(overrides)
        return request


# ---------------------------------------------------------------------------
# Pure validation / command-building tests (in-process)
# ---------------------------------------------------------------------------


class RunIdValidationTests(unittest.TestCase):
    def test_valid_run_id(self) -> None:
        run_id = str(uuid.uuid4())
        self.assertEqual(codex_child._validate_run_id(run_id), run_id)

    def test_rejects_non_string(self) -> None:
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_run_id(123)

    def test_rejects_bad_shape(self) -> None:
        for bad in (
            "../etc/passwd",
            "not-a-uuid",
            "a" * 37,
            "UPPERCASE-1234567890123456789012",
        ):
            with self.assertRaises(codex_child.ChildRefusal):
                codex_child._validate_run_id(bad)


class RunDirAndPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_root = Path(self._tmp.name)
        self.patcher = patch.object(codex_child, "WORK_ROOT", self.work_root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_run_dir_inside_work_root(self) -> None:
        run_id = str(uuid.uuid4())
        run_dir = codex_child._run_dir(run_id)
        self.assertEqual(run_dir, (self.work_root / run_id).resolve())

    def test_run_dir_rejects_traversal(self) -> None:
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._run_dir("../../etc")

    def test_ensure_within_accepts_nested_path(self) -> None:
        root = self.work_root / "run-1"
        root.mkdir()
        nested = root / "images" / "a.png"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b"x")
        self.assertEqual(codex_child._ensure_within(nested, root), nested.resolve())

    def test_ensure_within_rejects_escape(self) -> None:
        root = self.work_root / "run-1"
        root.mkdir()
        outside = self.work_root / "run-2" / "secret"
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._ensure_within(outside, root)

    def test_ensure_within_rejects_dotdot_escape(self) -> None:
        root = self.work_root / "run-1"
        root.mkdir()
        escape = root / ".." / "run-2" / "secret"
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._ensure_within(escape, root)


class MirrorValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mirrors_dir = Path(self._tmp.name) / "mirrors"
        self.mirrors_dir.mkdir()
        self.patcher = patch.object(codex_child, "MIRRORS_DIR", self.mirrors_dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_accepts_matching_mirror(self) -> None:
        mirror = self.mirrors_dir / "acme__widgets.git"
        mirror.mkdir()
        result = codex_child._validate_mirror(str(mirror), "acme/widgets")
        self.assertEqual(result, mirror.resolve())

    def test_rejects_mirror_outside_configured_dir(self) -> None:
        outside = Path(self._tmp.name) / "elsewhere" / "acme__widgets.git"
        outside.mkdir(parents=True)
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_mirror(str(outside), "acme/widgets")

    def test_rejects_mismatched_repo_name(self) -> None:
        mirror = self.mirrors_dir / "acme__widgets.git"
        mirror.mkdir()
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_mirror(str(mirror), "acme/other")

    def test_rejects_bad_repo_shape(self) -> None:
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_mirror(str(self.mirrors_dir / "x.git"), "not-a-repo-slug")

    def test_rejects_missing_mirror(self) -> None:
        missing = self.mirrors_dir / "acme__widgets.git"
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_mirror(str(missing), "acme/widgets")


class ExecRequestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Resolved up front: WORK_ROOT is compared against a `.resolve()`d
        # path inside `_run_dir`, and on macOS `$TMPDIR` is a symlink
        # (/var/folders -> /private/var/folders), so an unresolved path here
        # would never match.
        self.work_root = Path(self._tmp.name).resolve()
        patcher = patch.object(codex_child, "WORK_ROOT", self.work_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run_id = str(uuid.uuid4())
        self.run_dir = self.work_root / self.run_id
        self.run_dir.mkdir()

    def request(self, **overrides) -> dict:
        base = {
            "run_id": self.run_id,
            "lane": "code",
            "cwd": "empty",
            "model": "gpt-6-luna",
            "effort": "high",
            "sandbox": "workspace-write",
            "multi_agent": False,
            "prompt": "hi",
            "images": [],
            "output_schema": None,
            "timeout_s": 10,
            "idle_timeout_s": 5,
        }
        base.update(overrides)
        return base

    def test_valid_request_with_empty_cwd(self) -> None:
        params = codex_child._validate_exec_request(self.request())
        self.assertEqual(params.cwd_path, self.run_dir / "empty")
        self.assertTrue(params.cwd_path.is_dir())

    def test_requires_prepared_worktree_for_wt(self) -> None:
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_exec_request(self.request(cwd="wt"))
        (self.run_dir / "wt").mkdir()
        params = codex_child._validate_exec_request(self.request(cwd="wt"))
        self.assertEqual(params.cwd_path, self.run_dir / "wt")

    def test_rejects_bad_model(self) -> None:
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_exec_request(self.request(model="gpt-4"))

    def test_rejects_bad_effort(self) -> None:
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_exec_request(self.request(effort="ultra"))

    def test_rejects_bad_sandbox(self) -> None:
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_exec_request(self.request(sandbox="danger-full-access"))

    def test_rejects_bad_lane(self) -> None:
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_exec_request(self.request(lane="admin"))

    def test_rejects_bad_cwd_kind(self) -> None:
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_exec_request(self.request(cwd="anywhere"))

    def test_rejects_non_bool_multi_agent(self) -> None:
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_exec_request(self.request(multi_agent="false"))

    def test_rejects_out_of_range_timeout(self) -> None:
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_exec_request(self.request(timeout_s=0))
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_exec_request(self.request(timeout_s=99999))
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_exec_request(self.request(timeout_s=True))

    def test_rejects_image_path_outside_run_dir(self) -> None:
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_exec_request(self.request(images=["/etc/passwd"]))

    def test_rejects_image_path_traversal(self) -> None:
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_exec_request(
                self.request(images=[str(self.run_dir / ".." / "escape.png")])
            )

    def test_accepts_image_inside_run_dir(self) -> None:
        image_dir = self.run_dir / "images"
        image_dir.mkdir()
        image = image_dir / "a.png"
        image.write_bytes(b"x")
        params = codex_child._validate_exec_request(self.request(images=[str(image)]))
        self.assertEqual(params.images, [image.resolve()])

    def test_rejects_too_many_images(self) -> None:
        image_dir = self.run_dir / "images"
        image_dir.mkdir()
        paths = []
        for index in range(codex_child.MAX_IMAGES + 1):
            image = image_dir / f"{index}.png"
            image.write_bytes(b"x")
            paths.append(str(image))
        with self.assertRaises(codex_child.ChildRefusal):
            codex_child._validate_exec_request(self.request(images=paths))


class BuildCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name) / "run"
        self.run_dir.mkdir()
        (self.run_dir / "empty").mkdir()
        patcher = patch.object(codex_child, "CODEX_BINARY", "/fake/codex")
        patcher.start()
        self.addCleanup(patcher.stop)

    def params(self, **overrides) -> codex_child.ExecParams:
        base = dict(
            run_dir=self.run_dir,
            lane="code",
            cwd_path=self.run_dir / "empty",
            model="gpt-6-luna",
            effort="high",
            sandbox="workspace-write",
            multi_agent=False,
            prompt="hi",
            images=[],
            output_schema=None,
            timeout_s=10.0,
            idle_timeout_s=5.0,
            codex_home=Path("/fake/home/.codex-code"),
        )
        base.update(overrides)
        return codex_child.ExecParams(**base)

    def test_command_multi_agent_false_adds_flag(self) -> None:
        params = self.params(multi_agent=False)
        command = codex_child._build_exec_command(
            params, self.run_dir / "out/schema.json", self.run_dir / "out/final.txt"
        )
        self.assertIn("-c", command)
        self.assertIn("features.multi_agent=false", command)

    def test_command_multi_agent_true_omits_flag(self) -> None:
        params = self.params(multi_agent=True)
        command = codex_child._build_exec_command(
            params, self.run_dir / "out/schema.json", self.run_dir / "out/final.txt"
        )
        self.assertNotIn("features.multi_agent=false", command)

    def test_command_never_has_ephemeral(self) -> None:
        for multi_agent in (True, False):
            params = self.params(multi_agent=multi_agent)
            command = codex_child._build_exec_command(
                params, self.run_dir / "out/schema.json", self.run_dir / "out/final.txt"
            )
            self.assertNotIn("--ephemeral", command)

    def test_command_exact_shape_no_schema_no_images(self) -> None:
        params = self.params(multi_agent=False)
        schema_path = self.run_dir / "out" / "schema.json"
        final_path = self.run_dir / "out" / "final.txt"
        command = codex_child._build_exec_command(params, schema_path, final_path)
        self.assertEqual(
            command,
            [
                "/fake/codex",
                "exec",
                "--sandbox",
                "workspace-write",
                "-c",
                "approval_policy=never",
                "-c",
                "model_reasoning_effort=high",
                "--model",
                "gpt-6-luna",
                "--json",
                "-c",
                "sandbox_workspace_write.exclude_slash_tmp=true",
                "-c",
                "features.multi_agent=false",
                "--output-last-message",
                str(final_path),
                "-",
            ],
        )

    def test_command_includes_schema_and_images_in_order(self) -> None:
        params = self.params(
            multi_agent=True,
            output_schema={"type": "object"},
            images=[Path("/run/a.png"), Path("/run/b.png")],
        )
        schema_path = self.run_dir / "out" / "schema.json"
        final_path = self.run_dir / "out" / "final.txt"
        command = codex_child._build_exec_command(params, schema_path, final_path)
        self.assertEqual(
            command,
            [
                "/fake/codex",
                "exec",
                "--sandbox",
                "workspace-write",
                "-c",
                "approval_policy=never",
                "-c",
                "model_reasoning_effort=high",
                "--model",
                "gpt-6-luna",
                "--json",
                "-c",
                "sandbox_workspace_write.exclude_slash_tmp=true",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(final_path),
                "--image",
                "/run/a.png",
                "--image",
                "/run/b.png",
                "-",
            ],
        )


class ExecEnvTests(unittest.TestCase):
    def test_env_has_only_allowlisted_keys(self) -> None:
        run_dir = Path("/tmp/run-1")
        params = codex_child.ExecParams(
            run_dir=run_dir,
            lane="chat",
            cwd_path=run_dir / "empty",
            model="gpt-6-sol",
            effort="medium",
            sandbox="read-only",
            multi_agent=True,
            prompt="hi",
            images=[],
            output_schema=None,
            timeout_s=1.0,
            idle_timeout_s=1.0,
            codex_home=Path("/home/agent-codex/.codex-chat"),
        )
        env = codex_child._exec_env(params)
        self.assertEqual(
            set(env),
            {
                "HOME",
                "CODEX_HOME",
                "PATH",
                "TMPDIR",
                "LANG",
                "LC_ALL",
                "GIT_CONFIG_NOSYSTEM",
                "GIT_CONFIG_GLOBAL",
            },
        )
        self.assertEqual(env["CODEX_HOME"], "/home/agent-codex/.codex-chat")
        self.assertEqual(env["TMPDIR"], str(run_dir / "tmp"))
        self.assertEqual(env["LANG"], "C.UTF-8")
        self.assertEqual(env["LC_ALL"], "C.UTF-8")


class RawDiffParsingTests(unittest.TestCase):
    def test_parses_added_modified_deleted(self) -> None:
        raw = (
            ":000000 100644 0000000 1111111 A\tnew.txt\n"
            ":100644 100644 2222222 3333333 M\tchanged.txt\n"
            ":100644 000000 4444444 0000000 D\tremoved.txt\n"
        )
        paths, bad = codex_child._parse_raw_diff(raw)
        self.assertIsNone(bad)
        self.assertEqual(paths, ["new.txt", "changed.txt", "removed.txt"])

    def test_flags_symlink_mode(self) -> None:
        raw = ":000000 120000 0000000 1111111 A\tlink\n"
        _paths, bad = codex_child._parse_raw_diff(raw)
        self.assertEqual(bad, "link")

    def test_flags_submodule_mode(self) -> None:
        raw = ":000000 160000 0000000 1111111 A\tsub\n"
        _paths, bad = codex_child._parse_raw_diff(raw)
        self.assertEqual(bad, "sub")


class DescendantPidTests(unittest.TestCase):
    def test_finds_all_descendants(self) -> None:
        table = [(1, 0), (10, 1), (11, 1), (20, 10), (21, 10), (30, 20), (99, 1234)]
        result = codex_child._descendant_pids(1, table)
        self.assertEqual(set(result), {10, 11, 20, 21, 30})
        self.assertNotIn(99, result)

    def test_no_descendants(self) -> None:
        self.assertEqual(codex_child._descendant_pids(5, [(1, 0)]), [])


class KillTreeUnitTests(unittest.TestCase):
    def test_sigterm_then_sigkill_when_still_alive(self) -> None:
        signals: list[tuple[int, int]] = []
        alive = {100, 101}

        def fake_provider():
            return [(100, 1), (101, 100)]

        def fake_signal(pid: int, sig: int) -> None:
            signals.append((pid, sig))
            if sig == 9:
                alive.discard(pid)

        def fake_alive(pid: int) -> bool:
            return pid in alive

        with (
            patch.object(codex_child, "_signal_pid", fake_signal),
            patch.object(codex_child, "_pid_alive", fake_alive),
        ):
            codex_child.kill_tree(
                100, proc_table_provider=fake_provider, grace_s=0.05, sleep=lambda _s: None
            )

        term = {pid for pid, sig in signals if sig == 15}
        kill = {pid for pid, sig in signals if sig == 9}
        self.assertEqual(term, {100, 101})
        self.assertEqual(kill, {100, 101})

    def test_no_sigkill_if_already_dead(self) -> None:
        signals: list[tuple[int, int]] = []

        def fake_provider():
            return [(200, 1)]

        def fake_signal(pid: int, sig: int) -> None:
            signals.append((pid, sig))

        with (
            patch.object(codex_child, "_signal_pid", fake_signal),
            patch.object(codex_child, "_pid_alive", lambda pid: False),
        ):
            codex_child.kill_tree(
                200, proc_table_provider=fake_provider, grace_s=5, sleep=lambda _s: None
            )

        self.assertEqual(signals, [(200, 15)])


class RolloutUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sessions_dir = Path(self._tmp.name) / "sessions"
        self.sessions_dir.mkdir()

    def _token_count_line(self, ordinal: int, tokens: int, cached: int = 1) -> dict:
        # Real shape: outer type is "event_msg", the token_count event is
        # nested under "payload", and total_token_usage carries more fields
        # than we sum (cache_write_input_tokens, reasoning_output_tokens,
        # total_tokens) -- codex_child only sums the three the frame reports.
        return {
            "ordinal": ordinal,
            "timestamp": "2026-09-26T00:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": tokens,
                        "cached_input_tokens": cached,
                        "cache_write_input_tokens": 0,
                        "output_tokens": tokens // 2,
                        "reasoning_output_tokens": 0,
                        "total_tokens": tokens + tokens // 2,
                    },
                    "last_token_usage": {
                        "input_tokens": tokens,
                        "cached_input_tokens": cached,
                        "output_tokens": tokens // 2,
                    },
                },
            },
        }

    def write_root_rollout(self, name: str, root_thread_id: str, tokens: int) -> Path:
        path = self.sessions_dir / name
        lines = [
            {
                "ordinal": 0,
                "timestamp": "2026-09-26T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": root_thread_id,
                    "session_id": root_thread_id,
                    "source": "exec",
                },
            },
            self._token_count_line(1, tokens),
        ]
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        return path

    def write_subagent_rollout(
        self, name: str, own_thread_id: str, root_thread_id: str, tokens: int
    ) -> Path:
        path = self.sessions_dir / name
        lines = [
            {
                "ordinal": 0,
                "timestamp": "2026-09-26T00:00:01Z",
                "type": "session_meta",
                "payload": {
                    "id": own_thread_id,
                    "session_id": root_thread_id,
                    "parent_thread_id": root_thread_id,
                    "subagent_history_start_ordinal": 5,
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": root_thread_id,
                                "depth": 1,
                                "agent_role": "luna_worker",
                            }
                        }
                    },
                },
            },
            # The forked parent history: a SECOND session_meta line, copied
            # from the parent, that aggregation must ignore.
            {
                "ordinal": 1,
                "timestamp": "2026-09-26T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": root_thread_id,
                    "session_id": root_thread_id,
                    "source": "exec",
                },
            },
            self._token_count_line(2, tokens),
        ]
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        return path

    def test_sums_parent_and_child_and_deletes(self) -> None:
        parent = self.write_root_rollout("rollout-parent.jsonl", "parent-1", 100)
        child = self.write_subagent_rollout("rollout-child.jsonl", "child-1", "parent-1", 40)
        unrelated = self.write_root_rollout("rollout-other.jsonl", "other-1", 999)

        usage, thread_id = codex_child._collect_and_delete_usage(
            self.sessions_dir.parent, {"type": "thread.started", "thread_id": "parent-1"}, None
        )
        self.assertEqual(thread_id, "parent-1")
        self.assertEqual(
            usage, {"input_tokens": 140, "cached_input_tokens": 2, "output_tokens": 70}
        )
        self.assertFalse(parent.exists())
        self.assertFalse(child.exists())
        self.assertTrue(unrelated.exists())

    def test_ignores_second_session_meta_line(self) -> None:
        # The sub-agent file's ordinal-1 session_meta claims the id of the
        # ROOT thread; if aggregation looked past the first line it would
        # wrongly treat this file as the root's own rollout too (it still
        # matches by session_id either way here, so assert the more direct
        # thing: the match decision comes from `_session_meta_first_line`,
        # which only ever returns the first line's payload).
        child = self.write_subagent_rollout("rollout-child.jsonl", "child-1", "root-9", 40)
        payload = codex_child._session_meta_first_line(child)
        self.assertEqual(payload["id"], "child-1")
        self.assertEqual(payload["session_id"], "root-9")

    def test_falls_back_to_turn_usage_without_rollouts(self) -> None:
        fallback = {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 1}
        usage, thread_id = codex_child._collect_and_delete_usage(
            self.sessions_dir.parent, {"type": "thread.started", "thread_id": "nope"}, fallback
        )
        self.assertEqual(usage, fallback)
        self.assertEqual(thread_id, "nope")

    def test_no_thread_started_uses_fallback_and_no_thread_id(self) -> None:
        fallback = {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 0}
        usage, thread_id = codex_child._collect_and_delete_usage(
            self.sessions_dir.parent, None, fallback
        )
        self.assertEqual(usage, fallback)
        self.assertIsNone(thread_id)


# ---------------------------------------------------------------------------
# Subprocess-level integration tests
# ---------------------------------------------------------------------------


class PrepareIntegrationTests(ChildProcessTestCase):
    def test_prepare_success_returns_head(self) -> None:
        mirror, sha = build_mirror(self.mirrors_dir, "acme/widgets", {"README.md": "hi\n"})
        run_id = new_run_id()
        completed = self.run_child(
            "prepare",
            {"run_id": run_id, "repo": "acme/widgets", "mirror": mirror, "base_sha": sha},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        body = json.loads(completed.stdout)
        self.assertEqual(body, {"ok": True, "head": sha})
        wt = self.work_root / run_id / "wt"
        self.assertTrue((wt / "README.md").is_file())

    def test_prepare_refuses_dot_codex(self) -> None:
        mirror, sha = build_mirror(
            self.mirrors_dir,
            "acme/hasagents",
            {"README.md": "hi\n", ".codex/agents/x.toml": "bad = true\n"},
        )
        run_id = new_run_id()
        completed = self.run_child(
            "prepare",
            {"run_id": run_id, "repo": "acme/hasagents", "mirror": mirror, "base_sha": sha},
        )
        self.assertEqual(completed.returncode, 3)
        reason = json.loads(completed.stdout)["reason"]
        self.assertIn(".codex", reason)

    def test_prepare_refuses_dot_agents_in_subdir(self) -> None:
        mirror, sha = build_mirror(
            self.mirrors_dir,
            "acme/hasagents2",
            {"src/.agents/x": "bad\n", "README.md": "hi\n"},
        )
        run_id = new_run_id()
        completed = self.run_child(
            "prepare",
            {"run_id": run_id, "repo": "acme/hasagents2", "mirror": mirror, "base_sha": sha},
        )
        self.assertEqual(completed.returncode, 3)

    def test_prepare_rejects_mirror_outside_dir(self) -> None:
        run_id = new_run_id()
        completed = self.run_child(
            "prepare",
            {
                "run_id": run_id,
                "repo": "acme/widgets",
                "mirror": "/tmp/not-a-mirror.git",
                "base_sha": "a" * 40,
            },
        )
        self.assertEqual(completed.returncode, 3)

    def test_prepare_rejects_bad_sha(self) -> None:
        mirror, _sha = build_mirror(self.mirrors_dir, "acme/widgets", {"README.md": "hi\n"})
        run_id = new_run_id()
        completed = self.run_child(
            "prepare",
            {
                "run_id": run_id,
                "repo": "acme/widgets",
                "mirror": mirror,
                "base_sha": "not-a-sha",
            },
        )
        self.assertEqual(completed.returncode, 3)

    def test_prepare_rejects_bad_run_id(self) -> None:
        mirror, sha = build_mirror(self.mirrors_dir, "acme/widgets", {"README.md": "hi\n"})
        completed = self.run_child(
            "prepare",
            {"run_id": "../escape", "repo": "acme/widgets", "mirror": mirror, "base_sha": sha},
        )
        self.assertEqual(completed.returncode, 3)


class ExecAllowlistIntegrationTests(ChildProcessTestCase):
    def test_rejects_bad_model(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        request = self.base_exec_request(run_id, model="not-a-model")
        completed = self.run_child("exec", request)
        self.assertEqual(completed.returncode, 3)
        self.assertIn("model", json.loads(completed.stdout)["reason"])

    def test_rejects_bad_lane(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        completed = self.run_child("exec", self.base_exec_request(run_id, lane="root"))
        self.assertEqual(completed.returncode, 3)

    def test_rejects_bad_run_id_shape(self) -> None:
        completed = self.run_child("exec", self.base_exec_request("../../etc"))
        self.assertEqual(completed.returncode, 3)

    def test_rejects_oversized_request(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        request = self.base_exec_request(
            run_id, prompt="x" * (codex_child.MAX_REQUEST_BYTES + 10)
        )
        completed = self.run_child("exec", request)
        self.assertEqual(completed.returncode, 2)

    def test_rejects_bad_json(self) -> None:
        completed = subprocess.run(
            [PYTHON_BIN, str(CHILD_SCRIPT), "exec"],
            input=b"not json",
            capture_output=True,
            env=self.child_env(),
            timeout=10,
        )
        self.assertEqual(completed.returncode, 2)

    def test_unknown_subcommand(self) -> None:
        completed = subprocess.run(
            [PYTHON_BIN, str(CHILD_SCRIPT), "bogus"],
            input=b"{}",
            capture_output=True,
            env=self.child_env(),
            timeout=10,
        )
        self.assertEqual(completed.returncode, 2)


class ExecStreamingIntegrationTests(ChildProcessTestCase):
    def write_control(self, run_id: str, control: dict) -> None:
        tmp_dir = self.work_root / run_id / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        (tmp_dir / "fake_codex_control.json").write_text(json.dumps(control), encoding="utf-8")

    def test_streams_events_and_final_frame(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        self.write_control(
            run_id,
            {
                "scenario": "normal",
                "thread_id": "parent-abc",
                "final_message": "all done",
                "tokens": 77,
            },
        )
        request = self.base_exec_request(run_id, timeout_s=15, idle_timeout_s=10)
        completed = self.run_child("exec", request)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        events = [json.loads(line) for line in lines]
        types = [event.get("type") for event in events]
        self.assertIn("thread.started", types)
        self.assertIn("turn.completed", types)
        self.assertEqual(events[-1]["type"], "agent_svc.result")
        frame = events[-1]
        self.assertEqual(frame["exit_code"], 0)
        self.assertFalse(frame["timed_out"])
        self.assertFalse(frame["idle_killed"])
        self.assertEqual(frame["final_message"], "all done")
        self.assertEqual(frame["thread_id"], "parent-abc")
        self.assertEqual(
            frame["usage"], {"input_tokens": 77, "cached_input_tokens": 0, "output_tokens": 19}
        )
        # Rollout files are deleted once usage has been read.
        sessions_dir = self.codex_home_code / "sessions"
        self.assertEqual(list(sessions_dir.glob("rollout-*.jsonl")), [])

    def test_usage_sums_parent_and_child_rollouts(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        self.write_control(
            run_id,
            {
                "scenario": "normal",
                "thread_id": "parent-xyz",
                "child_thread_id": "child-xyz",
                "tokens": 100,
                "child_tokens": 40,
            },
        )
        request = self.base_exec_request(run_id, timeout_s=15, idle_timeout_s=10)
        completed = self.run_child("exec", request)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        frame = json.loads(completed.stdout.splitlines()[-1])
        self.assertEqual(frame["usage"]["input_tokens"], 140)
        self.assertEqual(frame["usage"]["output_tokens"], 25 + 10)
        sessions_dir = self.codex_home_code / "sessions"
        self.assertEqual(list(sessions_dir.glob("rollout-*.jsonl")), [])

    def test_exec_writes_and_uses_output_schema(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        self.write_control(run_id, {"scenario": "normal", "thread_id": "t1"})
        request = self.base_exec_request(
            run_id, timeout_s=15, idle_timeout_s=10, output_schema={"type": "object"}
        )
        completed = self.run_child("exec", request)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        schema_path = self.work_root / run_id / "out" / "schema.json"
        self.assertEqual(json.loads(schema_path.read_text()), {"type": "object"})

    def test_nonzero_exit_reported_in_frame(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        self.write_control(run_id, {"scenario": "fail", "thread_id": "t-fail"})
        request = self.base_exec_request(run_id, timeout_s=15, idle_timeout_s=10)
        completed = self.run_child("exec", request)
        self.assertEqual(
            completed.returncode, 0
        )  # child itself still exits 0 (emitted a frame)
        frame = json.loads(completed.stdout.splitlines()[-1])
        self.assertEqual(frame["exit_code"], 7)
        self.assertIn("simulated failure", "\n".join(frame["stderr_tail"]))


class ExecTimeoutIntegrationTests(ChildProcessTestCase):
    def write_control(self, run_id: str, control: dict) -> None:
        tmp_dir = self.work_root / run_id / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        (tmp_dir / "fake_codex_control.json").write_text(json.dumps(control), encoding="utf-8")

    def test_overall_timeout_kills_hanging_codex(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        self.write_control(run_id, {"scenario": "hang", "sleep_s": 120, "thread_id": "hang-1"})
        request = self.base_exec_request(run_id, timeout_s=1, idle_timeout_s=30)
        start = time.monotonic()
        completed = self.run_child("exec", request, timeout=20)
        elapsed = time.monotonic() - start
        self.assertEqual(completed.returncode, 0, completed.stderr)
        frame = json.loads(completed.stdout.splitlines()[-1])
        self.assertTrue(frame["timed_out"])
        self.assertFalse(frame["idle_killed"])
        self.assertLess(elapsed, 15)

    def test_idle_timeout_kills_silent_codex(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        self.write_control(run_id, {"scenario": "idle", "sleep_s": 120, "thread_id": "idle-1"})
        request = self.base_exec_request(run_id, timeout_s=30, idle_timeout_s=1)
        start = time.monotonic()
        completed = self.run_child("exec", request, timeout=20)
        elapsed = time.monotonic() - start
        self.assertEqual(completed.returncode, 0, completed.stderr)
        frame = json.loads(completed.stdout.splitlines()[-1])
        self.assertFalse(frame["timed_out"])
        self.assertTrue(frame["idle_killed"])
        self.assertLess(elapsed, 15)

    def test_huge_stdout_is_fully_streamed(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        self.write_control(
            run_id, {"scenario": "huge_stdout", "line_count": 4000, "thread_id": "huge-1"}
        )
        request = self.base_exec_request(run_id, timeout_s=30, idle_timeout_s=20)
        completed = self.run_child("exec", request, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        # 4000 item.completed lines + thread.started + final frame.
        self.assertEqual(len(lines), 4002)
        self.assertEqual(json.loads(lines[-1])["type"], "agent_svc.result")


@unittest.skipUnless(hasattr(os, "fork"), "requires POSIX fork to create a grandchild process")
class ExecSignalIntegrationTests(ChildProcessTestCase):
    def test_sigterm_kills_grandchild_in_new_session(self) -> None:
        run_id = new_run_id()
        run_dir = self.make_run_dir(run_id)
        tmp_dir = run_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        marker = tmp_dir / "grandchild.pid"
        (tmp_dir / "fake_codex_control.json").write_text(
            json.dumps(
                {
                    "scenario": "grandchild",
                    "sleep_s": 60,
                    "thread_id": "gc-1",
                    "grandchild_marker": str(marker),
                }
            ),
            encoding="utf-8",
        )
        request = self.base_exec_request(run_id, timeout_s=60, idle_timeout_s=60)
        process = self.run_child("exec", request, popen=True)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.1)
            self.assertTrue(marker.exists(), "fake codex never reported its grandchild pid")
            grandchild_pid = int(marker.read_text().strip())
            self.assertTrue(_pid_alive_for_test(grandchild_pid))

            process.send_signal(codex_child.signal.SIGTERM)
            # `run_child(popen=True)` already wrote and closed stdin, so
            # `communicate()` (which tries to flush/close stdin itself) isn't
            # usable here; wait for exit, then drain the already-closed pipes.
            process.wait(timeout=15)
            stderr = process.stderr.read() if process.stderr else ""
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

        self.assertEqual(process.returncode, 0, stderr)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive_for_test(grandchild_pid):
            time.sleep(0.1)
        self.assertFalse(
            _pid_alive_for_test(grandchild_pid), "grandchild in a new session survived SIGTERM"
        )


def _pid_alive_for_test(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class PackageIntegrationTests(ChildProcessTestCase):
    def prepared_run(self) -> tuple[str, Path]:
        mirror, sha = build_mirror(
            self.mirrors_dir, "acme/pkg", {"README.md": "hi\n", "keep.txt": "keep\n"}
        )
        run_id = new_run_id()
        completed = self.run_child(
            "prepare",
            {"run_id": run_id, "repo": "acme/pkg", "mirror": mirror, "base_sha": sha},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        wt = self.work_root / run_id / "wt"
        return run_id, wt

    def test_includes_modified_and_untracked_files(self) -> None:
        run_id, wt = self.prepared_run()
        (wt / "README.md").write_text("hi\nedited\n", encoding="utf-8")
        (wt / "new_untracked.txt").write_text("brand new\n", encoding="utf-8")
        completed = self.run_child("package", {"run_id": run_id})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        body = json.loads(completed.stdout)
        self.assertEqual(set(body["changed_paths"]), {"README.md", "new_untracked.txt"})
        self.assertGreater(body["bytes"], 0)
        patch_bytes = base64.b64decode(body["patch_b64"])
        self.assertIn(b"new_untracked.txt", patch_bytes)
        self.assertIn(b"edited", patch_bytes)

    def test_empty_patch_when_nothing_changed(self) -> None:
        run_id, _wt = self.prepared_run()
        completed = self.run_child("package", {"run_id": run_id})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout), {"patch_b64": "", "changed_paths": [], "bytes": 0}
        )

    def test_rejects_symlink(self) -> None:
        run_id, wt = self.prepared_run()
        (wt / "escape_link").symlink_to("/etc/passwd")
        completed = self.run_child("package", {"run_id": run_id})
        self.assertEqual(completed.returncode, 3)
        self.assertIn("symlink", json.loads(completed.stdout)["reason"])

    def test_rejects_submodule_gitlink(self) -> None:
        # `cmd_package` always runs its own `git read-tree HEAD` before `git
        # add -A`, so a gitlink staged directly on the temp index ahead of
        # time (e.g. via `git update-index --add --cacheinfo 160000,<sha>,sub`,
        # the mechanism `_parse_raw_diff`'s unit test below exercises
        # directly) would just be wiped by that read-tree. The realistic way
        # a gitlink shows up in `add -A`'s diff is the same way `git
        # submodule add` produces one: a nested git repository inside the
        # worktree. `git add -A` picks that up as a mode 160000 entry on its
        # own, which is what we assert `cmd_package` refuses here.
        run_id, wt = self.prepared_run()
        nested = wt / "sub"
        nested.mkdir()
        env = {**os.environ, **GIT_AUTHOR_ENV}
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=str(nested),
            env=env,
            check=True,
            capture_output=True,
        )
        (nested / "inner.txt").write_text("inner\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-A"], cwd=str(nested), env=env, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "inner"],
            cwd=str(nested),
            env=env,
            check=True,
            capture_output=True,
        )
        completed = self.run_child("package", {"run_id": run_id})
        self.assertEqual(completed.returncode, 3)
        self.assertIn("submodule", json.loads(completed.stdout)["reason"])

    def test_rejects_patch_over_size_cap(self) -> None:
        run_id, wt = self.prepared_run()
        big = wt / "big.bin"
        big.write_bytes(os.urandom(codex_child.MAX_PATCH_BYTES + 1024))
        completed = self.run_child("package", {"run_id": run_id})
        self.assertEqual(completed.returncode, 3)
        self.assertIn("size", json.loads(completed.stdout)["reason"])

    def test_rejects_missing_worktree(self) -> None:
        run_id = new_run_id()
        self.make_run_dir(run_id)
        completed = self.run_child("package", {"run_id": run_id})
        self.assertEqual(completed.returncode, 3)


class CleanupIntegrationTests(ChildProcessTestCase):
    def test_removes_only_wt_tmp_out(self) -> None:
        run_id = new_run_id()
        run_dir = self.make_run_dir(run_id)
        for name in ("wt", "tmp", "out", "images"):
            (run_dir / name).mkdir()
            (run_dir / name / "file").write_text("x", encoding="utf-8")
        completed = self.run_child("cleanup", {"run_id": run_id})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"ok": True})
        for name in ("wt", "tmp", "out"):
            self.assertFalse((run_dir / name).exists())
        self.assertTrue((run_dir / "images").exists())
        self.assertTrue((run_dir / "images" / "file").exists())

    def test_confined_to_run_dir_even_with_missing_dirs(self) -> None:
        run_id = new_run_id()
        run_dir = self.make_run_dir(run_id)
        sibling = self.work_root / "sibling-marker"
        sibling.write_text("do not touch", encoding="utf-8")
        completed = self.run_child("cleanup", {"run_id": run_id})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(sibling.exists())
        self.assertTrue(run_dir.exists())

    def test_rejects_bad_run_id(self) -> None:
        completed = self.run_child("cleanup", {"run_id": "../../etc"})
        self.assertEqual(completed.returncode, 3)


class TestModeGuardTests(unittest.TestCase):
    def test_disabled_without_env_flag(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_CHILD_TEST_MODE", None)
            self.assertFalse(codex_child._test_mode_enabled())

    def test_disabled_as_root(self) -> None:
        with (
            patch.dict(os.environ, {"AGENT_CHILD_TEST_MODE": "1"}),
            patch.object(codex_child.os, "geteuid", return_value=0),
        ):
            self.assertFalse(codex_child._test_mode_enabled())

    def test_disabled_as_agent_codex(self) -> None:
        class FakePwEntry:
            pw_name = "agent-codex"

        with (
            patch.dict(os.environ, {"AGENT_CHILD_TEST_MODE": "1"}),
            patch.object(codex_child.os, "geteuid", return_value=999),
            patch.object(codex_child.pwd, "getpwuid", return_value=FakePwEntry()),
        ):
            self.assertFalse(codex_child._test_mode_enabled())

    def test_enabled_for_ordinary_user(self) -> None:
        class FakePwEntry:
            pw_name = "someone-else"

        with (
            patch.dict(os.environ, {"AGENT_CHILD_TEST_MODE": "1"}),
            patch.object(codex_child.os, "geteuid", return_value=501),
            patch.object(codex_child.pwd, "getpwuid", return_value=FakePwEntry()),
        ):
            self.assertTrue(codex_child._test_mode_enabled())


# ---------------------------------------------------------------------------
# image_state.py (runs as root via sudo; argv is a list of container names)
# ---------------------------------------------------------------------------


class FakeInspectCompleted:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


class ImageStateTests(unittest.TestCase):
    """Fixture catalogs mirror the real `backend/app/services/agent_repos.py`
    shape: `REPOSITORIES` (a tuple of `AgentRepository`-like objects, private
    repos among them carrying an empty `images` tuple) plus one more
    `AgentRepository` in `QA_REPOSITORY`, each `images` a tuple of
    (container_name, image_repo) pairs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.trusted_dir = Path(self._tmp.name)

    def write_catalog(
        self,
        *,
        repo_images: list[tuple[str, str]] | None = None,
        include_private_repo: bool = False,
        qa_images: list[tuple[str, str]] | None = None,
    ) -> None:
        repo_images = repo_images or []
        qa_images = qa_images or []
        lines = [
            "from dataclasses import dataclass",
            "",
            "@dataclass(frozen=True)",
            "class AgentRepository:",
            "    images: tuple = ()",
            "",
            f"_repo_a = AgentRepository(images={tuple(repo_images)!r})",
        ]
        repositories = ["_repo_a"]
        if include_private_repo:
            lines.append("_repo_private = AgentRepository(images=())")
            repositories.append("_repo_private")
        lines.append(f"REPOSITORIES = ({', '.join(repositories)},)")
        lines.append(f"QA_REPOSITORY = AgentRepository(images={tuple(qa_images)!r})")
        (self.trusted_dir / "agent_repos.py").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def capture_output(self, argv: list[str], *, runner) -> tuple[int, list[str]]:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = image_state.main(argv, trusted_dir=self.trusted_dir, runner=runner)
        lines = [line for line in captured.getvalue().splitlines() if line.strip()]
        return code, lines

    def test_rejects_unknown_container(self) -> None:
        self.write_catalog(repo_images=[("agent-code", "acme/agent-code")])
        code, _ = self.capture_output(
            ["image_state.py", "not-in-catalog"],
            runner=lambda *a, **k: FakeInspectCompleted(""),
        )
        self.assertEqual(code, 3)

    def test_rejects_when_any_name_unknown(self) -> None:
        self.write_catalog(repo_images=[("agent-code", "acme/agent-code")])
        code, _ = self.capture_output(
            ["image_state.py", "agent-code", "bogus"],
            runner=lambda *a, **k: FakeInspectCompleted(""),
        )
        self.assertEqual(code, 3)

    def test_no_args_is_rejected(self) -> None:
        self.write_catalog(repo_images=[("agent-code", "acme/agent-code")])
        code, _ = self.capture_output(
            ["image_state.py"], runner=lambda *a, **k: FakeInspectCompleted("")
        )
        self.assertEqual(code, 2)

    def test_missing_catalog_module_refused(self) -> None:
        code, _ = self.capture_output(
            ["image_state.py", "agent-code"], runner=lambda *a, **k: FakeInspectCompleted("")
        )
        self.assertEqual(code, 3)

    def test_missing_repositories_attribute_refused(self) -> None:
        (self.trusted_dir / "agent_repos.py").write_text(
            "SOMETHING_ELSE = 1\n", encoding="utf-8"
        )
        code, _ = self.capture_output(
            ["image_state.py", "agent-code"], runner=lambda *a, **k: FakeInspectCompleted("")
        )
        self.assertEqual(code, 3)

    def test_private_repo_contributes_no_names(self) -> None:
        self.write_catalog(
            repo_images=[("agent-code", "acme/agent-code")], include_private_repo=True
        )
        code, _ = self.capture_output(
            ["image_state.py", "some-private-container"],
            runner=lambda *a, **k: FakeInspectCompleted(""),
        )
        self.assertEqual(code, 3)

    def test_qa_repository_names_are_allowed(self) -> None:
        # Names come from both REPOSITORIES and QA_REPOSITORY.
        self.write_catalog(
            repo_images=[("agent-code", "acme/agent-code")],
            qa_images=[("agent-qa", "acme/agent-qa")],
        )
        code, buffer = self.capture_output(
            ["image_state.py", "agent-qa"],
            runner=lambda *a, **k: FakeInspectCompleted('{"Config": {}, "State": {}}'),
        )
        self.assertEqual(code, 0)
        self.assertIn("agent-qa", json.loads(buffer[0]))

    def test_parses_docker_inspect_output_via_injected_runner(self) -> None:
        self.write_catalog(
            repo_images=[("agent-code", "acme/agent-code")],
            qa_images=[("agent-chat", "acme/agent-chat")],
        )
        calls: list[list[str]] = []

        def fake_runner(argv, **kwargs):
            calls.append(argv)
            name = argv[-1]
            if name == "agent-code":
                payload = {
                    "Config": {"Image": "acme/agent-code:1.2.3"},
                    "State": {"Running": True, "Health": {"Status": "healthy"}},
                }
            else:
                payload = {
                    "Config": {"Image": "acme/agent-chat:9"},
                    "State": {"Running": False},
                }
            return FakeInspectCompleted(json.dumps(payload))

        code, buffer = self.capture_output(
            ["image_state.py", "agent-code", "agent-chat"], runner=fake_runner
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        for call, name in zip(calls, ("agent-code", "agent-chat"), strict=True):
            self.assertEqual(
                call,
                [
                    image_state.DOCKER_BINARY,
                    "inspect",
                    "--type",
                    "container",
                    "--format",
                    "{{json .}}",
                    "--",
                    name,
                ],
            )
        result = json.loads(buffer[0])
        self.assertEqual(
            result,
            {
                "agent-code": {
                    "image": "acme/agent-code:1.2.3",
                    "running": True,
                    "health": "healthy",
                },
                "agent-chat": {"image": "acme/agent-chat:9", "running": False, "health": None},
            },
        )

    def test_inspect_failure_yields_null_summary(self) -> None:
        self.write_catalog(repo_images=[("agent-code", "acme/agent-code")])
        code, buffer = self.capture_output(
            ["image_state.py", "agent-code"],
            runner=lambda *a, **k: FakeInspectCompleted("", returncode=1),
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(buffer[0]),
            {"agent-code": {"image": None, "running": False, "health": None}},
        )

    def test_no_shell_true_used(self) -> None:
        import inspect

        self.assertNotIn("shell=True", inspect.getsource(image_state))

    def test_real_subprocess_run_default_uses_argv_list(self) -> None:
        # End-to-end (still no real docker): the default `runner=subprocess.run`
        # is exercised against a stand-in "docker" script on PATH, proving
        # `_docker_inspect` invokes it as an argv list, not a shell string.
        self.write_catalog(repo_images=[("agent-code", "acme/agent-code")])
        fake_docker = self.trusted_dir / "docker"
        fake_docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "print(json.dumps({'Config': {'Image': 'x'}, 'State': {'Running': True}}))\n",
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)
        with patch.object(image_state, "DOCKER_BINARY", str(fake_docker)):
            code, buffer = self.capture_output(
                ["image_state.py", "agent-code"], runner=subprocess.run
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer[0])["agent-code"]["image"], "x")


if __name__ == "__main__":
    unittest.main()
