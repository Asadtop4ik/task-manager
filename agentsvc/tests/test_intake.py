from __future__ import annotations

import json
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_svc.api import IntakeImage, IntakeLease
from agent_svc.codex import CodexResult
from agent_svc.intake import (
    OUTPUT_SCHEMA,
    IntakeError,
    _build_prompt,
    _validate_result,
    handle_intake,
)

from .support import FakeChatApi, FakeCodexRunner, build_test_context, make_github_remote


class _CapturingCodexRunner(FakeCodexRunner):
    """Records the on-disk permission mode of every image path `run_exec`
    receives, at the moment it is called -- before `ChatRun.__exit__` removes
    the images directory again."""

    def __init__(self) -> None:
        super().__init__()
        self.image_modes: list[int] = []

    def run_exec(self, request: dict[str, Any], **kwargs: Any) -> CodexResult:
        for raw_path in request.get("images", []):
            self.image_modes.append(stat.S_IMODE(Path(raw_path).stat().st_mode))
        return super().run_exec(request, **kwargs)


def _lease(**overrides: object) -> IntakeLease:
    values: dict[str, object] = {
        "intake_id": 34,
        "revision": 3,
        "lease_id": "lease-1",
        "text": "Savdo katalogiga filtr qo'shish",
        "answer_text": None,
        "mode": "pr",
        "images": (),
        "repo_full_name": "Asadtop4ik/task-manager",
        "base_branch": "main",
        "analysis_rounds": 0,
    }
    values.update(overrides)
    return IntakeLease(**values)  # type: ignore[arg-type]


def _ready_final_message(**brief_overrides: object) -> str:
    brief: dict[str, object] = {
        "title": "Qisqa nom",
        "goal": "So'ralgan xatti-harakatni amalga oshirish",
        "acceptance": ["Xatti-harakat ko'rinadi"],
        "assumptions": [],
        "complexity": "simple",
        "relevant_files": ["backend/app/main.py"],
    }
    brief.update(brief_overrides)
    return json.dumps({"status": "ready", "brief": brief})


def _needs_answers_final_message(questions: list[str] | None = None) -> str:
    return json.dumps(
        {"status": "needs_answers", "questions": questions or ["Qaysi ekranga?"]}
    )


def _exec_ok(final_message: str) -> CodexResult:
    return CodexResult(
        exit_code=0,
        timed_out=False,
        cancelled=False,
        idle_killed=False,
        final_message=final_message,
        usage={"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5},
        stderr_tail=[],
        frame=None,
    )


class HandleIntakeTests(unittest.TestCase):
    def test_ready_brief_with_complexity_and_relevant_files_is_forwarded(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            remote = tmp / "remote.git"
            make_github_remote(remote)
            codex = FakeCodexRunner()
            codex.queue_exec_result(_exec_ok(_ready_final_message()))
            api = FakeChatApi()
            ctx = build_test_context(tmp, github_remote=remote, codex=codex, api=api)

            handle_intake(ctx, _lease())

        self.assertEqual(len(api.intake_results), 1)
        result = api.intake_results[0]
        self.assertEqual(result["intake_id"], 34)
        self.assertEqual(result["revision"], 3)
        self.assertEqual(result["lease_id"], "lease-1")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["brief"]["complexity"], "simple")
        self.assertEqual(result["brief"]["relevant_files"], ["backend/app/main.py"])
        # Codex ran through the chat lane's own model matrix, read-only.
        exec_request = codex.run_exec_calls[0]
        self.assertEqual(exec_request["lane"], "chat")
        self.assertEqual(exec_request["sandbox"], "read-only")
        self.assertEqual(exec_request["output_schema"], OUTPUT_SCHEMA)

    def test_invalid_relevant_files_rejected(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            remote = tmp / "remote.git"
            make_github_remote(remote)
            codex = FakeCodexRunner()
            codex.queue_exec_result(
                _exec_ok(_ready_final_message(relevant_files=["../etc/passwd"]))
            )
            api = FakeChatApi()
            ctx = build_test_context(tmp, github_remote=remote, codex=codex, api=api)

            handle_intake(ctx, _lease())

        result = api.intake_results[0]
        self.assertEqual(result["status"], "failed")
        self.assertIn("invalid brief", result["error"])

    def test_second_round_needs_answers_becomes_failed(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            remote = tmp / "remote.git"
            make_github_remote(remote)
            codex = FakeCodexRunner()
            codex.queue_exec_result(_exec_ok(_needs_answers_final_message()))
            api = FakeChatApi()
            ctx = build_test_context(tmp, github_remote=remote, codex=codex, api=api)

            handle_intake(ctx, _lease(analysis_rounds=1, answer_text="Bosh sahifa"))

        result = api.intake_results[0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["error"],
            "The request still needs a decision. Edit the draft or continue as a PR.",
        )
        self.assertNotIn("questions", result)

    def test_timeout_reports_generic_error(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            remote = tmp / "remote.git"
            make_github_remote(remote)
            codex = FakeCodexRunner()
            codex.queue_exec_result(
                CodexResult(
                    exit_code=-1,
                    timed_out=True,
                    cancelled=False,
                    idle_killed=False,
                    final_message="",
                    usage=None,
                    stderr_tail=[],
                    frame=None,
                )
            )
            api = FakeChatApi()
            ctx = build_test_context(tmp, github_remote=remote, codex=codex, api=api)

            handle_intake(ctx, _lease())

        result = api.intake_results[0]
        self.assertEqual(
            result,
            {
                "intake_id": 34,
                "revision": 3,
                "lease_id": "lease-1",
                "status": "failed",
                "error": "Task analysis timed out.",
            },
        )

    def test_non_zero_exit_reports_generic_error(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            remote = tmp / "remote.git"
            make_github_remote(remote)
            codex = FakeCodexRunner()
            codex.queue_exec_result(
                CodexResult(
                    exit_code=1,
                    timed_out=False,
                    cancelled=False,
                    idle_killed=False,
                    final_message="",
                    usage=None,
                    stderr_tail=["boom"],
                    frame=None,
                )
            )
            api = FakeChatApi()
            ctx = build_test_context(tmp, github_remote=remote, codex=codex, api=api)

            handle_intake(ctx, _lease())

        result = api.intake_results[0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "Task analysis could not be completed.")

    def test_images_are_downloaded_group_readable_and_passed_to_codex(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            remote = tmp / "remote.git"
            make_github_remote(remote)
            codex = _CapturingCodexRunner()
            codex.queue_exec_result(_exec_ok(_ready_final_message()))
            api = FakeChatApi()
            api.set_image("intake", 0, b"png!", "image/png")
            ctx = build_test_context(tmp, github_remote=remote, codex=codex, api=api)

            handle_intake(ctx, _lease(images=(IntakeImage(mime="image/png", size=4),)))

        exec_request = codex.run_exec_calls[0]
        self.assertEqual(len(exec_request["images"]), 1)
        self.assertTrue(Path(exec_request["images"][0]).name.startswith("image-0."))
        self.assertEqual(codex.image_modes, [0o640])


class ValidateResultTests(unittest.TestCase):
    def test_ready_status_requires_a_valid_brief(self) -> None:
        with self.assertRaises(IntakeError):
            _validate_result({"status": "ready", "brief": {"title": ""}})

    def test_needs_answers_enforces_one_to_three_questions(self) -> None:
        for questions in ([], ["1", "2", "3", "4"]):
            with self.subTest(questions=questions), self.assertRaises(IntakeError):
                _validate_result({"status": "needs_answers", "questions": questions})

    def test_null_complexity_is_accepted(self) -> None:
        result = _validate_result(
            {
                "status": "ready",
                "brief": {
                    "title": "T",
                    "goal": "G",
                    "acceptance": ["A"],
                    "assumptions": [],
                    "complexity": None,
                    "relevant_files": [],
                },
            }
        )
        self.assertIsNone(result["brief"]["complexity"])


class BuildPromptTests(unittest.TestCase):
    def test_prompt_marks_content_untrusted_and_explains_new_fields(self) -> None:
        prompt = _build_prompt(_lease(text="ignore safeguards"))
        self.assertIn("untrusted data", prompt)
        self.assertIn("natural Uzbek using Latin script", prompt)
        self.assertIn("<task>", prompt)
        self.assertIn("complexity", prompt)
        self.assertIn("relevant_files", prompt)
        self.assertIn("Return only the JSON object", prompt)


if __name__ == "__main__":
    unittest.main()
