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

    def _validate(self, raw, **overrides):
        kwargs = dict(
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
            allowlist=self.allowlist,
            policy_module=self.policy,
        )
        kwargs.update(overrides)
        return validate(raw, **kwargs)


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


class SplitTrailerFuzzyVariantTests(unittest.TestCase):
    """P1-3 regression: a message trying (deliberately or not) to dodge the
    exact-prefix check must still never leak into the summary, even though
    none of these variants is ever trusted enough to be PARSED as JSON."""

    def test_leading_spaces_are_stripped_from_summary_but_never_parsed(self) -> None:
        message = 'Intro.\n   AGENT_OPS_REQUESTS: [{"key":"X"}]\nOutro.'
        summary, raw, _note = split_trailer(message)
        self.assertEqual(summary, "Intro.\nOutro.")
        self.assertIsNone(raw)

    def test_bullet_prefix_is_stripped_but_never_parsed(self) -> None:
        message = 'Intro.\n- AGENT_OPS_REQUESTS: [{"key":"X"}]\nOutro.'
        summary, raw, _note = split_trailer(message)
        self.assertEqual(summary, "Intro.\nOutro.")
        self.assertIsNone(raw)

    def test_backtick_wrapped_is_stripped_but_never_parsed(self) -> None:
        message = 'Intro.\n`AGENT_OPS_REQUESTS: [{"key":"X"}]`\nOutro.'
        summary, raw, _note = split_trailer(message)
        self.assertEqual(summary, "Intro.\nOutro.")
        self.assertIsNone(raw)

    def test_mid_line_occurrence_is_stripped_but_never_parsed(self) -> None:
        message = 'Intro.\nNote: AGENT_OPS_REQUESTS: [{"key":"X"}] end\nOutro.'
        summary, raw, _note = split_trailer(message)
        self.assertEqual(summary, "Intro.\nOutro.")
        self.assertIsNone(raw)

    def test_lowercase_variant_is_stripped_but_never_parsed(self) -> None:
        message = 'Intro.\nagent_ops_requests: [{"key":"X"}]\nOutro.'
        summary, raw, _note = split_trailer(message)
        self.assertEqual(summary, "Intro.\nOutro.")
        self.assertIsNone(raw)

    def test_zero_width_prefix_is_stripped_but_never_parsed(self) -> None:
        message = 'Intro.\n​AGENT_OPS_REQUESTS: [{"key":"X"}]\nOutro.'
        summary, raw, _note = split_trailer(message)
        self.assertEqual(summary, "Intro.\nOutro.")
        self.assertIsNone(raw)

    def test_bom_prefix_is_stripped_but_never_parsed(self) -> None:
        message = 'Intro.\n﻿AGENT_OPS_REQUESTS: [{"key":"X"}]\nOutro.'
        summary, raw, _note = split_trailer(message)
        self.assertEqual(summary, "Intro.\nOutro.")
        self.assertIsNone(raw)

    def test_bare_cr_line_separator_isolates_the_marker_line(self) -> None:
        message = 'Intro.\r AGENT_OPS_REQUESTS: [{"key":"X"}]\rOutro.'
        summary, _raw, _note = split_trailer(message)
        self.assertNotIn("AGENT_OPS_REQUESTS", summary)
        self.assertNotIn("key", summary)

    def test_unicode_line_separator_isolates_the_marker_line(self) -> None:
        message = 'Intro. AGENT_OPS_REQUESTS: [{"key":"X"}] Outro.'
        summary, raw, note = split_trailer(message)
        self.assertNotIn("AGENT_OPS_REQUESTS", summary)
        self.assertEqual(summary, "Intro.\nOutro.")
        # Here the marker line, isolated via U+2028, DOES start exactly at
        # column 0 with the literal prefix -- so unlike the other fuzzy
        # variants above, this one is trusted and parsed.
        self.assertEqual(raw, '[{"key":"X"}]')
        self.assertIsNone(note)

    def test_json_on_the_line_after_an_empty_marker_line_is_scrubbed(self) -> None:
        message = (
            'Intro.\nAGENT_OPS_REQUESTS:\n[{"kind":"env_set","value":"5339875840"}]\nOutro.'
        )
        summary, raw, _note = split_trailer(message)
        self.assertEqual(summary, "Intro.\nOutro.")
        self.assertNotIn("5339875840", summary)
        # Nothing was on the marker line itself to trust as raw JSON.
        self.assertIsNone(raw)

    def test_json_on_the_next_line_after_a_fuzzy_marker_is_also_scrubbed(self) -> None:
        message = 'Intro.\n- agent_ops_requests:\n[{"value":"5339875840"}]\nOutro.'
        summary, raw, _note = split_trailer(message)
        self.assertEqual(summary, "Intro.\nOutro.")
        self.assertNotIn("5339875840", summary)
        self.assertIsNone(raw)

    def test_non_bracket_line_after_a_marker_is_left_alone(self) -> None:
        # Only a following line that ITSELF looks like the start of a JSON
        # array is treated as a leaked continuation; ordinary prose right
        # after a fuzzy-matched line is not swept away too.
        message = "Intro.\nagent_ops_requests thoughts:\nThis stays.\nOutro."
        summary, _raw, _note = split_trailer(message)
        self.assertIn("This stays.", summary)


class BuildOpsNoteTests(OpsRequestsTestCase):
    def test_no_notes_returns_none(self) -> None:
        self.assertIsNone(build_ops_note(policy_module=self.policy))

    def test_ambiguous_trailer_note_passes_through_sanitized(self) -> None:
        note = build_ops_note(trailer_note="ambiguous", policy_module=self.policy)
        self.assertEqual(note, "ambiguous")

    def test_drop_note_passes_through_when_no_trailer_note(self) -> None:
        note = build_ops_note(drop_note="dropped_2", policy_module=self.policy)
        self.assertEqual(note, "dropped_2")

    def test_trailer_note_takes_priority_over_drop_note(self) -> None:
        note = build_ops_note(
            trailer_note="ambiguous", drop_note="dropped_1", policy_module=self.policy
        )
        self.assertEqual(note, "ambiguous")

    def test_note_is_capped_to_200_chars(self) -> None:
        long_note = "x" * 500
        note = build_ops_note(drop_note=long_note, policy_module=self.policy)
        self.assertEqual(len(note), 200)


class ValidateStructuralTests(OpsRequestsTestCase):
    def test_none_raw_returns_no_proposals_and_no_note(self) -> None:
        proposals, note = self._validate(None)
        self.assertEqual(proposals, [])
        self.assertIsNone(note)

    def test_bad_json_returns_invalid_trailer(self) -> None:
        proposals, note = self._validate("{not valid json")
        self.assertEqual(proposals, [])
        self.assertEqual(note, "invalid_trailer")

    def test_oversize_raw_returns_invalid_trailer(self) -> None:
        huge_value = "1" * 5000
        raw = json.dumps([_one(value=huge_value)])
        self.assertGreater(len(raw.encode("utf-8")), 4096)
        proposals, note = self._validate(raw)
        self.assertEqual(proposals, [])
        self.assertEqual(note, "invalid_trailer")

    def test_more_than_three_objects_returns_invalid_trailer(self) -> None:
        raw = json.dumps([_one() for _ in range(4)])
        proposals, note = self._validate(raw)
        self.assertEqual(proposals, [])
        self.assertEqual(note, "invalid_trailer")

    def test_exactly_three_objects_is_accepted(self) -> None:
        raw = json.dumps([_one(), _one(op="list_remove"), _one(key="SUPER_ADMIN_TG_IDS")])
        proposals, note = self._validate(raw)
        self.assertEqual(len(proposals), 3)
        self.assertIsNone(note)

    def test_unknown_key_in_one_object_invalidates_the_whole_batch(self) -> None:
        bad = _one()
        bad["extra_field"] = "not allowed"
        raw = json.dumps([bad])
        proposals, note = self._validate(raw)
        self.assertEqual(proposals, [])
        self.assertEqual(note, "invalid_trailer")

    def test_missing_key_in_one_object_invalidates_the_whole_batch(self) -> None:
        bad = _one()
        del bad["reason"]
        raw = json.dumps([bad])
        proposals, note = self._validate(raw)
        self.assertEqual(proposals, [])
        self.assertEqual(note, "invalid_trailer")

    def test_non_list_top_level_returns_invalid_trailer(self) -> None:
        raw = json.dumps(_one())
        proposals, note = self._validate(raw)
        self.assertEqual(proposals, [])
        self.assertEqual(note, "invalid_trailer")

    def test_empty_list_is_valid_and_yields_no_proposals_or_note(self) -> None:
        proposals, note = self._validate("[]")
        self.assertEqual(proposals, [])
        self.assertIsNone(note)


class ValidateKindOpTests(OpsRequestsTestCase):
    """P1-1 regression: `kind`/`op` are backend Literal fields -- a bad
    value here must never reach the returned proposal list (it would 422
    the whole callback), for BOTH an otherwise-allowed and an
    otherwise-denied row, and must show up in the drop-count note."""

    def test_kind_other_than_env_set_is_dropped(self) -> None:
        raw = json.dumps([_one(kind="db_migrate")])
        proposals, note = self._validate(raw)
        self.assertEqual(proposals, [])
        self.assertEqual(note, "dropped_1")

    def test_op_outside_the_known_set_is_dropped(self) -> None:
        raw = json.dumps([_one(op="delete_everything")])
        proposals, note = self._validate(raw)
        self.assertEqual(proposals, [])
        self.assertEqual(note, "dropped_1")

    def test_bad_kind_is_dropped_even_when_the_row_would_otherwise_be_denied(self) -> None:
        # `not_allowlisted`/`project_denied`/`repo_mismatch` all return from
        # `validate_request` before it ever inspects `kind`/`op` -- so a
        # row denied for one of THOSE reasons still needs its own kind/op
        # check here, independent of the policy call.
        raw = json.dumps([_one(kind="db_migrate")])
        proposals, note = self._validate(
            raw, project_key="kans-shop", repo_full_name="muradjanov-dev/kans-shop"
        )
        self.assertEqual(proposals, [])
        self.assertEqual(note, "dropped_1")

    def test_bad_op_is_dropped_when_allowlist_is_none(self) -> None:
        # `allowlist is None` short-circuits straight to `policy_reason=
        # "no_allowlist"` without ever calling `validate_request` at all --
        # the kind/op check here is the ONLY thing protecting this path.
        raw = json.dumps([_one(op="delete_everything")])
        proposals, note = self._validate(raw, allowlist=None)
        self.assertEqual(proposals, [])
        self.assertEqual(note, "dropped_1")

    def test_good_row_survives_alongside_a_bad_kind_row(self) -> None:
        raw = json.dumps([_one(kind="db_migrate"), _one(key="SUPER_ADMIN_TG_IDS")])
        proposals, note = self._validate(raw)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["key"], "SUPER_ADMIN_TG_IDS")
        self.assertEqual(note, "dropped_1")


class ValidatePolicyOutcomeTests(OpsRequestsTestCase):
    def test_allowed_request_shape_and_restart_services(self) -> None:
        raw = json.dumps([_one()])
        proposals, note = self._validate(raw)
        self.assertEqual(len(proposals), 1)
        self.assertIsNone(note)
        proposal = proposals[0]
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
        proposals, _note = self._validate(
            raw, project_key="kans-shop", repo_full_name="muradjanov-dev/kans-shop"
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["policy"], "denied")
        self.assertEqual(proposals[0]["policy_reason"], "not_allowlisted")
        self.assertEqual(proposals[0]["restart_services"], [])

    def test_secret_shaped_key_is_denied_even_though_charset_is_fine(self) -> None:
        raw = json.dumps([_one(key="DATABASE_URL", value="postgres_host")])
        proposals, _note = self._validate(raw)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["policy"], "denied")
        self.assertEqual(proposals[0]["policy_reason"], "secret_key")

    def test_unlisted_key_for_an_allowlisted_project_is_denied(self) -> None:
        raw = json.dumps([_one(key="RANDOM_FLAG", value="on")])
        proposals, _note = self._validate(raw)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["policy"], "denied")
        self.assertEqual(proposals[0]["policy_reason"], "not_allowlisted")

    def test_missing_allowlist_denies_every_request_as_no_allowlist(self) -> None:
        raw = json.dumps([_one()])
        proposals, _note = self._validate(raw, allowlist=None)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["policy"], "denied")
        self.assertEqual(proposals[0]["policy_reason"], "no_allowlist")
        self.assertEqual(proposals[0]["restart_services"], [])

    def test_disabled_lane_denies_every_request_as_ops_disabled_even_with_an_allowlist(
        self,
    ) -> None:
        # P3-5: `enabled=False` must win over a real, otherwise-allowing
        # allowlist -- never fall back to `validate_request`/"no_allowlist".
        raw = json.dumps([_one()])
        proposals, _note = self._validate(raw, allowlist=self.allowlist, enabled=False)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["policy"], "denied")
        self.assertEqual(proposals[0]["policy_reason"], "ops_disabled")
        self.assertEqual(proposals[0]["restart_services"], [])

    def test_reason_is_sanitized_and_capped(self) -> None:
        raw = json.dumps([_one(reason="line1\nline2\x00" + "y" * 400)])
        proposals, _note = self._validate(raw)
        self.assertEqual(len(proposals), 1)
        reason = proposals[0]["reason"]
        self.assertNotIn("\x00", reason)
        self.assertLessEqual(len(reason), 300)


class ValidateCharsetDropTests(OpsRequestsTestCase):
    def test_key_failing_key_regex_is_dropped_not_sent(self) -> None:
        # Structurally valid (5 correct string fields) but a key shape the
        # backend's AgentOpsProposal would 422 on -- must never reach the
        # returned list, allowed or denied, or it would lose the whole
        # callback for every other -- possibly valid -- proposal in it.
        raw = json.dumps([_one(key="lowercase_key")])
        proposals, note = self._validate(raw)
        self.assertEqual(proposals, [])
        self.assertEqual(note, "dropped_1")

    def test_value_with_newline_is_dropped(self) -> None:
        raw = json.dumps([_one(value="5339875840\nDATABASE_URL=x")])
        proposals, note = self._validate(raw)
        self.assertEqual(proposals, [])
        self.assertEqual(note, "dropped_1")

    def test_value_with_scheme_separator_is_dropped(self) -> None:
        raw = json.dumps([_one(value="http://attacker.example")])
        proposals, note = self._validate(raw)
        self.assertEqual(proposals, [])
        self.assertEqual(note, "dropped_1")

    def test_bad_row_is_dropped_but_a_good_sibling_row_survives(self) -> None:
        raw = json.dumps([_one(key="lowercase_key"), _one(key="SUPER_ADMIN_TG_IDS")])
        proposals, note = self._validate(raw)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["key"], "SUPER_ADMIN_TG_IDS")
        self.assertEqual(note, "dropped_1")


if __name__ == "__main__":
    unittest.main()
