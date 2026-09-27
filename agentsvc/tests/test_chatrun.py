from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_svc.chatrun import ChatRun

from .support import FakeCodexRunner, build_test_context


class ChatRunTests(unittest.TestCase):
    def test_enter_registers_cancel_with_the_service_wide_registry(self) -> None:
        with TemporaryDirectory() as tmp_str:
            ctx = build_test_context(Path(tmp_str))
            with ChatRun(ctx) as run:
                self.assertIn(run.cancel, ctx.cancel_registry._events)  # type: ignore[attr-defined]
            self.assertNotIn(run.cancel, ctx.cancel_registry._events)  # type: ignore[attr-defined]

    def test_exit_cleans_up_codex_and_removes_images_even_on_a_raised_exception(self) -> None:
        codex = FakeCodexRunner()
        with TemporaryDirectory() as tmp_str:
            ctx = build_test_context(Path(tmp_str), codex=codex)
            with self.assertRaises(RuntimeError), ChatRun(ctx) as run:
                images_dir = run.run_dir / "images"
                (images_dir / "leftover.png").write_bytes(b"x")
                self.assertTrue(images_dir.is_dir())
                raise RuntimeError("simulated handler failure")
            # __exit__ ran despite the exception: cleanup was still called,
            # the images directory (sensitive downloaded attachments) is
            # gone, and cancel was unregistered -- the exception itself
            # still propagates (this test's own assertRaises proves that).
            self.assertEqual(len(codex.cleanup_calls), 1)
            self.assertEqual(codex.cleanup_calls[0]["run_id"], run.run_id)
            self.assertFalse(images_dir.is_dir())
            self.assertNotIn(run.cancel, ctx.cancel_registry._events)  # type: ignore[attr-defined]

    def test_a_codex_cleanup_failure_is_logged_and_never_masks_the_real_exception(
        self,
    ) -> None:
        class _FailingCleanupCodexRunner(FakeCodexRunner):
            def cleanup(self, request, **kwargs):
                raise RuntimeError("cleanup itself is broken")

        codex = _FailingCleanupCodexRunner()
        with TemporaryDirectory() as tmp_str:
            ctx = build_test_context(Path(tmp_str), codex=codex)
            with self.assertRaises(ValueError), ChatRun(ctx):
                raise ValueError("the real failure")

    def test_remaining_s_counts_down_and_floors_above_zero(self) -> None:
        with TemporaryDirectory() as tmp_str:
            ctx = build_test_context(Path(tmp_str))
            with ChatRun(ctx) as run:
                first = run.remaining_s()
                self.assertGreater(first, 0.0)
                self.assertLessEqual(first, 270.0)


if __name__ == "__main__":
    unittest.main()
