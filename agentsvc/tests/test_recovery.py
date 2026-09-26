from __future__ import annotations

import http.client
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_svc.http import HttpError
from agent_svc.journal import Journal, JournalEntry
from agent_svc.log import Logger, Redactor
from agent_svc.recovery import recover

RUN_A = "11111111-1111-1111-1111-111111111111"
RUN_B = "22222222-2222-2222-2222-222222222222"


class FakeCodex:
    def __init__(self) -> None:
        self.cleanup_calls: list[str] = []

    def cleanup(self, request: dict) -> dict:
        self.cleanup_calls.append(request["run_id"])
        return {"ok": True}


def _entry(
    run_id: str, *, lease_id: str = "lease-1", stage: str = "workspace_ready"
) -> JournalEntry:
    return JournalEntry(
        run_id=run_id,
        kind="implement",
        lease_id=lease_id,
        stage=stage,
        base_sha="a" * 40,
        branch=None,
        started_at="2026-09-26T10:00:00+00:00",
        updated_at="2026-09-26T10:00:00+00:00",
    )


def _logger() -> Logger:
    return Logger(Redactor([]), stream=io.StringIO())


class FakeApi:
    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses

    def status(self, run_id: str) -> object:
        result = self._responses[run_id]
        if isinstance(result, BaseException):
            raise result
        return result


class RecoveryTests(unittest.TestCase):
    def test_terminal_status_is_cleaned_up(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry(RUN_A))
            api = FakeApi({RUN_A: {"status": "deployed"}})
            resume = recover(journal, api, _logger())
            self.assertEqual(resume, [])
            self.assertIsNone(journal.read(RUN_A))

    def test_non_terminal_status_with_matching_lease_is_kept_for_resume(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry(RUN_A, lease_id="lease-1"))
            api = FakeApi({RUN_A: {"status": "pr_opened", "lease_id": "lease-1"}})
            resume = recover(journal, api, _logger())
            self.assertEqual([entry.run_id for entry in resume], [RUN_A])
            self.assertIsNotNone(journal.read(RUN_A))

    def test_mismatched_lease_is_cleaned_up(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry(RUN_A, lease_id="lease-1"))
            api = FakeApi({RUN_A: {"status": "pr_opened", "lease_id": "someone-elses-lease"}})
            resume = recover(journal, api, _logger())
            self.assertEqual(resume, [])
            self.assertIsNone(journal.read(RUN_A))

    def test_status_without_lease_field_falls_back_to_terminal_check_only(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry(RUN_A))
            api = FakeApi({RUN_A: {"status": "pr_opened"}})
            resume = recover(journal, api, _logger())
            self.assertEqual([entry.run_id for entry in resume], [RUN_A])

    def test_invalid_status_payload_is_dropped_but_journal_entry_kept(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry(RUN_A))
            api = FakeApi({RUN_A: "not-a-dict"})
            resume = recover(journal, api, _logger())
            self.assertEqual(resume, [])
            self.assertIsNotNone(journal.read(RUN_A))

    def test_one_bad_entry_does_not_block_the_others(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry(RUN_A))
            journal.write(_entry(RUN_B))
            api = FakeApi(
                {
                    RUN_A: HttpError(500, "boom"),
                    RUN_B: {"status": "pr_opened", "lease_id": "lease-1"},
                }
            )
            resume = recover(journal, api, _logger())
            self.assertEqual([entry.run_id for entry in resume], [RUN_B])
            self.assertIsNotNone(journal.read(RUN_A))  # left alone for the next attempt

    def test_non_http_errors_are_caught_and_recovery_continues(self) -> None:
        # `api.status` can fail with more than `HttpError`: a socket timeout,
        # a dropped connection, or a malformed JSON body all reach here as
        # different exception types. None of them may crash startup recovery.
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry(RUN_A))
            journal.write(_entry(RUN_B))
            other_ids = [
                "33333333-3333-3333-3333-333333333333",
                "44444444-4444-4444-4444-444444444444",
            ]
            for run_id in other_ids:
                journal.write(_entry(run_id))
            api = FakeApi(
                {
                    RUN_A: TimeoutError("timed out"),
                    RUN_B: json.JSONDecodeError("bad json", "{", 0),
                    other_ids[0]: http.client.RemoteDisconnected("connection closed"),
                    other_ids[1]: {"status": "pr_opened", "lease_id": "lease-1"},
                }
            )
            resume = recover(journal, api, _logger())
            self.assertEqual([entry.run_id for entry in resume], [other_ids[1]])
            for run_id in (RUN_A, RUN_B, other_ids[0]):
                self.assertIsNotNone(journal.read(run_id))  # left alone, not crashed


class RecoveryCleansUpLeftoversTests(unittest.TestCase):
    def test_cleans_up_codex_and_publish_dir_for_a_terminal_entry(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry(RUN_A))
            api = FakeApi({RUN_A: {"status": "deployed"}})
            codex = FakeCodex()
            state_dir = Path(tmp) / "state"
            publish_dir = state_dir / "publish" / RUN_A
            publish_dir.mkdir(parents=True)
            (publish_dir / "leftover.txt").write_text("x", encoding="utf-8")

            recover(journal, api, _logger(), codex=codex, state_dir=str(state_dir))

            self.assertEqual(codex.cleanup_calls, [RUN_A])
            self.assertFalse(publish_dir.exists())

    def test_cleans_up_codex_and_publish_dir_for_a_resumed_entry_too(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry(RUN_A, lease_id="lease-1"))
            api = FakeApi({RUN_A: {"status": "pr_opened", "lease_id": "lease-1"}})
            codex = FakeCodex()
            state_dir = Path(tmp) / "state"
            publish_dir = state_dir / "publish" / RUN_A
            publish_dir.mkdir(parents=True)

            resume = recover(journal, api, _logger(), codex=codex, state_dir=str(state_dir))

            self.assertEqual([entry.run_id for entry in resume], [RUN_A])
            self.assertEqual(codex.cleanup_calls, [RUN_A])
            self.assertFalse(publish_dir.exists())

    def test_status_failure_never_touches_the_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry(RUN_A))
            api = FakeApi({RUN_A: TimeoutError("timed out")})
            codex = FakeCodex()
            state_dir = Path(tmp) / "state"
            publish_dir = state_dir / "publish" / RUN_A
            publish_dir.mkdir(parents=True)

            recover(journal, api, _logger(), codex=codex, state_dir=str(state_dir))

            self.assertEqual(codex.cleanup_calls, [])
            self.assertTrue(publish_dir.exists())

    def test_a_codex_cleanup_exception_is_logged_and_never_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry(RUN_A))
            api = FakeApi({RUN_A: {"status": "deployed"}})

            class BoomCodex:
                def cleanup(self, request: dict) -> dict:
                    raise RuntimeError("sudo unavailable")

            recover(journal, api, _logger(), codex=BoomCodex(), state_dir=tmp)  # must not raise

    def test_without_codex_or_state_dir_recovery_behaves_exactly_as_before(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry(RUN_A))
            api = FakeApi({RUN_A: {"status": "deployed"}})
            resume = recover(journal, api, _logger())  # no codex=, no state_dir=
            self.assertEqual(resume, [])


if __name__ == "__main__":
    unittest.main()
