"""Every `--output-schema` we send must satisfy strict structured outputs."""

from __future__ import annotations

import unittest
from typing import Any

from agent_svc.intake import OUTPUT_SCHEMA as INTAKE_SCHEMA
from agent_svc.review import REVIEW_OUTPUT_SCHEMA


def _violations(schema: Any, path: str = "$") -> list[str]:
    problems: list[str] = []
    if not isinstance(schema, dict):
        return problems
    types = schema.get("type")
    is_object = types == "object" or (isinstance(types, list) and "object" in types)
    if is_object:
        if schema.get("additionalProperties") is not False:
            problems.append(f"{path}: additionalProperties must be false")
        props = schema.get("properties", {})
        if sorted(schema.get("required", [])) != sorted(props):
            problems.append(f"{path}: every property must be required")
        for name, sub in props.items():
            problems += _violations(sub, f"{path}.{name}")
    if "items" in schema:
        problems += _violations(schema["items"], f"{path}[]")
    for key in ("anyOf", "oneOf"):
        for index, sub in enumerate(schema.get(key, [])):
            problems += _violations(sub, f"{path}.{key}[{index}]")
    return problems


class StrictSchemaTests(unittest.TestCase):
    def test_review_schema_is_strict(self) -> None:
        self.assertEqual(_violations(REVIEW_OUTPUT_SCHEMA), [])

    def test_intake_schema_is_strict(self) -> None:
        self.assertEqual(_violations(INTAKE_SCHEMA), [])

    def test_checker_catches_the_production_bug(self) -> None:
        loose = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
        self.assertTrue(_violations(loose))


class CodexErrorEventTests(unittest.TestCase):
    def test_error_event_message_is_extracted(self) -> None:
        from agent_svc.codex import _event_error_message

        self.assertEqual(_event_error_message({"type": "error", "message": "boom"}), "boom")
        self.assertEqual(
            _event_error_message({"type": "turn.failed", "error": {"message": "limit"}}),
            "limit",
        )
        self.assertEqual(_event_error_message({"type": "error"}), "")


if __name__ == "__main__":
    unittest.main()
