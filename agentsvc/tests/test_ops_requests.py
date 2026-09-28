from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_svc.ops_requests import build_ops_note, split_trailer, validate
from agent_svc.trusted import TrustedModules

from .support import copy_trusted_dir

# The spec's own worked example (agent-svc-notes/ops-requests-spec.md
# section 3), for the one project ("qurbot") whose real catalog entry
# (backend/app/services/agent_repos.py) this allowlist is checked against.
ALLOWLIST_DATA = {
    "version": 1,
    "projects": {
        "qurbot": {
            "repo_full_name": "muradjanov-dev/qurbot",
            "stack": "qurbot",
            "env_file": "/srv/stack/env/qurbot.env",
            "services": ["qurbot-web", "qurbot-worker"],
            "containers": [
                ["qurbot-web", "ghcr.io/muradjanov-dev/qurbot"],
                ["qurbot-worker", "ghcr.io/muradjanov-dev/qurbot"],
            ],
            "ready_url": None,
            "keys": {
                "ADMIN_TG_IDS": {
                    "format": "json_int_list",
                    "ops": ["list_add", "list_remove"],
                    "item_re": "[1-9][0-9]{4,14}",
                    "max_items": 50,
                    "protected_items": [917456291],
                    "description": "Telegram admin IDs",
                },
                "SUPER_ADMIN_TG_IDS": {
                    "format": "json_int_list",
                    "ops": ["list_add"],
                    "item_re": "[1-9][0-9]{4,14}",
                    "max_items": 10,
                    "protected_items": [917456291],
                    "description": "Telegram super-admin IDs",
                },
            },
        }
    },
}


def _one(**overrides: str) -> dict[str, str]:
    base = {
        "kind": "env_set",
        "key": "ADMIN_TG_IDS",
        "op": "list_add",
        "value": "5339875840",
        "reason": "owner asked to add a new admin",
    }
    base.update(overrides)
    return base


class OpsRequestsTestCase(unittest.TestCase):
    """Loads the real trusted `agent_ops_policy` module + catalog once per
    test, exactly the way `implement.py`/`ops.py` do in production (by file
    path, from a flat `trusted_dir`) -- never a bare `import
    agent_ops_policy`."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        trusted_dir = copy_trusted_dir(Path(self._tmp.name) / "trusted")
        trusted = TrustedModules(trusted_dir)
        self.policy = trusted.agent_ops_policy
        self.repositories = trusted.agent_repos.REPOSITORIES
        self.allowlist = self.policy.parse_allowlist(ALLOWLIST_DATA, self.repositories)


class SplitTrailerTests(unittest.TestCase):
    def test_no_marker_is_a_no_op_besides_trimming(self) -> None:
        summary, raw, note = split_trailer("  Added the notes file.  ")
        self.assertEqual(summary, "Added the notes file.")
        self.assertIsNone(raw)
        self.assertIsNone(note)

    def test_none_message_returns_empty_summary(self) -> None:
        summary, raw, note = split_trailer(None)
        self.assertEqual(summary, "")
        self.assertIsNone(raw)
        self.assertIsNone(note)

    def test_one_marker_line_is_extracted_and_stripped(self) -> None:
        message = (
            "Added a config note.\n"
            'AGENT_OPS_REQUESTS: [{"kind":"env_set","key":"ADMIN_TG_IDS"}]'
        )
        summary, raw, note = split_trailer(message)
        self.assertEqual(summary, "Added a config note.")
        self.assertEqual(raw, '[{"kind":"env_set","key":"ADMIN_TG_IDS"}]')
        self.assertIsNone(note)

    def test_marker_anywhere_in_the_message_is_still_stripped(self) -> None:
        # Codex is instructed to put it last, but the parser must never
        # trust message *position* to keep a value out of the summary.
        message = "AGENT_OPS_REQUESTS: [1,2,3]\nSecond line stays.\nThird line too."
        summary, raw, _note = split_trailer(message)
        self.assertNotIn("AGENT_OPS_REQUESTS", summary)
        self.assertEqual(summary, "Second line stays.\nThird line too.")
        self.assertEqual(raw, "[1,2,3]")

    def test_two_marker_lines_are_ambiguous_and_both_stripped(self) -> None:
        message = (
            "Intro.\n"
            'AGENT_OPS_REQUESTS: [{"a":1}]\n'
            "Middle.\n"
            'AGENT_OPS_REQUESTS: [{"a":2}]'
        )
        summary, raw, note = split_trailer(message)
        self.assertNotIn("AGENT_OPS_REQUESTS", summary)
        self.assertEqual(summary, "Intro.\nMiddle.")
        self.assertIsNone(raw)
        self.assertEqual(note, "ambiguous")

    def test_three_marker_lines_are_also_ambiguous(self) -> None:
        message = "\n".join(["AGENT_OPS_REQUESTS: [1]"] * 3)
        summary, raw, note = split_trailer(message)
        self.assertEqual(summary, "")
        self.assertIsNone(raw)
        self.assertEqual(note, "ambiguous")

    def test_marker_only_message_leaves_empty_summary(self) -> None:
        summary, raw, note = split_trailer('AGENT_OPS_REQUESTS: [{"kind":"env_set"}]')
        self.assertEqual(summary, "")
        self.assertEqual(raw, '[{"kind":"env_set"}]')
        self.assertIsNone(note)


class BuildOpsNoteTests(OpsRequestsTestCase):
    def test_none_trailer_note_returns_none(self) -> None:
        self.assertIsNone(build_ops_note(None, self.policy))

    def test_ambiguous_note_passes_through_sanitized(self) -> None:
        self.assertEqual(build_ops_note("ambiguous", self.policy), "ambiguous")

    def test_note_is_capped_to_200_chars(self) -> None:
        long_note = "x" * 500
        self.assertEqual(len(build_ops_note(long_note, self.policy)), 200)


class ValidateStructuralTests(OpsRequestsTestCase):
    def test_none_raw_returns_no_proposals(self) -> None:
        self.assertEqual(
            validate(
                None,
                project_key="qurbot",
                repo_full_name="muradjanov-dev/qurbot",
                allowlist=self.allowlist,
                policy_module=self.policy,
            ),
            [],
        )

    def test_bad_json_returns_no_proposals(self) -> None:
        result = validate(
            "{not valid json",
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(result, [])

    def test_oversize_raw_returns_no_proposals(self) -> None:
        huge_value = "1" * 5000
        raw = json.dumps([_one(value=huge_value)])
        self.assertGreater(len(raw.encode("utf-8")), 4096)
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(result, [])

    def test_more_than_three_objects_returns_no_proposals(self) -> None:
        raw = json.dumps([_one() for _ in range(4)])
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(result, [])

    def test_exactly_three_objects_is_accepted(self) -> None:
        raw = json.dumps([_one(), _one(op="list_remove"), _one(key="SUPER_ADMIN_TG_IDS")])
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(len(result), 3)

    def test_unknown_key_in_one_object_invalidates_the_whole_batch(self) -> None:
        bad = _one()
        bad["extra_field"] = "not allowed"
        raw = json.dumps([bad])
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(result, [])

    def test_missing_key_in_one_object_invalidates_the_whole_batch(self) -> None:
        bad = _one()
        del bad["reason"]
        raw = json.dumps([bad])
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(result, [])

    def test_non_list_top_level_returns_no_proposals(self) -> None:
        raw = json.dumps(_one())
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(result, [])

    def test_empty_list_is_valid_and_yields_no_proposals(self) -> None:
        result = validate(
            "[]",
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(result, [])


class ValidatePolicyOutcomeTests(OpsRequestsTestCase):
    def test_allowed_request_shape_and_restart_services(self) -> None:
        raw = json.dumps([_one()])
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(len(result), 1)
        proposal = result[0]
        self.assertEqual(
            set(proposal),
            {
                "kind",
                "key",
                "op",
                "value",
                "reason",
                "policy",
                "policy_reason",
                "restart_services",
            },
        )
        self.assertEqual(proposal["kind"], "env_set")
        self.assertEqual(proposal["key"], "ADMIN_TG_IDS")
        self.assertEqual(proposal["op"], "list_add")
        self.assertEqual(proposal["value"], "5339875840")
        self.assertEqual(proposal["policy"], "allowed")
        self.assertEqual(proposal["restart_services"], ["qurbot-web", "qurbot-worker"])

    def test_project_not_in_allowlist_is_denied_not_allowlisted(self) -> None:
        raw = json.dumps([_one()])
        result = validate(
            raw,
            project_key="kans-shop",
            repo_full_name="muradjanov-dev/kans-shop",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["policy"], "denied")
        self.assertEqual(result[0]["policy_reason"], "not_allowlisted")
        self.assertEqual(result[0]["restart_services"], [])

    def test_secret_shaped_key_is_denied_even_though_charset_is_fine(self) -> None:
        raw = json.dumps([_one(key="DATABASE_URL", value="postgres_host")])
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["policy"], "denied")
        self.assertEqual(result[0]["policy_reason"], "secret_key")

    def test_unlisted_key_for_an_allowlisted_project_is_denied(self) -> None:
        raw = json.dumps([_one(key="RANDOM_FLAG", value="on")])
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["policy"], "denied")
        self.assertEqual(result[0]["policy_reason"], "not_allowlisted")

    def test_missing_allowlist_denies_every_request_as_no_allowlist(self) -> None:
        raw = json.dumps([_one()])
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=None,
            policy_module=self.policy,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["policy"], "denied")
        self.assertEqual(result[0]["policy_reason"], "no_allowlist")
        self.assertEqual(result[0]["restart_services"], [])

    def test_reason_is_sanitized_and_capped(self) -> None:
        raw = json.dumps([_one(reason="line1\nline2\x00" + "y" * 400)])
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(len(result), 1)
        reason = result[0]["reason"]
        self.assertNotIn("\x00", reason)
        self.assertLessEqual(len(reason), 300)


class ValidateCharsetDropTests(OpsRequestsTestCase):
    def test_key_failing_key_regex_is_dropped_not_sent(self) -> None:
        # Structurally valid (5 correct string fields) but a key shape the
        # backend's AgentOpsProposal would 422 on -- must never reach the
        # returned list, allowed or denied, or it would lose the whole
        # callback for every other -- possibly valid -- proposal in it.
        raw = json.dumps([_one(key="lowercase_key")])
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(result, [])

    def test_value_with_newline_is_dropped(self) -> None:
        raw = json.dumps([_one(value="5339875840\nDATABASE_URL=x")])
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(result, [])

    def test_value_with_scheme_separator_is_dropped(self) -> None:
        raw = json.dumps([_one(value="http://attacker.example")])
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(result, [])

    def test_bad_row_is_dropped_but_a_good_sibling_row_survives(self) -> None:
        raw = json.dumps([_one(key="lowercase_key"), _one(key="SUPER_ADMIN_TG_IDS")])
        result = validate(
            raw,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["key"], "SUPER_ADMIN_TG_IDS")


if __name__ == "__main__":
    unittest.main()
