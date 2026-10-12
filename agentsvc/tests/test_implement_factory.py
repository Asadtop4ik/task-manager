"""Factory acceptance checks in `handle_implement`: test-author station,
splice, Check Gate, fix-ups, spec conflict, time budget, callback fallback."""

from __future__ import annotations

import base64
import json
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from agent_svc import acceptance, implement
from agent_svc.codex import CodexChildError
from agent_svc.http import HttpError
from agent_svc.implement import handle_implement

from .support import (
    FakeCodexRunner,
    build_test_context,
    make_github_remote,
    make_patch,
    run_git,
)
from .test_implement import RUN_ID, _b64, _exec_result, _work

TEST_SRC = (
    "import unittest\n\n\n"
    "class AcceptanceTests(unittest.TestCase):\n"
    "    def test_ac1_header(self):\n"
    "        self.assertEqual('X-Total', 'X-Total')\n\n"
    "    def test_ac2_message(self):\n"
    "        self.assertEqual('Topilmadi', 'Topilmadi')\n"
)
TASK_PATH = "tests/acceptance/test_task_7.py"
INIT_PATH = "tests/acceptance/__init__.py"
BRANCH = "codex/task-7-" + RUN_ID
AUTHOR_OK = json.dumps(
    {
        "status": "ok",
        "checks": [{"criterion": 1, "tests": ["test_ac1_header"]}],
        "unchecked": [],
    }
)
ACCEPTANCE = ("returns header `X-Total: 5`", "404 body is `Topilmadi`")


def _qa_work(**overrides: object):
    base: dict[str, Any] = dict(
        repo_full_name="Asadtop4ik/agent-qa",
        base_branch="main",
        task_revision="a" * 64,
        description="Add a total header `X-Total: 5` and 404 message `Topilmadi`.",
        acceptance=ACCEPTANCE,
    )
    base.update(overrides)
    return _work(**base)


class _Env:
    """One run's fixtures: a real bare remote, a context with the fakes, and
    helpers to queue the author/builder/fix-up execs and packages."""

    def __init__(self, root: Path, **config: Any) -> None:
        self.remote = root / "remote.git"
        self.base_sha = make_github_remote(self.remote)
        self.codex = FakeCodexRunner()
        overrides = {"acceptance_checks_enabled": True, **config}
        self.ctx = build_test_context(
            root, github_remote=self.remote, codex=self.codex, config_overrides=overrides
        )
        self.author_files = {INIT_PATH: "", TASK_PATH: TEST_SRC}
        self.author_patch = make_patch(self.remote, self.base_sha, self.author_files)

    def patch(self, files: dict[str, str], *, with_acceptance: bool = True) -> bytes:
        merged = {**(self.author_files if with_acceptance else {}), **files}
        return make_patch(self.remote, self.base_sha, merged)

    def queue_author(self, *, message: str = AUTHOR_OK, patch_bytes: bytes | None = None) -> None:
        self.codex.queue_exec_result(_exec_result(final_message=message))
        self.codex.queue_package_result(
            {"patch_b64": _b64(self.author_patch if patch_bytes is None else patch_bytes)}
        )

    def queue_builder(self, patch_bytes: bytes, *, message: str = "Done.") -> None:
        self.codex.queue_exec_result(_exec_result(final_message=message))
        self.codex.queue_package_result({"patch_b64": _b64(patch_bytes)})

    def queue_fixup(self, patch_bytes: bytes | None, *, message: str = "Fixed.") -> None:
        self.codex.queue_exec_result(_exec_result(final_message=message))
        if patch_bytes is not None:
            self.codex.queue_package_result({"patch_b64": _b64(patch_bytes)})

    def run(self, work: Any = None) -> dict[str, Any]:
        handle_implement(self.ctx, work or _qa_work(), threading.Event())
        callbacks = self.ctx.api.callbacks  # type: ignore[attr-defined]
        assert len(callbacks) == 1, callbacks
        return callbacks[0]

    def stages(self) -> list[str]:
        return [stage for (_run, _lease, stage, _err) in self.ctx.api.stages]  # type: ignore[attr-defined]

    def pushed(self, path: str) -> str:
        return run_git(["git", "-C", str(self.remote), "show", f"refs/heads/{BRANCH}:{path}"]).stdout

    def pr_body(self) -> str:
        return self.ctx.github.created_pulls[0]["body"]  # type: ignore[attr-defined]


def _failed(names: list[str], tail: str = "FAIL: test_ac1_header") -> dict[str, Any]:
    return {
        "status": "failed",
        "total": 2,
        "passed": 2 - len(names),
        "failed": names,
        "output_tail": tail,
        "detail": "",
    }


GATE_KEYS = {
    "status", "total", "passed", "failed", "fixups", "acceptance_modified",
    "author_input_tokens", "author_output_tokens", "fixup_input_tokens",
    "fixup_output_tokens", "author_model", "fixup_model",
}  # fmt: skip


class KillSwitchTests(unittest.TestCase):
    def _assert_untouched(self, env: _Env, payload: dict[str, Any]) -> None:
        self.assertEqual(payload["status"], "pr_opened")
        self.assertNotIn("check_gate", payload)
        self.assertEqual(len(env.codex.run_exec_calls), 1)
        self.assertEqual(env.codex.check_calls, [])
        self.assertNotIn("Locked acceptance", env.codex.run_exec_calls[0]["prompt"])
        self.assertEqual(payload["input_tokens"], 100)
        self.assertNotIn("acceptance_authored", env.stages())

    def test_switch_off_is_identical_to_before(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp), acceptance_checks_enabled=False)
            env.queue_builder(env.patch({"NOTES.md": "hi\n"}, with_acceptance=False))
            self._assert_untouched(env, env.run())

    def test_repo_without_profile_is_untouched(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_builder(env.patch({"NOTES.md": "hi\n"}, with_acceptance=False))
            work = _work(acceptance=ACCEPTANCE)  # task-manager: no profile
            self._assert_untouched(env, env.run(work))

    def test_empty_acceptance_is_untouched(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_builder(env.patch({"NOTES.md": "hi\n"}, with_acceptance=False))
            self._assert_untouched(env, env.run(_qa_work(acceptance=())))

    def test_builder_timeout_is_the_configured_value_when_budget_is_ample(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp), acceptance_checks_enabled=False)
            env.queue_builder(env.patch({"NOTES.md": "hi\n"}, with_acceptance=False))
            env.run()
            self.assertEqual(env.codex.run_exec_calls[0]["timeout_s"], 1200)


class AuthorStationHappyPathTests(unittest.TestCase):
    def test_author_gate_pass_and_pr_with_check_gate(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            env.queue_builder(env.patch({"app.py": "TOTAL = 5\n"}))
            payload = env.run()

            self.assertEqual(payload["status"], "pr_opened")
            gate = payload["check_gate"]
            self.assertEqual(set(gate), GATE_KEYS)
            self.assertEqual(gate["status"], "passed")
            self.assertEqual((gate["total"], gate["passed"], gate["failed"]), (2, 2, []))
            self.assertEqual(gate["fixups"], 0)
            self.assertFalse(gate["acceptance_modified"])
            self.assertEqual(gate["author_input_tokens"], 100)
            self.assertEqual(gate["author_output_tokens"], 20)
            self.assertEqual(gate["fixup_input_tokens"], 0)
            self.assertEqual(gate["author_model"], "gpt-6-luna")
            self.assertIsNone(gate["fixup_model"])
            # author + builder usage summed
            self.assertEqual(payload["input_tokens"], 200)
            self.assertEqual(payload["cached_input_tokens"], 20)
            self.assertEqual(payload["output_tokens"], 40)

            stages = env.stages()
            self.assertIn("acceptance_authored", stages)
            self.assertIn("check_gate_passed", stages)
            self.assertLess(stages.index("acceptance_authored"), stages.index("codex_started"))
            self.assertLess(stages.index("codex_finished"), stages.index("check_gate_passed"))
            self.assertLess(stages.index("check_gate_passed"), stages.index("branch_pushed"))

            # The locked tests are in the PR branch.
            self.assertEqual(env.pushed(TASK_PATH), TEST_SRC)
            self.assertEqual(env.pushed("app.py"), "TOTAL = 5\n")

    def test_author_exec_request_and_prompt(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            env.queue_builder(env.patch({"app.py": "TOTAL = 5\n"}))
            work = _qa_work()
            env.run(work)
            author, builder = env.codex.run_exec_calls
            self.assertEqual(author["cwd"], "wt")
            self.assertEqual(author["lane"], "code")
            self.assertEqual(author["model"], "gpt-6-luna")
            self.assertEqual(author["effort"], "high")
            self.assertEqual(author["sandbox"], "workspace-write")
            self.assertFalse(author["multi_agent"])
            self.assertEqual(author["timeout_s"], 300)
            self.assertIs(author["output_schema"], acceptance.TEST_AUTHOR_SCHEMA)
            self.assertIn(work.description, author["prompt"])
            self.assertIn("1. returns header `X-Total: 5`", author["prompt"])
            self.assertIn(TASK_PATH, author["prompt"])
            # Builder gets the locked-file block and no output schema.
            self.assertIsNone(builder["output_schema"])
            self.assertIn("Locked acceptance tests", builder["prompt"])
            self.assertIn(
                "python3 -m unittest tests.acceptance.test_task_7", builder["prompt"]
            )

    def test_check_request_shape(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            builder = env.patch({"app.py": "TOTAL = 5\n"})
            env.queue_builder(builder)
            env.run()
            self.assertEqual(len(env.codex.check_calls), 1)
            request = env.codex.check_calls[0]
            self.assertEqual(
                set(request),
                {
                    "run_id", "repo", "mirror", "base_sha", "patch_b64", "pinned_b64",
                    "profile", "task_id", "expected_tests", "timeout_s",
                },
            )  # fmt: skip
            self.assertEqual(request["run_id"], RUN_ID)
            self.assertEqual(request["repo"], "Asadtop4ik/agent-qa")
            self.assertEqual(request["mirror"], str(env.ctx.mirrors.mirror_path(request["repo"])))
            self.assertEqual(request["base_sha"], env.base_sha)
            self.assertEqual(request["profile"], "unittest")
            self.assertEqual(request["task_id"], 7)
            self.assertEqual(request["expected_tests"], 2)
            self.assertEqual(base64.b64decode(request["pinned_b64"]), env.author_patch)
            sent = base64.b64decode(request["patch_b64"])
            self.assertIn(b"app.py", sent)
            self.assertNotIn(b"tests/acceptance", sent)
            kwargs = env.codex.check_kwargs[0]
            self.assertIsNotNone(kwargs["cancel"])
            self.assertGreater(kwargs["timeout_s"], request["timeout_s"])

    def test_author_logged_with_sha256_but_not_journaled(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            env.queue_builder(env.patch({"app.py": "TOTAL = 5\n"}))
            stream = env.ctx.logger._stream  # type: ignore[attr-defined]
            env.run()
            text = stream.getvalue()
            self.assertIn("acceptance_authored", text)
            self.assertIn(acceptance.sha256_hex(env.author_patch), text)
            self.assertIn(TASK_PATH, text)

    def test_builder_with_no_changes_outside_tests_is_no_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            env.queue_builder(env.author_patch)  # only the acceptance files
            payload = env.run()
            self.assertEqual(payload["status"], "failed")
            self.assertIn("no file changes", payload["error"])
            self.assertEqual(payload["check_gate"]["status"], "skipped")
            self.assertEqual(env.codex.check_calls, [])
            self.assertEqual(payload["input_tokens"], 200)


class AuthorStationSkipTests(unittest.TestCase):
    def _assert_skipped_and_flow_continues(self, env: _Env, payload: dict[str, Any]) -> None:
        self.assertEqual(payload["status"], "pr_opened")
        gate = payload["check_gate"]
        self.assertEqual(set(gate), GATE_KEYS)
        self.assertEqual(gate["status"], "skipped")
        self.assertEqual((gate["total"], gate["passed"], gate["fixups"]), (0, 0, 0))
        self.assertEqual(env.codex.check_calls, [])
        # clean workspace restored: cleanup + prepare ran again
        self.assertEqual(len(env.codex.prepare_calls), 2)
        self.assertEqual(len(env.codex.cleanup_calls), 3)  # enter, restore, exit
        # builder ran without the acceptance block
        builder = env.codex.run_exec_calls[-1]
        self.assertNotIn("Locked acceptance", builder["prompt"])
        self.assertNotIn("acceptance_authored", env.stages())
        self.assertNotIn("check_gate_passed", env.stages())

    def _builder_files(self, env: _Env) -> bytes:
        return env.patch({"app.py": "TOTAL = 5\n"}, with_acceptance=False)

    def test_cannot_check(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            message = json.dumps(
                {
                    "status": "cannot_check",
                    "checks": [],
                    "unchecked": [{"criterion": 1, "reason": "needs a socket"}],
                }
            )
            env.codex.queue_exec_result(_exec_result(final_message=message))
            env.queue_builder(self._builder_files(env))
            payload = env.run()
            self._assert_skipped_and_flow_continues(env, payload)
            # author tokens are still reported
            self.assertEqual(payload["check_gate"]["author_input_tokens"], 100)
            self.assertEqual(payload["input_tokens"], 200)
            self.assertEqual(len(env.codex.package_calls), 1)  # the builder's only

    def test_author_wrote_outside_the_acceptance_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author(patch_bytes=env.patch({"app.py": "evil\n"}))
            env.queue_builder(self._builder_files(env))
            self._assert_skipped_and_flow_continues(env, env.run())

    def test_author_wrote_a_helper_file(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author(patch_bytes=env.patch({"tests/acceptance/helpers.py": "x = 1\n"}))
            env.queue_builder(self._builder_files(env))
            self._assert_skipped_and_flow_continues(env, env.run())

    def test_author_made_no_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.codex.queue_exec_result(_exec_result(final_message=AUTHOR_OK))
            env.codex.queue_package_result({"patch_b64": ""})
            env.queue_builder(self._builder_files(env))
            self._assert_skipped_and_flow_continues(env, env.run())

    def test_author_wrote_no_tests(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author(patch_bytes=env.patch({TASK_PATH: "import unittest\n"}))
            env.queue_builder(self._builder_files(env))
            self._assert_skipped_and_flow_continues(env, env.run())

    def test_preflight_changes_the_author_patch(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            reformatted = env.patch({TASK_PATH: TEST_SRC.replace("'", '"')})
            env.codex.queue_preflight_result({"ok": True, "patch_b64": _b64(reformatted)})
            env.queue_builder(self._builder_files(env))
            self._assert_skipped_and_flow_continues(env, env.run())
            # The first preflight call was the author patch, in the shared request shape.
            first = env.codex.preflight_calls[0]
            self.assertEqual(base64.b64decode(first["patch_b64"]), env.author_patch)
            self.assertEqual(first["base_sha"], env.base_sha)
            self.assertEqual(first["tools"], {"ruff": "/opt/agent-svc/tools/ruff-0.7.4/bin/ruff"})

    def test_preflight_refuses_the_author_patch(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            env.codex.queue_preflight_result(
                CodexChildError(3, "preflight failed", body={"preflight_failure": "E501"})
            )
            env.queue_builder(self._builder_files(env))
            self._assert_skipped_and_flow_continues(env, env.run())

    def test_author_exec_failures(self) -> None:
        results = {
            "timeout": _exec_result(timed_out=True, exit_code=1),
            "nonzero": _exec_result(exit_code=2),
            "idle": _exec_result(idle_killed=True),
            "garbage": _exec_result(final_message="not json at all"),
            "wrong status": _exec_result(final_message='{"status": "maybe"}'),
        }
        for name, result in results.items():
            with self.subTest(name), TemporaryDirectory() as tmp:
                env = _Env(Path(tmp))
                env.codex.queue_exec_result(result)
                env.queue_builder(self._builder_files(env))
                self._assert_skipped_and_flow_continues(env, env.run())

    def test_author_json_embedded_in_prose_is_accepted(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author(message="Here you go:\n" + AUTHOR_OK + "\n")
            env.queue_builder(env.patch({"app.py": "TOTAL = 5\n"}))
            self.assertEqual(env.run()["check_gate"]["status"], "passed")

    def test_author_package_error(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.codex.queue_exec_result(_exec_result(final_message=AUTHOR_OK))
            env.codex.package_error = CodexChildError(3, "boom")
            # package_error is global: only the author package is attempted
            # before the builder's own, so clear it after the author ran.
            original = env.codex.package

            def package(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
                try:
                    return original(request, **kwargs)
                finally:
                    env.codex.package_error = None

            env.codex.package = package  # type: ignore[method-assign]
            env.queue_builder(self._builder_files(env))
            self._assert_skipped_and_flow_continues(env, env.run())

    def test_restore_failure_fails_the_run(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.codex.queue_exec_result(_exec_result(final_message='{"status": "cannot_check"}'))
            original = env.codex.prepare
            calls = {"n": 0}

            def prepare(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
                calls["n"] += 1
                if calls["n"] == 2:
                    raise CodexChildError(3, "prepare broke")
                return original(request, **kwargs)

            env.codex.prepare = prepare  # type: ignore[method-assign]
            payload = env.run()
            self.assertEqual(payload["status"], "failed")
            self.assertIn("workspace preparation failed", payload["error"])
            self.assertEqual(payload["failure_phase"], "implement")
            self.assertEqual(len(env.codex.run_exec_calls), 1)  # builder never ran
            self.assertEqual(payload["check_gate"]["status"], "skipped")

    def test_insufficient_budget_skips_the_author(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_builder(self._builder_files(env))
            clock = _Clock()
            original = env.codex.prepare

            def prepare(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
                if not env.codex.prepare_calls:  # the run's first workspace
                    clock.t = implement.RUN_BUDGET_S - 100  # < 900s left
                return original(request, **kwargs)

            env.codex.prepare = prepare  # type: ignore[method-assign]
            with clock.patched():
                payload = env.run()
            self.assertEqual(payload["status"], "pr_opened")
            self.assertEqual(payload["check_gate"]["status"], "skipped")
            self.assertEqual(len(env.codex.run_exec_calls), 1)
            self.assertEqual(payload["check_gate"]["author_input_tokens"], 0)
            self.assertIsNone(payload["check_gate"]["author_model"])


class _Clock:
    """A controllable replacement for `implement._monotonic`."""

    def __init__(self) -> None:
        self.t = 0.0

    @contextmanager
    def patched(self) -> Iterator[None]:
        with patch.object(implement, "_monotonic", lambda: self.t):
            yield


class SpliceTests(unittest.TestCase):
    def test_builder_edit_of_the_locked_test_is_discarded_and_flagged(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            cheated = TEST_SRC.replace("'Topilmadi', 'Topilmadi'", "1, 1")
            env.queue_builder(env.patch({"app.py": "TOTAL = 5\n", TASK_PATH: cheated}))
            payload = env.run()
            self.assertEqual(payload["status"], "pr_opened")
            self.assertTrue(payload["check_gate"]["acceptance_modified"])
            self.assertEqual(env.pushed(TASK_PATH), TEST_SRC)
            # The check ran against the builder patch WITHOUT acceptance files.
            sent = base64.b64decode(env.codex.check_calls[0]["patch_b64"])
            self.assertNotIn(b"tests/acceptance", sent)
            self.assertEqual(
                base64.b64decode(env.codex.check_calls[0]["pinned_b64"]), env.author_patch
            )

    def test_builder_deleting_the_test_is_restored_and_flagged(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            env.queue_builder(env.patch({"app.py": "TOTAL = 5\n"}, with_acceptance=False))
            payload = env.run()
            self.assertTrue(payload["check_gate"]["acceptance_modified"])
            self.assertEqual(env.pushed(TASK_PATH), TEST_SRC)

    def test_builder_adding_extra_acceptance_files_drops_them(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            env.queue_builder(
                env.patch({"app.py": "TOTAL = 5\n", "tests/acceptance/helper.py": "x = 1\n"})
            )
            payload = env.run()
            self.assertTrue(payload["check_gate"]["acceptance_modified"])
            result = run_git(
                ["git", "-C", str(env.remote), "ls-tree", "-r", "--name-only", f"refs/heads/{BRANCH}"]
            ).stdout
            self.assertNotIn("helper.py", result)
            self.assertIn("test_task_7.py", result)


class GateOutcomeTests(unittest.TestCase):
    def _env(self, tmp: str) -> _Env:
        env = _Env(Path(tmp))
        env.queue_author()
        env.queue_builder(env.patch({"app.py": "TOTAL = 4\n"}))
        return env

    def test_fail_then_fixup_then_pass(self) -> None:
        with TemporaryDirectory() as tmp:
            env = self._env(tmp)
            env.codex.queue_check_result(_failed(["test_ac1_header"], tail="AssertionError: 4 != 5"))
            env.queue_fixup(env.patch({"app.py": "TOTAL = 5\n"}))
            payload = env.run()

            self.assertEqual(payload["status"], "pr_opened")
            gate = payload["check_gate"]
            self.assertEqual(gate["status"], "passed")
            self.assertEqual(gate["fixups"], 1)
            self.assertEqual((gate["total"], gate["passed"]), (2, 2))
            self.assertEqual(gate["fixup_input_tokens"], 100)
            self.assertEqual(gate["fixup_output_tokens"], 20)
            self.assertEqual(gate["fixup_model"], "gpt-6-luna")
            self.assertEqual(payload["input_tokens"], 300)  # author + builder + fixup
            self.assertEqual(len(env.codex.check_calls), 2)
            self.assertEqual(env.pushed("app.py"), "TOTAL = 5\n")

            fixup = env.codex.run_exec_calls[2]
            self.assertEqual(fixup["cwd"], "wt")
            self.assertEqual(fixup["model"], "gpt-6-luna")
            self.assertFalse(fixup["multi_agent"])
            self.assertEqual(fixup["timeout_s"], 900)
            self.assertIsNone(fixup["output_schema"])
            self.assertIn("test_ac1_header", fixup["prompt"])
            self.assertIn("AssertionError: 4 != 5", fixup["prompt"])
            self.assertIn("Do NOT edit", fixup["prompt"])
            self.assertIn("Add a total header `X-Total: 5`", fixup["prompt"])
            self.assertNotIn("check_gate_failed", env.stages())
            self.assertIn("check_gate_passed", env.stages())

    def test_two_fixups_still_failing_opens_the_pr_with_failed_status(self) -> None:
        with TemporaryDirectory() as tmp:
            env = self._env(tmp)
            for _ in range(3):
                env.codex.queue_check_result(_failed(["test_ac1_header"]))
            env.queue_fixup(env.patch({"app.py": "TOTAL = 3\n"}))
            env.queue_fixup(env.patch({"app.py": "TOTAL = 2\n"}))
            payload = env.run()

            self.assertEqual(payload["status"], "pr_opened")
            gate = payload["check_gate"]
            self.assertEqual(gate["status"], "failed")
            self.assertEqual(gate["fixups"], 2)
            self.assertEqual(gate["failed"], ["test_ac1_header"])
            self.assertEqual((gate["total"], gate["passed"]), (2, 1))
            self.assertEqual(len(env.codex.check_calls), 3)
            self.assertEqual(len(env.codex.run_exec_calls), 4)
            self.assertIn("check_gate_failed", env.stages())
            self.assertNotIn("check_gate_passed", env.stages())
            self.assertEqual(env.pushed("app.py"), "TOTAL = 2\n")
            self.assertEqual(payload["input_tokens"], 400)

    def test_max_fixups_config_is_honoured(self) -> None:
        for limit, expected_execs in ((0, 2), (1, 3)):
            with self.subTest(limit), TemporaryDirectory() as tmp:
                env = _Env(Path(tmp), max_fixups=limit)
                env.queue_author()
                env.queue_builder(env.patch({"app.py": "TOTAL = 4\n"}))
                for _ in range(3):
                    env.codex.queue_check_result(_failed(["test_ac1_header"]))
                env.queue_fixup(env.patch({"app.py": "TOTAL = 3\n"}))
                payload = env.run()
                self.assertEqual(len(env.codex.run_exec_calls), expected_execs)
                self.assertEqual(payload["check_gate"]["fixups"], limit)
                self.assertEqual(payload["check_gate"]["status"], "failed")

    def test_exhausted_budget_skips_fixups(self) -> None:
        with TemporaryDirectory() as tmp:
            env = self._env(tmp)
            clock = _Clock()
            original = env.codex.check

            def check(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
                clock.t = implement.RUN_BUDGET_S - (implement.FIXUP_MIN_REMAINING_S - 1)
                return original(request, **kwargs)

            env.codex.check = check  # type: ignore[method-assign]
            env.codex.queue_check_result(_failed(["test_ac1_header"]))
            with clock.patched():
                payload = env.run()
            self.assertEqual(payload["status"], "pr_opened")
            self.assertEqual(payload["check_gate"]["status"], "failed")
            self.assertEqual(payload["check_gate"]["fixups"], 0)
            self.assertEqual(len(env.codex.run_exec_calls), 2)  # author + builder only
            self.assertIn("check_gate_failed", env.stages())

    def test_exec_timeouts_follow_the_remaining_budget(self) -> None:
        clock = _Clock()
        with clock.patched():
            clock.t = 0.0
            self.assertEqual(implement._exec_timeout(1200, 3300.0), 1200)
            clock.t = 2500.0  # 800s left - 240s reserve
            self.assertEqual(implement._exec_timeout(1200, 3300.0), 560.0)
            clock.t = 3299.0
            self.assertEqual(implement._exec_timeout(1200, 3300.0), 30.0)

    def test_gate_error_means_no_fixups(self) -> None:
        for name, reply in (
            ("error", {"status": "error", "total": 0, "passed": 0, "failed": [], "detail": "0 tests"}),
            ("unavailable", {"status": "unavailable", "detail": "canary"}),
            ("raises", CodexChildError(3, "refused")),
            ("raises-other", RuntimeError("boom")),
            ("garbage", {"status": "weird"}),
        ):
            with self.subTest(name), TemporaryDirectory() as tmp:
                env = self._env(tmp)
                env.codex.queue_check_result(reply)
                payload = env.run()
                self.assertEqual(payload["status"], "pr_opened")
                expected = reply["status"] if name == "unavailable" else "error"
                self.assertEqual(payload["check_gate"]["status"], expected)
                self.assertEqual(payload["check_gate"]["fixups"], 0)
                self.assertEqual(len(env.codex.run_exec_calls), 2)
                self.assertEqual(len(env.codex.check_calls), 1)
                self.assertNotIn("check_gate_failed", env.stages())
                self.assertNotIn("check_gate_passed", env.stages())

    def test_failed_ids_are_capped_at_50(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp), max_fixups=0)
            env.queue_author()
            env.queue_builder(env.patch({"app.py": "TOTAL = 4\n"}))
            env.codex.queue_check_result(_failed([f"test_{i}" for i in range(80)]))
            payload = env.run()
            self.assertEqual(len(payload["check_gate"]["failed"]), 50)

    def test_fixup_exec_failure_keeps_the_previous_patch(self) -> None:
        with TemporaryDirectory() as tmp:
            env = self._env(tmp)
            env.codex.queue_check_result(_failed(["test_ac1_header"]))
            env.codex.queue_exec_result(_exec_result(timed_out=True, exit_code=1))
            payload = env.run()
            self.assertEqual(payload["status"], "pr_opened")
            self.assertEqual(payload["check_gate"]["status"], "failed")
            self.assertEqual(payload["check_gate"]["fixups"], 1)
            self.assertEqual(len(env.codex.check_calls), 1)
            self.assertEqual(env.pushed("app.py"), "TOTAL = 4\n")

    def test_fixup_that_edits_the_locked_test_is_spliced_back(self) -> None:
        with TemporaryDirectory() as tmp:
            env = self._env(tmp)
            env.codex.queue_check_result(_failed(["test_ac1_header"]))
            env.queue_fixup(env.patch({"app.py": "TOTAL = 5\n", TASK_PATH: "# gutted\n"}))
            payload = env.run()
            self.assertTrue(payload["check_gate"]["acceptance_modified"])
            self.assertEqual(env.pushed(TASK_PATH), TEST_SRC)
            sent = base64.b64decode(env.codex.check_calls[1]["patch_b64"])
            self.assertNotIn(b"gutted", sent)

    def test_lost_lease_during_the_gate_sends_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            env = self._env(tmp)
            cancel = threading.Event()
            original = env.codex.check

            def check(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
                cancel.set()
                return original(request, **kwargs)

            env.codex.check = check  # type: ignore[method-assign]
            handle_implement(env.ctx, _qa_work(), cancel)
            self.assertEqual(env.ctx.api.callbacks, [])  # type: ignore[attr-defined]
            self.assertEqual(env.ctx.github.created_pulls, [])  # type: ignore[attr-defined]


class SpecConflictTests(unittest.TestCase):
    CONFLICT = 'AGENT_SPEC_CONFLICT: {"criterion": 2, "reason": "404 text conflicts with API"}'

    def test_builder_conflict_fails_without_publishing(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            env.codex.queue_exec_result(
                _exec_result(final_message=f"I stopped.\n{self.CONFLICT}")
            )
            payload = env.run()
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["failure_phase"], "implement")
            self.assertEqual(
                payload["error"], "SPEC_CONFLICT (mezon 2): 404 text conflicts with API"
            )
            self.assertEqual(payload["input_tokens"], 200)
            self.assertEqual(payload["check_gate"]["author_input_tokens"], 100)
            self.assertEqual(len(env.codex.package_calls), 1)  # only the author's
            self.assertEqual(env.codex.check_calls, [])
            self.assertEqual(env.ctx.github.created_pulls, [])  # type: ignore[attr-defined]
            self.assertIsNone(
                env.ctx.github.get_ref("Asadtop4ik/agent-qa", BRANCH)  # type: ignore[attr-defined]
            )

    def test_conflict_without_the_factory_still_fails_the_run(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp), acceptance_checks_enabled=False)
            env.codex.queue_exec_result(
                _exec_result(final_message=f"Cannot do it.\n{self.CONFLICT}")
            )
            payload = env.run()
            self.assertEqual(payload["status"], "failed")
            self.assertTrue(payload["error"].startswith("SPEC_CONFLICT (mezon 2):"))
            self.assertNotIn("check_gate", payload)
            self.assertEqual(env.codex.package_calls, [])

    def test_conflict_with_null_criterion_and_secret_is_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp), acceptance_checks_enabled=False)
            secret = "svc-token"  # one of the test secrets (see support._SECRETS)
            line = (
                'AGENT_SPEC_CONFLICT: {"criterion": null, "reason": "uses ' + secret + '"}'
            )
            env.codex.queue_exec_result(_exec_result(final_message=line))
            payload = env.run()
            self.assertNotIn(secret, payload["error"])
            self.assertIn("SPEC_CONFLICT (mezon ?)", payload["error"])

    def test_conflict_in_a_fixup_fails_the_run(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            env.queue_builder(env.patch({"app.py": "TOTAL = 4\n"}))
            env.codex.queue_check_result(_failed(["test_ac1_header"]))
            env.queue_fixup(None, message=self.CONFLICT)
            payload = env.run()
            self.assertEqual(payload["status"], "failed")
            self.assertTrue(payload["error"].startswith("SPEC_CONFLICT (mezon 2)"))
            self.assertEqual(payload["check_gate"]["fixups"], 1)
            self.assertEqual(payload["check_gate"]["status"], "failed")
            self.assertEqual(payload["input_tokens"], 300)
            self.assertEqual(env.ctx.github.created_pulls, [])  # type: ignore[attr-defined]

    def test_lookalike_marker_never_reaches_the_pr_body(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            message = (
                "Implemented it.\n"
                '  AGENT_SPEC_CONFLICT: {"criterion": 1, "reason": "SECRET-REASON"}\n'
                "Checks run: none."
            )
            env.queue_builder(env.patch({"app.py": "TOTAL = 5\n"}), message=message)
            payload = env.run()
            self.assertEqual(payload["status"], "pr_opened")
            body = env.pr_body()
            self.assertNotIn("AGENT_SPEC_CONFLICT", body)
            self.assertNotIn("SECRET-REASON", body)
            self.assertIn("Implemented it.", body)

    def test_marker_and_ops_trailer_coexist_without_leaking(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp), acceptance_checks_enabled=False)
            message = (
                'Done.\nAGENT_SPEC_CONFLICT: {"criterion": 1, "reason": "x"}\n'
                'AGENT_OPS_REQUESTS: [{"kind":"env_set"}]'
            )
            env.codex.queue_exec_result(_exec_result(final_message=message))
            payload = env.run()
            self.assertEqual(payload["status"], "failed")
            self.assertNotIn("AGENT_OPS_REQUESTS", payload["error"])


class OwnerNoteTests(unittest.TestCase):
    def test_owner_note_reaches_the_builder_prompt(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp), acceptance_checks_enabled=False)
            env.queue_builder(env.patch({"NOTES.md": "hi\n"}, with_acceptance=False))
            env.run(_qa_work(owner_note="Owner approved the spec: follow it literally."))
            prompt = env.codex.run_exec_calls[0]["prompt"]
            self.assertIn("Owner instruction", prompt)
            self.assertIn("Owner approved the spec: follow it literally.", prompt)


class CallbackFallbackTests(unittest.TestCase):
    def _run_with_rejections(self, *statuses: int) -> tuple[_Env, dict[str, Any]]:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = _Env(Path(tmp.name))
        env.queue_author()
        env.queue_builder(env.patch({"app.py": "TOTAL = 5\n"}))
        env.ctx.api.queue_callback_effects(  # type: ignore[attr-defined]
            *[HttpError(status, "unprocessable") for status in statuses]
        )
        return env, env.run()

    def test_422_resends_without_check_gate_but_keeps_ops_fields(self) -> None:
        _env, payload = self._run_with_rejections(422)
        self.assertEqual(payload["status"], "pr_opened")
        self.assertNotIn("check_gate", payload)
        self.assertIn("ops_requests", payload)
        self.assertIn("ops_note", payload)
        self.assertEqual(payload["input_tokens"], 200)

    def test_400_is_handled_like_422(self) -> None:
        _env, payload = self._run_with_rejections(400)
        self.assertNotIn("check_gate", payload)

    def test_second_rejection_strips_ops_fields_too(self) -> None:
        _env, payload = self._run_with_rejections(422, 422)
        self.assertEqual(payload["status"], "pr_opened")
        self.assertNotIn("check_gate", payload)
        self.assertNotIn("ops_requests", payload)
        self.assertNotIn("ops_note", payload)

    def test_failed_callback_with_gate_is_also_resent_without_it(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            env.queue_builder(env.author_patch)  # no changes outside tests -> failed
            env.ctx.api.queue_callback_effects(HttpError(422, "bad"))  # type: ignore[attr-defined]
            payload = env.run()
            self.assertEqual(payload["status"], "failed")
            self.assertNotIn("check_gate", payload)

    def test_other_statuses_are_not_retried_by_the_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            env = _Env(Path(tmp))
            env.queue_author()
            env.queue_builder(env.patch({"app.py": "TOTAL = 5\n"}))
            env.ctx.api.queue_callback_effects(HttpError(500, "boom"))  # type: ignore[attr-defined]
            # run.deliver's own retry delivers the second attempt unchanged.
            with patch("agent_svc.runctx.time.sleep"):
                payload = env.run()
            self.assertIn("check_gate", payload)


if __name__ == "__main__":
    unittest.main()
