from __future__ import annotations

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
    ORCHESTRATOR_RULES,
    compose_correction_prompt,
    compose_implement_prompt,
    is_complex,
    route_correction,
    route_implement,
)


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

    def test_explicit_simple_even_on_a_retry(self) -> None:
        self.assertFalse(is_complex(_work(complexity="simple", attempt_index=5)))

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
