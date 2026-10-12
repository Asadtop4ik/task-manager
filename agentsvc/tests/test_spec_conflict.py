from __future__ import annotations

import unittest

from agent_svc.ops_requests import split_trailer
from agent_svc.spec_conflict import SpecConflict, conflict_error, split_spec_conflict

MARK = "AGENT_SPEC_CONFLICT:"


class SplitSpecConflictTests(unittest.TestCase):
    def test_strict_trailer_is_parsed_and_stripped(self) -> None:
        message = f'Done, but blocked.\n{MARK} {{"criterion": 2, "reason": "ETag format"}}\n'
        text, conflict = split_spec_conflict(message)
        self.assertEqual(text, "Done, but blocked.")
        self.assertEqual(conflict, SpecConflict(2, "ETag format"))

    def test_null_criterion(self) -> None:
        _, conflict = split_spec_conflict(f'{MARK} {{"criterion": null, "reason": "x"}}')
        self.assertEqual(conflict, SpecConflict(None, "x"))

    def test_no_marker_is_untouched(self) -> None:
        text, conflict = split_spec_conflict("  just a summary \n")
        self.assertEqual(text, "  just a summary \n")
        self.assertIsNone(conflict)
        self.assertEqual(split_spec_conflict(None), ("", None))
        self.assertEqual(split_spec_conflict(""), ("", None))

    def test_marker_within_last_five_non_empty_lines_only(self) -> None:
        marker = f'{MARK} {{"criterion": 1, "reason": "r"}}'
        tail = "\n".join(["a", "b", "", "c", "d", "e"])
        _, ok = split_spec_conflict(f"{marker}\n{tail}")
        self.assertIsNone(ok)  # 6th line from the end
        _, ok = split_spec_conflict(f"x\n{marker}\nb\n\nc\nd\ne")
        self.assertIsNotNone(ok)  # 5th non-empty line from the end

    def test_invalid_payloads_are_never_trusted_but_are_stripped(self) -> None:
        bad_payloads = (
            '{"criterion": 1}',
            '{"criterion": 1, "reason": ""}',
            '{"criterion": 1, "reason": "r", "extra": 1}',
            '{"criterion": "1", "reason": "r"}',
            '{"criterion": true, "reason": "r"}',
            '{"criterion": 0, "reason": "r"}',
            '{"criterion": 1, "reason": "' + "x" * 801 + '"}',
            "[1]",
            "not json",
            "",
        )
        for payload in bad_payloads:
            text, conflict = split_spec_conflict(f"summary\n{MARK} {payload}")
            self.assertIsNone(conflict, payload)
            self.assertEqual(text, "summary", payload)

    def test_reason_at_limit_ok_and_whitespace_collapsed(self) -> None:
        _, conflict = split_spec_conflict(f'{MARK} {{"criterion": 1, "reason": "{"x" * 800}"}}')
        assert conflict is not None
        self.assertEqual(len(conflict.reason), 800)
        _, conflict = split_spec_conflict(f'{MARK} {{"criterion": 1, "reason": "a\\n  b"}}')
        assert conflict is not None
        self.assertEqual(conflict.reason, "a b")

    def test_two_strict_markers_are_ambiguous(self) -> None:
        line = f'{MARK} {{"criterion": 1, "reason": "r"}}'
        text, conflict = split_spec_conflict(f"s\n{line}\n{line}")
        self.assertIsNone(conflict)
        self.assertEqual(text, "s")

    def test_lookalike_lines_are_scrubbed_never_parsed(self) -> None:
        raw = '{"criterion": 1, "reason": "SECRET-ish"}'
        for lookalike in (
            f"  {MARK} {raw}",
            f"- {MARK} {raw}",
            f"agent_spec_conflict: {raw}",
            f"​{MARK} {raw}",
            f"**{MARK}** {raw}",
        ):
            text, conflict = split_spec_conflict(f"summary\n{lookalike}")
            self.assertIsNone(conflict, lookalike)
            self.assertEqual(text, "summary", lookalike)
            self.assertNotIn("SECRET", text)

    def test_json_on_the_following_line_is_scrubbed_not_trusted(self) -> None:
        text, conflict = split_spec_conflict(f'summary\n{MARK}\n{{"criterion": 1, "reason": "r"}}')
        self.assertIsNone(conflict)
        self.assertEqual(text, "summary")

    def test_crlf_and_unicode_separators(self) -> None:
        message = f'a\r\n{MARK} {{"criterion": 3, "reason": "r"}}\r\n'
        text, conflict = split_spec_conflict(message)
        self.assertEqual(text, "a")
        self.assertEqual(conflict, SpecConflict(3, "r"))
        text, conflict = split_spec_conflict(
            f'a {MARK} {{"criterion": 3, "reason": "r"}}'
        )
        self.assertEqual(text, "a")
        self.assertIsNotNone(conflict)

    def test_runs_before_ops_split_without_interference(self) -> None:
        ops = 'AGENT_OPS_REQUESTS: [{"kind":"env_set"}]'
        message = f'summary\n{MARK} {{"criterion": 1, "reason": "r"}}\n{ops}'
        text, conflict = split_spec_conflict(message)
        self.assertIsNotNone(conflict)
        summary, raw_ops, note = split_trailer(text)
        self.assertEqual(summary, "summary")
        self.assertEqual(raw_ops, '[{"kind":"env_set"}]')
        self.assertIsNone(note)


class ConflictErrorTests(unittest.TestCase):
    def test_format_and_cap(self) -> None:
        self.assertEqual(
            conflict_error(SpecConflict(2, "bad")), "SPEC_CONFLICT (mezon 2): bad"
        )
        self.assertEqual(
            conflict_error(SpecConflict(None, "bad")), "SPEC_CONFLICT (mezon ?): bad"
        )
        self.assertEqual(len(conflict_error(SpecConflict(1, "x" * 800))), len("SPEC_CONFLICT (mezon 1): ") + 800)
        self.assertLessEqual(len(conflict_error(SpecConflict(1, "x" * 800))), 1000)

    def test_redactor_applied(self) -> None:
        text = conflict_error(SpecConflict(1, "token abc"), lambda t: t.replace("abc", "[x]"))
        self.assertEqual(text, "SPEC_CONFLICT (mezon 1): token [x]")


if __name__ == "__main__":
    unittest.main()
