import dataclasses
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import agent_ops_policy as policy
from agent_ops_policy import (
    FORMATS,
    HARD_DENIED_PROJECTS,
    KEY_RE,
    MAX_REQUESTS,
    MAX_VALUE_LEN,
    OPS,
    SECRET_KEY_RE,
    VALUE_RE,
    Allowlist,
    KeyPolicy,
    PolicyError,
    apply_op,
    load_allowlist,
    parse_allowlist,
    request_hash,
    sanitize_reason,
    validate_request,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "app" / "services"))
from agent_repos import REPOSITORIES as REAL_REPOSITORIES  # noqa: E402

# ---------------------------------------------------------------------------
# A tiny fake catalog, shaped like `agent_repos.AgentRepository` in only the
# fields this module reads (`project_key`, `full_name`, `images`), so most
# tests do not depend on the real catalog's exact contents staying stable.
# One test class below (RealCatalogTests) exercises the real one directly.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _FakeRepo:
    project_key: str
    full_name: str
    images: tuple[tuple[str, str], ...]


FAKE_REPOSITORIES: tuple[_FakeRepo, ...] = (
    _FakeRepo(
        "qurbot",
        "muradjanov-dev/qurbot",
        (
            ("qurbot-web", "ghcr.io/muradjanov-dev/qurbot"),
            ("qurbot-worker", "ghcr.io/muradjanov-dev/qurbot"),
        ),
    ),
    _FakeRepo(
        "kans-shop",
        "muradjanov-dev/kans-shop",
        (("kans-api", "ghcr.io/muradjanov-dev/kans-shop-api"),),
    ),
)


def _qurbot_project() -> dict:
    """A valid project body, matching the spec section 3 example exactly."""
    return {
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


def _doc(**project_overrides: object) -> dict:
    project = _qurbot_project()
    project.update(project_overrides)
    return {"version": 1, "projects": {"qurbot": project}}


def _parse(doc: dict, repositories=FAKE_REPOSITORIES) -> Allowlist:
    return parse_allowlist(doc, repositories)


class GoldenHashTests(unittest.TestCase):
    def test_golden_vector(self) -> None:
        # WP-A reuses this exact hex for its own tests -- do not "fix" it
        # without updating that side too.
        digest = request_hash(
            run_id="00000000-0000-4000-8000-000000000001",
            project_key="qurbot",
            kind="env_set",
            key="ADMIN_TG_IDS",
            op="list_add",
            value="5339875840",
        )
        self.assertEqual(
            digest,
            "6f374a3b95d9eb840a36960be9eb88a702042b509bacce9e08911f55ee4b74ed",
        )
        self.assertEqual(len(digest), 64)

    def test_is_deterministic_and_kwarg_order_independent(self) -> None:
        kwargs = dict(
            run_id="r",
            project_key="qurbot",
            kind="env_set",
            key="ADMIN_TG_IDS",
            op="list_add",
            value="1",
        )
        self.assertEqual(request_hash(**kwargs), request_hash(**dict(kwargs)))

    def test_changes_with_any_field(self) -> None:
        base = request_hash(
            run_id="r", project_key="qurbot", kind="env_set", key="K", op="list_add", value="1"
        )
        self.assertNotEqual(
            base,
            request_hash(
                run_id="r2",
                project_key="qurbot",
                kind="env_set",
                key="K",
                op="list_add",
                value="1",
            ),
        )
        self.assertNotEqual(
            base,
            request_hash(
                run_id="r",
                project_key="qurbot",
                kind="env_set",
                key="K",
                op="list_add",
                value="2",
            ),
        )


class ConstantsTests(unittest.TestCase):
    def test_shapes(self) -> None:
        self.assertEqual(MAX_REQUESTS, 3)
        self.assertEqual(MAX_VALUE_LEN, 256)
        self.assertEqual(HARD_DENIED_PROJECTS, frozenset({"task-manager", "agent-qa"}))
        self.assertEqual(set(FORMATS), {"json_int_list", "csv_int_list", "scalar"})
        self.assertEqual(set(OPS), {"replace", "list_add", "list_remove"})


class RegexFullmatchTraps(unittest.TestCase):
    """The exact adversarial inputs section 2/10 of the spec calls out."""

    def test_newline_trap_rejected_even_though_dollar_would_pass(self) -> None:
        # A `^...$`-anchored `.match()` treats a trailing "\n" as matching
        # "$"; `.fullmatch()` must not.
        loose = policy.re.compile(r"^[1-9][0-9]{4,14}$")
        self.assertIsNotNone(loose.match("5339875840\n"))  # the trap, demonstrated
        item_re = policy.re.compile(r"[1-9][0-9]{4,14}")
        self.assertIsNone(item_re.fullmatch("5339875840\n"))
        self.assertFalse(VALUE_RE.fullmatch("5339875840\n"))

    def test_non_ascii_digit_lookalike_rejected(self) -> None:
        # U+0665 ARABIC-INDIC DIGIT FIVE.
        self.assertFalse(VALUE_RE.fullmatch("٥"))
        self.assertFalse(KEY_RE.fullmatch("AB٥"))

    def test_value_re_rejects_dangerous_characters(self) -> None:
        # "://" itself is inside VALUE_RE's charset (":" and "/" are both
        # allowed, for legitimate non-URL uses); it is refused by the
        # separate "://" substring check applied in `validate_request`
        # (see ValidateRequestTests.test_bad_value_scheme), not by VALUE_RE
        # alone -- so it is intentionally not in this list.
        for bad in ["a$b", "a#b", "a=b", "'q'", '"q"', "a b", "a\x00b", "a b"]:
            with self.subTest(bad=bad):
                self.assertFalse(VALUE_RE.fullmatch(bad))

    def test_value_re_accepts_the_allowed_charset(self) -> None:
        self.assertTrue(VALUE_RE.fullmatch("Abc123_.,:@/+-"))

    def test_key_re_rejects_lowercase_and_case_variants(self) -> None:
        for bad in ["admin_tg_ids", "Admin_Tg_Ids", "adminTgIds", "1BAD", "A"]:
            with self.subTest(bad=bad):
                self.assertFalse(KEY_RE.fullmatch(bad))
        self.assertTrue(KEY_RE.fullmatch("ADMIN_TG_IDS"))


class SecretKeyDenylistTests(unittest.TestCase):
    SECRET_KEYS = (
        "DATABASE_URL",
        "BOT_TOKEN",
        "OPENAI_API_KEY",
        "WEBHOOK_HOST",
        "ADMIN_BASIC_AUTH_PASSWORD",
    )

    def test_regex_matches_each_secret_key(self) -> None:
        for key in self.SECRET_KEYS:
            with self.subTest(key=key):
                self.assertTrue(SECRET_KEY_RE.fullmatch(key))

    def test_allowlist_parsing_rejects_secret_named_keys(self) -> None:
        for key in self.SECRET_KEYS:
            with self.subTest(key=key):
                project = _qurbot_project()
                project["keys"] = {
                    key: {
                        "format": "scalar",
                        "ops": ["replace"],
                        "value_re": "[A-Za-z0-9]{1,32}",
                        "description": "not allowed",
                    }
                }
                doc = {"version": 1, "projects": {"qurbot": project}}
                with self.assertRaises(PolicyError) as ctx:
                    _parse(doc)
                self.assertEqual(ctx.exception.args[0], "secret_key")

    def test_validate_request_rejects_secret_named_keys(self) -> None:
        allowlist = _parse(_doc())
        for key in self.SECRET_KEYS:
            with self.subTest(key=key):
                req = {
                    "kind": "env_set",
                    "key": key,
                    "op": "replace",
                    "value": "x",
                    "reason": "",
                }
                ok, code = validate_request(
                    req,
                    allowlist,
                    project_key="qurbot",
                    repo_full_name="muradjanov-dev/qurbot",
                )
                self.assertFalse(ok)
                self.assertEqual(code, "secret_key")


class ValidateRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.allowlist = _parse(_doc())

    def _req(self, **overrides: object) -> dict:
        base = {
            "kind": "env_set",
            "key": "ADMIN_TG_IDS",
            "op": "list_add",
            "value": "5339875840",
            "reason": "owner asked",
        }
        base.update(overrides)
        return base

    def _check(self, **overrides: object) -> tuple[bool, str]:
        return validate_request(
            self._req(**overrides),
            self.allowlist,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
        )

    def test_ok(self) -> None:
        self.assertEqual(self._check(), (True, "ok"))

    def test_reason_is_never_inspected(self) -> None:
        # Missing "reason" entirely, or garbage in it, changes nothing.
        req = self._req()
        del req["reason"]
        self.assertEqual(
            validate_request(
                req,
                self.allowlist,
                project_key="qurbot",
                repo_full_name="muradjanov-dev/qurbot",
            ),
            (True, "ok"),
        )
        self.assertEqual(self._check(reason="\x00\x01 not sanitized at all"), (True, "ok"))

    def test_bad_kind(self) -> None:
        self.assertEqual(self._check(kind="env_delete"), (False, "bad_kind"))

    def test_bad_key_lowercase(self) -> None:
        self.assertEqual(self._check(key="admin_tg_ids"), (False, "bad_key"))

    def test_bad_key_newline(self) -> None:
        self.assertEqual(self._check(key="ADMIN_TG_IDS\n"), (False, "bad_key"))

    def test_secret_key(self) -> None:
        self.assertEqual(self._check(key="DATABASE_URL"), (False, "secret_key"))

    def test_not_allowlisted_unknown_key(self) -> None:
        # "FLAG" (unlike "KEY") is not in the secret-name denylist, so this
        # exercises "not in this project's keys" rather than "secret_key".
        self.assertEqual(self._check(key="SOME_OTHER_FLAG"), (False, "not_allowlisted"))

    def test_not_allowlisted_unknown_project(self) -> None:
        ok, code = validate_request(
            self._req(),
            self.allowlist,
            project_key="kans-shop",
            repo_full_name="muradjanov-dev/kans-shop",
        )
        self.assertEqual((ok, code), (False, "not_allowlisted"))

    def test_project_denied_hard_denylist(self) -> None:
        ok, code = validate_request(
            self._req(), self.allowlist, project_key="task-manager", repo_full_name="x/y"
        )
        self.assertEqual((ok, code), (False, "project_denied"))

    def test_repo_mismatch(self) -> None:
        ok, code = validate_request(
            self._req(), self.allowlist, project_key="qurbot", repo_full_name="attacker/qurbot"
        )
        self.assertEqual((ok, code), (False, "repo_mismatch"))

    def test_bad_op_not_in_ops_vocabulary(self) -> None:
        self.assertEqual(self._check(op="delete"), (False, "bad_op"))

    def test_bad_op_not_allowed_for_key(self) -> None:
        # ADMIN_TG_IDS's policy only allows list_add/list_remove, never replace.
        self.assertEqual(self._check(op="replace"), (False, "bad_op"))

    def test_bad_value_newline(self) -> None:
        self.assertEqual(self._check(value="5339875840\n"), (False, "bad_value"))

    def test_bad_value_dollar_interpolation(self) -> None:
        self.assertEqual(self._check(value="$HOME"), (False, "bad_value"))

    def test_bad_value_scheme(self) -> None:
        self.assertEqual(self._check(value="http://1234567"), (False, "bad_value"))

    def test_bad_value_nul(self) -> None:
        self.assertEqual(self._check(value="1234\x00"), (False, "bad_value"))

    def test_bad_value_too_long(self) -> None:
        self.assertEqual(self._check(value="1" * (MAX_VALUE_LEN + 1)), (False, "bad_value"))

    def test_item_pattern_mismatch(self) -> None:
        # Passes the global VALUE_RE but not this key's item_re (too short).
        self.assertEqual(self._check(value="12"), (False, "item_pattern"))

    def test_scalar_bad_value_uses_bad_value_code(self) -> None:
        project = _qurbot_project()
        project["keys"] = {
            "FEATURE_FLAG": {
                "format": "scalar",
                "ops": ["replace"],
                "value_re": "on|off",
                "description": "toggle",
            }
        }
        allowlist = _parse({"version": 1, "projects": {"qurbot": project}})
        ok, code = validate_request(
            {"kind": "env_set", "key": "FEATURE_FLAG", "op": "replace", "value": "maybe"},
            allowlist,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
        )
        self.assertEqual((ok, code), (False, "bad_value"))
        ok, code = validate_request(
            {"kind": "env_set", "key": "FEATURE_FLAG", "op": "replace", "value": "on"},
            allowlist,
            project_key="qurbot",
            repo_full_name="muradjanov-dev/qurbot",
        )
        self.assertEqual((ok, code), (True, "ok"))


class ApplyOpJsonIntListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.allowlist = _parse(_doc())
        self.key_policy = self.allowlist.projects["qurbot"].keys["ADMIN_TG_IDS"]

    def test_round_trip_add_then_remove(self) -> None:
        new_raw, changed = apply_op(
            "json_int_list", "[11111,22222,33333]", "list_add", "44444", self.key_policy
        )
        self.assertEqual((new_raw, changed), ("[11111,22222,33333,44444]", True))
        new_raw, changed = apply_op(
            "json_int_list", new_raw, "list_remove", "44444", self.key_policy
        )
        self.assertEqual((new_raw, changed), ("[11111,22222,33333]", True))

    def test_list_add_idempotent(self) -> None:
        new_raw, changed = apply_op(
            "json_int_list", "[11111,22222,33333]", "list_add", "22222", self.key_policy
        )
        self.assertEqual((new_raw, changed), ("[11111,22222,33333]", False))

    def test_list_remove_absent_item_is_a_noop(self) -> None:
        new_raw, changed = apply_op(
            "json_int_list", "[11111,22222,33333]", "list_remove", "99999", self.key_policy
        )
        self.assertEqual((new_raw, changed), ("[11111,22222,33333]", False))

    def test_protected_item_never_removed_even_if_absent(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op(
                "json_int_list",
                "[11111,22222,33333]",
                "list_remove",
                "917456291",
                self.key_policy,
            )
        self.assertEqual(ctx.exception.args[0], "protected_item")

    def test_protected_item_refused_when_present(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op(
                "json_int_list",
                "[917456291,11111]",
                "list_remove",
                "917456291",
                self.key_policy,
            )
        self.assertEqual(ctx.exception.args[0], "protected_item")

    def test_max_items_enforced(self) -> None:
        # max_items is 50 for ADMIN_TG_IDS; build a list of exactly 50.
        full = json.dumps([10000 + i for i in range(50)], separators=(",", ":"))
        with self.assertRaises(PolicyError) as ctx:
            apply_op("json_int_list", full, "list_add", "99999", self.key_policy)
        self.assertEqual(ctx.exception.args[0], "max_items")

    def test_max_items_does_not_block_idempotent_add(self) -> None:
        full = json.dumps([10000 + i for i in range(50)], separators=(",", ":"))
        _, changed = apply_op("json_int_list", full, "list_add", "10000", self.key_policy)
        self.assertFalse(changed)

    def test_list_remove_last_item_refused(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op("json_int_list", "[123456]", "list_remove", "123456", self.key_policy)
        self.assertEqual(ctx.exception.args[0], "empty_result")

    def test_missing_key_precondition(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op("json_int_list", None, "list_add", "123456", self.key_policy)
        self.assertEqual(ctx.exception.args[0], "missing_key")

    def test_item_pattern_enforced_on_new_value(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op("json_int_list", "[1]", "list_add", "12", self.key_policy)  # too short
        self.assertEqual(ctx.exception.args[0], "item_pattern")

    def test_malformed_current_value_bool(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op("json_int_list", "[1,true]", "list_add", "123456", self.key_policy)
        self.assertEqual(ctx.exception.args[0], "malformed_list")

    def test_malformed_current_value_float(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op("json_int_list", "[1,2.5]", "list_add", "123456", self.key_policy)
        self.assertEqual(ctx.exception.args[0], "malformed_list")

    def test_malformed_current_value_dup(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op("json_int_list", "[1,1]", "list_add", "123456", self.key_policy)
        self.assertEqual(ctx.exception.args[0], "malformed_list")

    def test_malformed_current_value_not_a_list(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op("json_int_list", '{"a":1}', "list_add", "123456", self.key_policy)
        self.assertEqual(ctx.exception.args[0], "malformed_list")

    def test_malformed_current_value_not_json(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op("json_int_list", "not json", "list_add", "123456", self.key_policy)
        self.assertEqual(ctx.exception.args[0], "malformed_list")

    def test_no_duplicates_on_output(self) -> None:
        new_raw, _ = apply_op(
            "json_int_list", "[11111,22222]", "list_add", "33333", self.key_policy
        )
        numbers = json.loads(new_raw)
        self.assertEqual(len(numbers), len(set(numbers)))

    def test_format_key_policy_mismatch_refused(self) -> None:
        scalar_policy = KeyPolicy(
            format="scalar", ops=("replace",), description="x", value_re=policy.re.compile("a")
        )
        with self.assertRaises(PolicyError) as ctx:
            apply_op("json_int_list", "[1]", "list_add", "123456", scalar_policy)
        self.assertEqual(ctx.exception.args[0], "bad_format")


class ApplyOpCsvIntListTests(unittest.TestCase):
    def setUp(self) -> None:
        project = _qurbot_project()
        project["keys"] = {
            "ALLOWED_CHAT_IDS": {
                "format": "csv_int_list",
                "ops": ["list_add", "list_remove"],
                "item_re": "[1-9][0-9]{4,14}",
                "max_items": 5,
                "protected_items": [111111111],
                "description": "Allowed chat IDs",
            }
        }
        doc = {"version": 1, "projects": {"qurbot": project}}
        self.allowlist = _parse(doc)
        self.key_policy = self.allowlist.projects["qurbot"].keys["ALLOWED_CHAT_IDS"]

    def test_round_trip_add_then_remove(self) -> None:
        new_raw, changed = apply_op(
            "csv_int_list", "111111111,222222222", "list_add", "333333333", self.key_policy
        )
        self.assertEqual((new_raw, changed), ("111111111,222222222,333333333", True))
        new_raw, changed = apply_op(
            "csv_int_list", new_raw, "list_remove", "333333333", self.key_policy
        )
        self.assertEqual((new_raw, changed), ("111111111,222222222", True))

    def test_tolerates_spaces_on_input(self) -> None:
        new_raw, changed = apply_op(
            "csv_int_list",
            "111111111, 222222222 , 333333333",
            "list_add",
            "444444444",
            self.key_policy,
        )
        self.assertEqual(new_raw, "111111111,222222222,333333333,444444444")
        self.assertTrue(changed)

    def test_empty_current_value_is_empty_list(self) -> None:
        new_raw, changed = apply_op(
            "csv_int_list", "", "list_add", "444444444", self.key_policy
        )
        self.assertEqual((new_raw, changed), ("444444444", True))

    def test_protected_item_refused(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op("csv_int_list", "111111111", "list_remove", "111111111", self.key_policy)
        self.assertEqual(ctx.exception.args[0], "protected_item")

    def test_last_item_refused(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op("csv_int_list", "222222222", "list_remove", "222222222", self.key_policy)
        self.assertEqual(ctx.exception.args[0], "empty_result")

    def test_malformed_current_value(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op("csv_int_list", "1,abc", "list_add", "222222222", self.key_policy)
        self.assertEqual(ctx.exception.args[0], "malformed_list")


class ApplyOpScalarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key_policy = KeyPolicy(
            format="scalar",
            ops=("replace",),
            description="toggle",
            value_re=policy.re.compile(r"on|off"),
        )

    def test_replace_changes_value(self) -> None:
        self.assertEqual(
            apply_op("scalar", "off", "replace", "on", self.key_policy), ("on", True)
        )

    def test_replace_same_value_not_changed(self) -> None:
        self.assertEqual(
            apply_op("scalar", "on", "replace", "on", self.key_policy), ("on", False)
        )

    def test_replace_missing_key_sets_fresh_value(self) -> None:
        self.assertEqual(
            apply_op("scalar", None, "replace", "on", self.key_policy), ("on", True)
        )

    def test_replace_bad_value(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op("scalar", "off", "replace", "maybe", self.key_policy)
        self.assertEqual(ctx.exception.args[0], "bad_value")

    def test_non_replace_op_refused(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            apply_op("scalar", "off", "list_add", "on", self.key_policy)
        self.assertEqual(ctx.exception.args[0], "bad_op")


class SanitizeReasonTests(unittest.TestCase):
    def test_strips_control_chars_and_separators(self) -> None:
        raw = "owner asked\x00\x01\r\nfor this please\x1b"
        cleaned = sanitize_reason(raw)
        self.assertNotIn("\x00", cleaned)
        self.assertNotIn("\r", cleaned)
        self.assertNotIn("\n", cleaned)
        self.assertNotIn(" ", cleaned)
        self.assertNotIn(" ", cleaned)
        self.assertNotIn("\x1b", cleaned)
        self.assertEqual(cleaned, "owner askedforthisplease")

    def test_caps_length(self) -> None:
        cleaned = sanitize_reason("a" * 1000)
        self.assertEqual(len(cleaned), 300)

    def test_preserves_ordinary_text(self) -> None:
        text = "Telegram admin so'rovi, ID qo'shildi."
        self.assertEqual(sanitize_reason(text), text)


class AllowlistParsingTests(unittest.TestCase):
    def test_feature_off_example_parses(self) -> None:
        allowlist = _parse({"version": 1, "projects": {}})
        self.assertEqual(allowlist.projects, {})

    def test_spec_example_parses(self) -> None:
        allowlist = _parse(_doc())
        self.assertIn("qurbot", allowlist.projects)
        project = allowlist.projects["qurbot"]
        self.assertEqual(project.stack, "qurbot")
        self.assertEqual(set(project.keys), {"ADMIN_TG_IDS", "SUPER_ADMIN_TG_IDS"})
        admin = project.keys["ADMIN_TG_IDS"]
        self.assertEqual(admin.ops, ("list_add", "list_remove"))
        self.assertEqual(admin.protected_items, frozenset({917456291}))
        self.assertEqual(admin.max_items, 50)

    def test_unknown_top_level_field_rejects_whole_file(self) -> None:
        doc = _doc()
        doc["unexpected"] = True
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "unknown_field")

    def test_unknown_project_level_field_rejects_whole_file(self) -> None:
        doc = _doc(unexpected="x")
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "unknown_field")

    def test_unknown_key_level_field_rejects_whole_file(self) -> None:
        project = _qurbot_project()
        project["keys"]["ADMIN_TG_IDS"]["unexpected"] = "x"
        with self.assertRaises(PolicyError) as ctx:
            _parse({"version": 1, "projects": {"qurbot": project}})
        self.assertEqual(ctx.exception.args[0], "unknown_field")

    def test_version_must_be_1(self) -> None:
        for bad_version in (0, 2, "1", None):
            with self.subTest(bad_version=bad_version):
                doc = _doc()
                doc["version"] = bad_version
                with self.assertRaises(PolicyError) as ctx:
                    _parse(doc)
                self.assertEqual(ctx.exception.args[0], "bad_version")

    def test_task_manager_hard_denied(self) -> None:
        project = _qurbot_project()
        project["repo_full_name"] = "Asadtop4ik/task-manager"
        doc = {"version": 1, "projects": {"task-manager": project}}
        with self.assertRaises(PolicyError) as ctx:
            parse_allowlist(doc, REAL_REPOSITORIES)
        self.assertEqual(ctx.exception.args[0], "project_denied")

    def test_agent_qa_hard_denied(self) -> None:
        project = _qurbot_project()
        project["repo_full_name"] = "Asadtop4ik/agent-qa"
        doc = {"version": 1, "projects": {"agent-qa": project}}
        with self.assertRaises(PolicyError) as ctx:
            parse_allowlist(doc, REAL_REPOSITORIES)
        self.assertEqual(ctx.exception.args[0], "project_denied")

    def test_bad_project_key_format(self) -> None:
        for bad_key in ("Qurbot", "qur_bot", "qur bot", "a" * 41):
            with self.subTest(bad_key=bad_key):
                project = _qurbot_project()
                doc = {"version": 1, "projects": {bad_key: project}}
                with self.assertRaises(PolicyError) as ctx:
                    _parse(doc)
                self.assertEqual(ctx.exception.args[0], "bad_project_key")

    def test_env_file_path_mismatch(self) -> None:
        doc = _doc(env_file="/srv/stack/env/other.env")
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "bad_env_file")

    def test_env_file_must_match_stack(self) -> None:
        doc = _doc(stack="other-stack")
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "bad_env_file")

    def test_repo_full_name_must_match_catalog(self) -> None:
        doc = _doc(repo_full_name="attacker/qurbot")
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "repo_mismatch")

    def test_project_key_not_in_catalog(self) -> None:
        project = _qurbot_project()
        project["repo_full_name"] = "muradjanov-dev/unknown-project"
        project["stack"] = "unknown-project"
        project["env_file"] = "/srv/stack/env/unknown-project.env"
        doc = {"version": 1, "projects": {"unknown-project": project}}
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "repo_mismatch")

    def test_containers_not_in_catalog(self) -> None:
        doc = _doc(
            containers=[
                ["qurbot-web", "ghcr.io/evil/qurbot"],
                ["qurbot-worker", "ghcr.io/muradjanov-dev/qurbot"],
            ]
        )
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "container_not_in_catalog")

    def test_containers_must_be_non_empty(self) -> None:
        doc = _doc(containers=[])
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "bad_containers")

    def test_services_count_bounds(self) -> None:
        doc = _doc(services=[])
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "bad_services")

        doc = _doc(services=["a", "b", "c", "d", "e"])
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "bad_services")

    def test_service_name_regex(self) -> None:
        doc = _doc(services=["Bad Name"])
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "bad_services")

    def test_duplicate_services_rejected(self) -> None:
        doc = _doc(services=["qurbot-web", "qurbot-web"])
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "bad_services")

    def test_ready_url_null_is_valid(self) -> None:
        allowlist = _parse(_doc(ready_url=None))
        self.assertIsNone(allowlist.projects["qurbot"].ready_url)

    def test_ready_url_valid_local_url(self) -> None:
        allowlist = _parse(_doc(ready_url="http://127.0.0.1:8080/ready"))
        self.assertEqual(allowlist.projects["qurbot"].ready_url, "http://127.0.0.1:8080/ready")

    def test_ready_url_rejects_remote_host(self) -> None:
        doc = _doc(ready_url="http://example.com/ready")
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "bad_ready_url")

    def test_ready_url_rejects_missing_path(self) -> None:
        doc = _doc(ready_url="http://127.0.0.1:8080")
        with self.assertRaises(PolicyError) as ctx:
            _parse(doc)
        self.assertEqual(ctx.exception.args[0], "bad_ready_url")

    def test_key_regex_rejects_lowercase(self) -> None:
        project = _qurbot_project()
        project["keys"] = {
            "admin_tg_ids": project["keys"]["ADMIN_TG_IDS"],
        }
        with self.assertRaises(PolicyError) as ctx:
            _parse({"version": 1, "projects": {"qurbot": project}})
        self.assertEqual(ctx.exception.args[0], "bad_key")

    def test_scalar_ops_must_be_exactly_replace(self) -> None:
        project = _qurbot_project()
        project["keys"] = {
            "FEATURE_FLAG": {
                "format": "scalar",
                "ops": ["list_add"],
                "value_re": "on|off",
                "description": "toggle",
            }
        }
        with self.assertRaises(PolicyError) as ctx:
            _parse({"version": 1, "projects": {"qurbot": project}})
        self.assertEqual(ctx.exception.args[0], "bad_ops")

    def test_list_ops_reject_replace(self) -> None:
        project = _qurbot_project()
        project["keys"]["ADMIN_TG_IDS"]["ops"] = ["replace"]
        with self.assertRaises(PolicyError) as ctx:
            _parse({"version": 1, "projects": {"qurbot": project}})
        self.assertEqual(ctx.exception.args[0], "bad_ops")

    def test_max_items_bounds(self) -> None:
        for bad in (0, 201, True):
            with self.subTest(bad=bad):
                project = _qurbot_project()
                project["keys"]["ADMIN_TG_IDS"]["max_items"] = bad
                with self.assertRaises(PolicyError) as ctx:
                    _parse({"version": 1, "projects": {"qurbot": project}})
                self.assertEqual(ctx.exception.args[0], "bad_max_items")

    def test_protected_items_must_match_item_re(self) -> None:
        project = _qurbot_project()
        project["keys"]["ADMIN_TG_IDS"]["protected_items"] = [1]  # too short for item_re
        with self.assertRaises(PolicyError) as ctx:
            _parse({"version": 1, "projects": {"qurbot": project}})
        self.assertEqual(ctx.exception.args[0], "bad_protected_items")

    def test_description_length_and_printable(self) -> None:
        project = _qurbot_project()
        project["keys"]["ADMIN_TG_IDS"]["description"] = "x" * 201
        with self.assertRaises(PolicyError) as ctx:
            _parse({"version": 1, "projects": {"qurbot": project}})
        self.assertEqual(ctx.exception.args[0], "bad_description")

        project = _qurbot_project()
        project["keys"]["ADMIN_TG_IDS"]["description"] = "bad\x00desc"
        with self.assertRaises(PolicyError) as ctx:
            _parse({"version": 1, "projects": {"qurbot": project}})
        self.assertEqual(ctx.exception.args[0], "bad_description")

    def test_bad_item_re_syntax(self) -> None:
        project = _qurbot_project()
        project["keys"]["ADMIN_TG_IDS"]["item_re"] = "[unterminated"
        with self.assertRaises(PolicyError) as ctx:
            _parse({"version": 1, "projects": {"qurbot": project}})
        self.assertEqual(ctx.exception.args[0], "bad_item_re")

    def test_item_re_compiled_with_ascii_rejects_unicode_digit(self) -> None:
        project = _qurbot_project()
        project["keys"]["ADMIN_TG_IDS"]["item_re"] = r"\d+"
        allowlist = _parse({"version": 1, "projects": {"qurbot": project}})
        item_re = allowlist.projects["qurbot"].keys["ADMIN_TG_IDS"].item_re
        assert item_re is not None
        self.assertTrue(item_re.fullmatch("123"))
        self.assertFalse(item_re.fullmatch("٥٥٥"))  # Arabic-Indic digits

    def test_dataclasses_are_frozen(self) -> None:
        allowlist = _parse(_doc())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            allowlist.version = 2  # type: ignore[misc]
        project = allowlist.projects["qurbot"]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            project.stack = "x"  # type: ignore[misc]
        key_policy = project.keys["ADMIN_TG_IDS"]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            key_policy.description = "x"  # type: ignore[misc]


class RealCatalogTests(unittest.TestCase):
    """The one test the spec explicitly asks for against the real catalog."""

    def test_spec_example_parses_against_the_real_agent_repos_catalog(self) -> None:
        allowlist = parse_allowlist(_doc(), REAL_REPOSITORIES)
        project = allowlist.projects["qurbot"]
        self.assertEqual(project.repo_full_name, "muradjanov-dev/qurbot")
        self.assertEqual(
            set(project.containers),
            {
                ("qurbot-web", "ghcr.io/muradjanov-dev/qurbot"),
                ("qurbot-worker", "ghcr.io/muradjanov-dev/qurbot"),
            },
        )

    def test_task_manager_project_key_is_denied_before_any_catalog_lookup(self) -> None:
        # Even though "task-manager" *is* in the real catalog, it must never
        # be reachable as an ops project.
        project = _qurbot_project()
        project["repo_full_name"] = "Asadtop4ik/task-manager"
        project["stack"] = "task-manager"
        project["env_file"] = "/srv/stack/env/task-manager.env"
        doc = {"version": 1, "projects": {"task-manager": project}}
        with self.assertRaises(PolicyError) as ctx:
            parse_allowlist(doc, REAL_REPOSITORIES)
        self.assertEqual(ctx.exception.args[0], "project_denied")


class LoadAllowlistFileTests(unittest.TestCase):
    def _write(self, directory: str, name: str, doc: dict, *, mode: int = 0o644) -> str:
        target = os.path.join(directory, name)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        os.chmod(target, mode)
        return target

    def test_loads_a_well_formed_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "allow.json", _doc())
            allowlist = load_allowlist(path, FAKE_REPOSITORIES, require_root_owned=False)
            self.assertIn("qurbot", allowlist.projects)

    def test_refuses_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = self._write(directory, "target.json", _doc())
            link = os.path.join(directory, "link.json")
            os.symlink(target, link)
            with self.assertRaises(PolicyError) as ctx:
                load_allowlist(link, FAKE_REPOSITORIES, require_root_owned=False)
            self.assertEqual(ctx.exception.args[0], "symlink")

    def test_refuses_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PolicyError) as ctx:
                load_allowlist(
                    os.path.join(directory, "missing.json"),
                    FAKE_REPOSITORIES,
                    require_root_owned=False,
                )
            self.assertEqual(ctx.exception.args[0], "not_found")

    def test_refuses_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sub = os.path.join(directory, "adir")
            os.mkdir(sub)
            with self.assertRaises(PolicyError) as ctx:
                load_allowlist(sub, FAKE_REPOSITORIES, require_root_owned=False)
            self.assertEqual(ctx.exception.args[0], "not_regular_file")

    def test_refuses_group_writable_even_without_root_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "allow.json", _doc(), mode=0o664)
            with self.assertRaises(PolicyError) as ctx:
                load_allowlist(path, FAKE_REPOSITORIES, require_root_owned=False)
            self.assertEqual(ctx.exception.args[0], "writable_by_group_or_other")

    def test_refuses_world_writable_even_without_root_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "allow.json", _doc(), mode=0o646)
            with self.assertRaises(PolicyError) as ctx:
                load_allowlist(path, FAKE_REPOSITORIES, require_root_owned=False)
            self.assertEqual(ctx.exception.args[0], "writable_by_group_or_other")

    def test_refuses_non_root_owned_file_when_required(self) -> None:
        # This test process is not root (CI and dev machines both run
        # unprivileged), so the default `require_root_owned=True` must
        # refuse a file this same process owns.
        self.assertNotEqual(os.geteuid(), 0, "this test must run as a non-root user")
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "allow.json", _doc())
            with self.assertRaises(PolicyError) as ctx:
                load_allowlist(path, FAKE_REPOSITORIES)  # require_root_owned defaults True
            self.assertEqual(ctx.exception.args[0], "not_root_owned")

    def test_invalid_json_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "allow.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            os.chmod(path, 0o644)
            with self.assertRaises(PolicyError) as ctx:
                load_allowlist(path, FAKE_REPOSITORIES, require_root_owned=False)
            self.assertEqual(ctx.exception.args[0], "invalid_json")


if __name__ == "__main__":
    unittest.main()
