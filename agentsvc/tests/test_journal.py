from __future__ import annotations

import json
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_svc.journal import Journal, JournalEntry

RUN_ID = "11111111-1111-1111-1111-111111111111"


class FakeLogger:
    def __init__(self) -> None:
        self.errors: list[tuple[BaseException, dict[str, object]]] = []

    def error(self, exc: BaseException, **fields: object) -> None:
        self.errors.append((exc, fields))


def _entry(**overrides: object) -> JournalEntry:
    base = dict(
        run_id=RUN_ID,
        kind="implement",
        lease_id="lease-1",
        stage="workspace_ready",
        base_sha="a" * 40,
        branch="codex/task-1-" + RUN_ID,
        started_at="2026-09-26T10:00:00+00:00",
        updated_at="2026-09-26T10:01:00+00:00",
    )
    base.update(overrides)
    return JournalEntry(**base)  # type: ignore[arg-type]


class JournalRoundTripTests(unittest.TestCase):
    def test_write_then_read_round_trips(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            entry = _entry()
            journal.write(entry)
            loaded = journal.read(RUN_ID)
            self.assertEqual(loaded, entry)

    def test_write_is_group_owner_only_readable(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry())
            path = Path(tmp) / f"{RUN_ID}.json"
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_write_uses_a_temp_file_and_atomic_replace(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry())
            leftover_temp_files = list(Path(tmp).glob(".tmp-*"))
            self.assertEqual(leftover_temp_files, [])

    def test_read_missing_returns_none(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            self.assertIsNone(journal.read(RUN_ID))

    def test_list_returns_all_entries_sorted(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            other_id = "22222222-2222-2222-2222-222222222222"
            journal.write(_entry())
            journal.write(_entry(run_id=other_id))
            entries = journal.list()
            self.assertEqual({entry.run_id for entry in entries}, {RUN_ID, other_id})

    def test_remove_deletes_file_and_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            journal.write(_entry())
            journal.remove(RUN_ID)
            self.assertIsNone(journal.read(RUN_ID))
            journal.remove(RUN_ID)  # no error on a second remove

    def test_invalid_run_id_rejected_before_any_write(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            with self.assertRaises(ValueError):
                journal.write(_entry(run_id="not-a-run-id"))


class JournalCorruptionTests(unittest.TestCase):
    def test_corrupt_json_is_quarantined_and_logged(self) -> None:
        with TemporaryDirectory() as tmp:
            logger = FakeLogger()
            journal = Journal(tmp, logger=logger)
            path = Path(tmp) / f"{RUN_ID}.json"
            path.write_text("{not valid json")
            result = journal.read(RUN_ID)
            self.assertIsNone(result)
            self.assertFalse(path.exists())
            corrupt_files = list((Path(tmp) / "corrupt").glob(f"{RUN_ID}-*.json"))
            self.assertEqual(len(corrupt_files), 1)
            self.assertEqual(len(logger.errors), 1)
            self.assertEqual(logger.errors[0][1]["run_id"], RUN_ID)

    def test_schema_invalid_entry_is_quarantined(self) -> None:
        with TemporaryDirectory() as tmp:
            logger = FakeLogger()
            journal = Journal(tmp, logger=logger)
            path = Path(tmp) / f"{RUN_ID}.json"
            path.write_text(json.dumps({"run_id": RUN_ID, "kind": "implement"}))
            self.assertIsNone(journal.read(RUN_ID))
            self.assertEqual(len(logger.errors), 1)

    def test_list_skips_corrupt_entries_but_returns_valid_ones(self) -> None:
        with TemporaryDirectory() as tmp:
            journal = Journal(tmp, logger=FakeLogger())
            journal.write(_entry())
            bad_id = "33333333-3333-3333-3333-333333333333"
            (Path(tmp) / f"{bad_id}.json").write_text("garbage")
            entries = journal.list()
            self.assertEqual([entry.run_id for entry in entries], [RUN_ID])


if __name__ == "__main__":
    unittest.main()
