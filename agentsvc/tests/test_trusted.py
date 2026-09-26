from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_svc.trusted import TrustedModules

from .support import copy_trusted_dir


class TrustedModulesTests(unittest.TestCase):
    def test_every_accessor_loads_the_expected_module(self) -> None:
        with TemporaryDirectory() as tmp:
            trusted_dir = copy_trusted_dir(Path(tmp) / "trusted")
            trusted = TrustedModules(trusted_dir)
            self.assertTrue(hasattr(trusted.agent_task, "parse_task"))
            self.assertTrue(hasattr(trusted.public_agent_task, "parse_public_task"))
            self.assertTrue(hasattr(trusted.agent_preflight, "run"))
            self.assertTrue(hasattr(trusted.agent_release, "correction_prompt"))
            self.assertTrue(hasattr(trusted.agent_images, "download_images"))
            self.assertTrue(hasattr(trusted.agent_pr_review, "build_review_prompt"))
            self.assertTrue(hasattr(trusted.agent_repos, "REPOSITORIES"))

    def test_agent_pr_review_loads_for_real_with_everything_the_review_lane_needs(
        self,
    ) -> None:
        """`agent_svc.review.handle_review` calls `trusted.agent_pr_review.
        build_review_prompt`/`_current_pr`/`_parse_result`/`_review_decision`
        directly (see `review.py`). Without this accessor the review lane
        crashes with `AttributeError` the first time it leases work -- this
        loads the REAL trusted script (copied flat, exactly as
        `install_agent_svc.sh` lays out `trusted_dir` in production) and
        proves every one of those names is present and callable-shaped.
        """
        with TemporaryDirectory() as tmp:
            trusted_dir = copy_trusted_dir(Path(tmp) / "trusted")
            trusted = TrustedModules(trusted_dir)
            module = trusted.agent_pr_review
            for name in (
                "build_review_prompt",
                "_current_pr",
                "_parse_result",
                "_review_decision",
                "prepare",
                "finalize",
                "fail",
            ):
                attr = getattr(module, name, None)
                self.assertTrue(callable(attr), f"agent_pr_review.{name} is not callable")
            # A real, minimal call: proves the module is genuinely executable,
            # not just present as an attribute.
            prompt = module.build_review_prompt(
                "Asadtop4ik/task-manager", 1, "a" * 40, "diff --git a/x b/x\n"
            )
            self.assertIn("Asadtop4ik/task-manager", prompt)

    def test_repeated_access_returns_the_same_cached_module_object(self) -> None:
        with TemporaryDirectory() as tmp:
            trusted_dir = copy_trusted_dir(Path(tmp) / "trusted")
            trusted = TrustedModules(trusted_dir)
            self.assertIs(trusted.agent_task, trusted.agent_task)
            # A second `TrustedModules` over the *same* directory also shares
            # the cache (keyed by resolved file path, not by instance).
            self.assertIs(trusted.agent_task, TrustedModules(trusted_dir).agent_task)

    def test_missing_file_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            trusted = TrustedModules(tmp)
            with self.assertRaises(FileNotFoundError):
                _ = trusted.agent_task

    def test_public_agent_task_resolves_its_bare_imports_from_trusted_dir_only(self) -> None:
        """A malicious `scripts/`-shaped dir earlier on `sys.path` must never win.

        `public_agent_task.py` does `from agent_repos import ...` /
        `from agent_task import ...` internally. This plants a fake
        `agent_repos.py`/`agent_task.py` pair on `sys.path` *before* the real
        trusted directory is ever consulted, then proves the loaded
        `public_agent_task` still carries the real catalog, not the fake one.
        """
        with TemporaryDirectory() as tmp:
            malicious_dir = Path(tmp) / "malicious"
            malicious_dir.mkdir()
            (malicious_dir / "agent_repos.py").write_text(
                "class _Fake:\n"
                "    full_name = 'evil/owned'\n"
                "QA_REPOSITORY = _Fake()\n"
                "def public_catalog():\n"
                "    return ()\n",
                encoding="utf-8",
            )
            (malicious_dir / "agent_task.py").write_text(
                "def check_diff(*a, **k):\n"
                "    raise AssertionError('malicious check_diff must never run')\n",
                encoding="utf-8",
            )
            trusted_dir = copy_trusted_dir(Path(tmp) / "trusted")

            sys.path.insert(0, str(malicious_dir))
            try:
                trusted = TrustedModules(trusted_dir)
                public_agent_task = trusted.public_agent_task
            finally:
                sys.path.remove(str(malicious_dir))

            # The real catalog's QA repository, never the planted fake.
            self.assertEqual(public_agent_task.QA_REPOSITORY.full_name, "Asadtop4ik/agent-qa")
            # The real `agent_task.check_diff`, not the malicious stub.
            with self.assertRaises(TypeError):
                # The real check_diff requires a `cwd`-shaped call; the
                # malicious stub would instead raise AssertionError.
                public_agent_task.check_base_diff(cwd=None, image_dir="/x")


if __name__ == "__main__":
    unittest.main()
