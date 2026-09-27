from __future__ import annotations

import base64
import io
import json
import unittest

from agent_svc.log import Logger, Redactor


class RedactorTests(unittest.TestCase):
    def test_masks_exact_secret_value(self) -> None:
        redactor = Redactor(["super-secret-token"])
        self.assertEqual(
            redactor.redact("Authorization: super-secret-token in the clear"),
            "Authorization: [REDACTED] in the clear",
        )

    def test_masks_longer_secret_before_shorter_substring(self) -> None:
        redactor = Redactor(["abc", "abcdef"])
        self.assertEqual(redactor.redact("token=abcdef"), "token=[REDACTED]")

    def test_masks_github_token_shapes(self) -> None:
        redactor = Redactor([])
        cases = [
            "ghp_" + "a" * 36,
            "github_pat_" + "b" * 30,
            "sk-" + "c" * 40,
        ]
        for secret in cases:
            with self.subTest(secret=secret[:10]):
                self.assertNotIn(secret, redactor.redact(f"leaked: {secret}"))

    def test_masks_bearer_header(self) -> None:
        redactor = Redactor([])
        result = redactor.redact("Authorization: Bearer abc.def-123")
        self.assertNotIn("abc.def-123", result)

    def test_masks_jwt_shape(self) -> None:
        redactor = Redactor([])
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PYE"
        self.assertNotIn(token, redactor.redact(f"jwt={token}"))

    def test_redact_value_recurses_into_dict_and_list(self) -> None:
        redactor = Redactor(["s3cr3t"])
        value = {"a": ["ok", "has s3cr3t here"], "b": {"c": "s3cr3t"}}
        redacted = redactor.redact_value(value)
        self.assertEqual(redacted["a"][1], "has [REDACTED] here")
        self.assertEqual(redacted["b"]["c"], "[REDACTED]")

    def test_non_string_values_pass_through(self) -> None:
        redactor = Redactor(["s3cr3t"])
        self.assertEqual(redactor.redact_value(42), 42)
        self.assertIsNone(redactor.redact_value(None))

    def test_masks_base64_of_bare_token(self) -> None:
        token = "the-github-token-value"
        redactor = Redactor([token])
        blob = base64.b64encode(token.encode()).decode()
        self.assertNotIn(blob, redactor.redact(f"leaked b64: {blob}"))

    def test_masks_base64_of_x_access_token_form(self) -> None:
        # MirrorManager's git http.extraHeader value: base64(x-access-token:TOKEN).
        token = "the-github-token-value"
        redactor = Redactor([token])
        blob = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        self.assertNotIn(blob, redactor.redact(f"AUTHORIZATION: basic {blob}"))

    def test_masks_basic_auth_pattern_even_for_an_unknown_secret(self) -> None:
        redactor = Redactor([])  # no loaded secrets at all
        result = redactor.redact("AUTHORIZATION: Basic dW5rbm93bjpzZWNyZXQ=")
        self.assertNotIn("dW5rbm93bjpzZWNyZXQ=", result)

    def test_never_masks_the_ordinary_english_word_basic(self) -> None:
        redactor = Redactor([])
        text = "This is a basic setup with a basic auth fallback for legacy clients."
        self.assertEqual(redactor.redact(text), text)


class LoggerTests(unittest.TestCase):
    def _logger(self, secrets: list[str]) -> tuple[Logger, io.StringIO]:
        stream = io.StringIO()
        logger = Logger(Redactor(secrets), stream=stream)
        return logger, stream

    def test_event_writes_one_json_line_with_expected_fields(self) -> None:
        logger, stream = self._logger([])
        logger.event(
            "leased", lane="code", run_id="r-1", task_id=7, stage="leased", duration_ms=12.5
        )
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["event"], "leased")
        self.assertEqual(record["lane"], "code")
        self.assertEqual(record["run_id"], "r-1")
        self.assertEqual(record["task_id"], 7)
        self.assertEqual(record["stage"], "leased")
        self.assertEqual(record["duration_ms"], 12.5)
        self.assertEqual(record["level"], "info")
        self.assertIn("ts", record)

    def test_event_omits_unset_optional_fields(self) -> None:
        logger, stream = self._logger([])
        logger.event("idle")
        record = json.loads(stream.getvalue())
        self.assertNotIn("lane", record)
        self.assertNotIn("run_id", record)

    def test_event_redacts_extra_string_fields(self) -> None:
        logger, stream = self._logger(["top-secret-value"])
        logger.event("callback_sent", detail="token was top-secret-value in the body")
        record = json.loads(stream.getvalue())
        self.assertNotIn("top-secret-value", record["detail"])

    def test_error_sets_type_and_truncates_to_500_chars(self) -> None:
        logger, stream = self._logger([])
        exc = ValueError("x" * 1000)
        logger.error(exc, lane="watch")
        record = json.loads(stream.getvalue())
        self.assertEqual(record["error_type"], "ValueError")
        self.assertEqual(len(record["error"]), 500)
        self.assertEqual(record["level"], "error")
        self.assertEqual(record["lane"], "watch")

    def test_error_redacts_secret_inside_exception_message(self) -> None:
        logger, stream = self._logger(["hunter2"])
        exc = RuntimeError("failed with token hunter2 attached")
        logger.error(exc)
        record = json.loads(stream.getvalue())
        self.assertNotIn("hunter2", record["error"])

    def test_final_serialized_line_catches_what_default_str_would_otherwise_leak(
        self,
    ) -> None:
        secret = "leak-me-1234567890"

        class _Weird:
            def __str__(self) -> str:
                return f"wrapped({secret})"

        logger, stream = self._logger([secret])
        logger.event("odd_value", detail=_Weird())
        self.assertNotIn(secret, stream.getvalue())

    def test_final_serialized_line_catches_a_secret_used_as_a_dict_key(self) -> None:
        secret = "key-shaped-secret-value"
        logger, stream = self._logger([secret])
        logger.event("odd_key", payload={secret: "value"})
        self.assertNotIn(secret, stream.getvalue())

    def test_writes_are_flushed_immediately(self) -> None:
        logger, stream = self._logger([])
        logger.event("a")
        logger.event("b")
        self.assertEqual(len(stream.getvalue().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
