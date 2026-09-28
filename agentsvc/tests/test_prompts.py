from __future__ import annotations

import types
import unittest
from datetime import UTC, datetime

from agent_svc.api import Work
from agent_svc.config import (
    DEFAULT_MODEL_MATRIX,
    DEFAULT_TIMEOUTS,
    build_settings,
    load_config,
)
from agent_svc.prompts import (
    OPS_TRAILER_EXAMPLE,
    ORCHESTRATOR_RULES,
    compose_correction_prompt,
    compose_implement_prompt,
    is_complex,
    route_correction,
    route_implement,
)


def _fake_ops_project(**keys: tuple[tuple[str, ...], str]) -> object:
    """A minimal stand-in for one `agent_ops_policy.ProjectPolicy`: only
    `.keys` (mapping name -> an object with `.ops`/`.description`) is ever
    read by `prompts.py`, which has no import on the real trusted module.
    Each kwarg value is `(ops_tuple, description)`."""
    policies = {
        name: types.SimpleNamespace(ops=ops, description=description)
        for name, (ops, description) in keys.items()
    }
    return types.SimpleNamespace(keys=policies)


def _settings():
    config = load_config(None)
    secrets = {
        "agent_svc_token": "a",
        "callback_token": "b",
        "intake_worker_token": "c",
        "github_agent_token": "d",
        "github_public_agent_token": "e",
        "github_qa_token": "f",
    }
    return build_settings(config, secrets)


def _work(*, complexity=None, attempt_index=1, relevant_files=()):
    return Work(
        run_id="11111111-1111-1111-1111-111111111111",
        kind="implement",
        lease_id="lease-1",
        lease_until=datetime.now(UTC),
        attempts=1,
        attempt_index=attempt_index,
        task_id=7,
        task_revision="rev",
        repo_full_name="Owner/repo",
        base_branch="main",
        mode="pr",
        title="t",
        description="d",
        image_count=0,
        complexity=complexity,
        relevant_files=tuple(relevant_files),
        branch=None,
        pr_url=None,
        pr_number=None,
        head_sha=None,
        action_id=None,
        instruction=None,
        expected_head_sha=None,
    )


class IsComplexTests(unittest.TestCase):
    def test_explicit_complex(self) -> None:
        self.assertTrue(is_complex(_work(complexity="complex", attempt_index=1)))

    def test_attempt_two_is_complex_even_with_an_explicit_simple_hint(self) -> None:
        # A "simple" route already failed once; it never gets an identical
        # second try -- attempt_index >= 2 always routes to the complex
        # model, regardless of the complexity hint.
        self.assertTrue(is_complex(_work(complexity="simple", attempt_index=5)))

    def test_explicit_simple_on_the_first_attempt_stays_simple(self) -> None:
        self.assertFalse(is_complex(_work(complexity="simple", attempt_index=1)))

    def test_null_complexity_first_attempt_is_simple(self) -> None:
        self.assertFalse(is_complex(_work(complexity=None, attempt_index=1)))

    def test_null_complexity_second_attempt_is_complex(self) -> None:
        self.assertTrue(is_complex(_work(complexity=None, attempt_index=2)))


class RouteImplementTests(unittest.TestCase):
    def test_simple_route_uses_luna_high_single_agent(self) -> None:
        route = route_implement(_work(complexity="simple"), _settings())
        expected = DEFAULT_MODEL_MATRIX["implement_simple"]
        self.assertEqual(route.model, expected["model"])
        self.assertEqual(route.effort, expected["effort"])
        self.assertFalse(route.multi_agent)
        self.assertEqual(route.timeout_s, DEFAULT_TIMEOUTS["implement_simple"])
        self.assertFalse(route.complex)

    def test_complex_route_uses_sol_multi_agent(self) -> None:
        route = route_implement(_work(complexity="complex"), _settings())
        expected = DEFAULT_MODEL_MATRIX["implement_complex"]
        self.assertEqual(route.model, expected["model"])
        self.assertTrue(route.multi_agent)
        self.assertEqual(route.timeout_s, DEFAULT_TIMEOUTS["implement_complex"])
        self.assertTrue(route.complex)

    def test_attempt_two_routes_complex_even_without_a_complexity_hint(self) -> None:
        route = route_implement(_work(complexity=None, attempt_index=2), _settings())
        self.assertTrue(route.complex)


class RouteCorrectionTests(unittest.TestCase):
    def test_simple_correction_uses_the_single_correction_timeout(self) -> None:
        route = route_correction(_work(complexity="simple"), _settings())
        self.assertEqual(route.timeout_s, DEFAULT_TIMEOUTS["correction"])
        self.assertFalse(route.multi_agent)

    def test_complex_correction_still_uses_the_single_correction_timeout(self) -> None:
        route = route_correction(_work(complexity="complex"), _settings())
        self.assertEqual(route.timeout_s, DEFAULT_TIMEOUTS["correction"])
        self.assertTrue(route.multi_agent)


class ComposeImplementPromptTests(unittest.TestCase):
    def test_relevant_files_are_listed_when_present(self) -> None:
        prompt = compose_implement_prompt(
            "BASE",
            _work(relevant_files=["backend/app/main.py", "bot/app/main.py"]),
            complex_route=False,
        )
        self.assertIn("Start from these files:", prompt)
        self.assertIn("- backend/app/main.py", prompt)
        self.assertIn("- bot/app/main.py", prompt)

    def test_no_relevant_files_section_when_empty(self) -> None:
        prompt = compose_implement_prompt(
            "BASE", _work(relevant_files=()), complex_route=False
        )
        self.assertNotIn("Start from these files:", prompt)

    def test_efficiency_rules_always_present(self) -> None:
        prompt = compose_implement_prompt("BASE", _work(), complex_route=False)
        self.assertIn("Efficiency rules:", prompt)

    def test_orchestrator_rules_only_on_complex_route(self) -> None:
        simple_prompt = compose_implement_prompt("BASE", _work(), complex_route=False)
        complex_prompt = compose_implement_prompt("BASE", _work(), complex_route=True)
        self.assertNotIn(ORCHESTRATOR_RULES, simple_prompt)
        self.assertIn(ORCHESTRATOR_RULES, complex_prompt)
        self.assertIn("luna_worker", complex_prompt)


class ComposeImplementPromptOpsRulesTests(unittest.TestCase):
    def test_ops_trailer_example_matches_the_parser_marker_prefix(self) -> None:
        from agent_svc.ops_requests import MARKER_PREFIX

        self.assertTrue(OPS_TRAILER_EXAMPLE.startswith(MARKER_PREFIX))

    def test_no_ops_rules_when_project_has_no_allowlist_entry(self) -> None:
        prompt = compose_implement_prompt(
            "BASE", _work(), complex_route=False, ops_project=None
        )
        self.assertNotIn("AGENT_OPS_REQUESTS", prompt)
        self.assertNotIn("Ops requests:", prompt)

    def test_ops_rules_present_and_never_include_values(self) -> None:
        ops_project = _fake_ops_project(
            ADMIN_TG_IDS=(("list_add", "list_remove"), "Telegram admin IDs"),
            SUPER_ADMIN_TG_IDS=(("list_add",), "Telegram super-admin IDs"),
        )
        prompt = compose_implement_prompt(
            "BASE", _work(), complex_route=False, ops_project=ops_project
        )
        self.assertIn("Ops requests:", prompt)
        self.assertIn("ADMIN_TG_IDS", prompt)
        self.assertIn("Telegram admin IDs", prompt)
        self.assertIn("SUPER_ADMIN_TG_IDS", prompt)
        self.assertIn("list_add/list_remove", prompt)
        self.assertIn(OPS_TRAILER_EXAMPLE, prompt)
        self.assertIn("at most 3", prompt)
        # No key name looks anything like a real Telegram id / secret value:
        # only names, ops, and descriptions were ever handed to this prompt.
        self.assertNotIn("5339875840", prompt)
        self.assertNotIn("917456291", prompt)

    def test_ops_rules_combine_with_orchestrator_rules_on_complex_route(self) -> None:
        ops_project = _fake_ops_project(FOO=(("replace",), "a flag"))
        prompt = compose_implement_prompt(
            "BASE", _work(), complex_route=True, ops_project=ops_project
        )
        self.assertIn(ORCHESTRATOR_RULES, prompt)
        self.assertIn("Ops requests:", prompt)


class ComposeCorrectionPromptTests(unittest.TestCase):
    def test_simple_has_no_orchestrator_rules(self) -> None:
        prompt = compose_correction_prompt("BASE", complex_route=False)
        self.assertIn("BASE", prompt)
        self.assertIn("Efficiency rules:", prompt)
        self.assertNotIn(ORCHESTRATOR_RULES, prompt)

    def test_complex_has_orchestrator_rules(self) -> None:
        prompt = compose_correction_prompt("BASE", complex_route=True)
        self.assertIn(ORCHESTRATOR_RULES, prompt)


if __name__ == "__main__":
    unittest.main()
