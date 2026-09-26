from __future__ import annotations

import io
import unittest
from tempfile import TemporaryDirectory

from agent_svc.http import HttpError
from agent_svc.journal import Journal, JournalEntry
from agent_svc.log import Logger, Redactor
from agent_svc.recovery import recover

RUN_A = "11111111-1111-1111-1111-111111111111"
RUN_B = "22222222-2222-2222-2222-222222222222"


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


if __name__ == "__main__":
    unittest.main()
