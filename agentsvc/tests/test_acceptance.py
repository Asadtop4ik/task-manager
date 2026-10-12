from __future__ import annotations

import unittest

from agent_svc import acceptance
from agent_svc.acceptance import (
    AcceptanceError,
    Section,
    acceptance_sections,
    count_added_tests,
    gate_report,
    splice,
    split_sections,
    strip_acceptance,
    validate_author_patch,
)

DIR = "tests/acceptance"
PROFILE = {"profile": "unittest", "dir": DIR}


def _new_file(path: str, body: str, *, mode: str = "100644") -> bytes:
    lines = body.splitlines()
    text = f"diff --git a/{path} b/{path}\nnew file mode {mode}\nindex 0000000..1234567\n"
    if lines:
        text += f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
        text += "".join(f"+{line}\n" for line in lines)
    return text.encode()


def _modify(path: str, old: str, new: str) -> bytes:
    return (
        f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"
    ).encode()


def _delete(path: str) -> bytes:
    return (
        f"diff --git a/{path} b/{path}\ndeleted file mode 100644\n"
        f"index 1111111..0000000\n--- a/{path}\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    ).encode()


def _binary(path: str) -> bytes:
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
        "index 0000000..1234567\nGIT binary patch\nliteral 4\nLcmZQz00001\n\nliteral 0\n"
        "HcmV?d00001\n\n"
    ).encode()


TEST_BODY = (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_ac1_x(self):\n"
    "        self.assertEqual(1, 1)\n"
    "    def test_ac2_y(self):\n"
    "        self.assertEqual(2, 2)\n"
    "    def helper(self):\n"
    "        pass\n"
)
TASK = f"{DIR}/test_task_7.py"
INIT = f"{DIR}/__init__.py"


class SplitSectionsTests(unittest.TestCase):
    def test_roundtrip_and_paths(self) -> None:
        patch = _new_file("a.py", "x = 1\n") + _modify("b/c.py", "a", "b") + _delete("d.txt")
        sections = split_sections(patch)
        self.assertEqual(b"".join(s.data for s in sections), patch)
        self.assertEqual([s.paths for s in sections], [("a.py",), ("b/c.py",), ("d.txt",)])

    def test_empty_and_preamble(self) -> None:
        self.assertEqual(split_sections(b""), [])
        sections = split_sections(b"junk line\n" + _new_file("a.py", "x"))
        self.assertEqual(sections[0], Section((), b"junk line\n"))
        self.assertEqual(sections[1].paths, ("a.py",))

    def test_binary_section_and_following_section(self) -> None:
        patch = _binary(f"{DIR}/blob.bin") + _new_file("z.py", "y = 2\n")
        sections = split_sections(patch)
        self.assertEqual([s.paths for s in sections], [(f"{DIR}/blob.bin",), ("z.py",)])
        self.assertIn(b"GIT binary patch", sections[0].data)

    def test_added_line_that_looks_like_a_header_does_not_split(self) -> None:
        body = "x = 'diff --git a/tests/acceptance/y.py b/tests/acceptance/y.py'\n"
        patch = _new_file("a.py", body)
        patch = patch.replace(b"+x = ", b"+diff --git a/q b/q\n+x = ")  # body line, '+'-prefixed
        self.assertEqual(len(split_sections(patch)), 1)

    def test_rename_header_yields_both_paths(self) -> None:
        patch = b"diff --git a/old.py b/tests/acceptance/new.py\nsimilarity index 100%\n"
        self.assertEqual(split_sections(patch)[0].paths, ("old.py", "tests/acceptance/new.py"))

    def test_quoted_paths(self) -> None:
        patch = b'diff --git "a/tests/acceptance/t\\303\\251st.py" "b/tests/acceptance/t\\303\\251st.py"\n'
        paths = split_sections(patch)[0].paths
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].startswith("tests/acceptance/"))

    def test_paths_with_spaces_same_both_sides(self) -> None:
        patch = b"diff --git a/tests/acceptance/a b.py b/tests/acceptance/a b.py\n"
        self.assertEqual(split_sections(patch)[0].paths, ("tests/acceptance/a b.py",))

    def test_unparseable_header_has_no_paths(self) -> None:
        self.assertEqual(split_sections(b"diff --git nonsense\n")[0].paths, ())


class StripAndSpliceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pinned = _new_file(INIT, "") + _new_file(TASK, TEST_BODY)
        self.code = _new_file("app/x.py", "y = 1\n")

    def test_strip_and_extract_acceptance_sections(self) -> None:
        patch = self.code + self.pinned + _delete("docs/old.md")
        stripped = strip_acceptance(patch, DIR)
        self.assertEqual(stripped, self.code + _delete("docs/old.md"))
        self.assertEqual(acceptance_sections(patch, DIR), self.pinned)

    def test_lookalike_dirs_are_not_acceptance(self) -> None:
        other = _new_file("docs/tests/acceptance/x.md", "x\n") + _new_file(
            "tests/acceptance_extra/y.py", "y\n"
        )
        self.assertEqual(strip_acceptance(other, DIR), other)
        self.assertEqual(acceptance_sections(other, DIR), b"")

    def test_splice_identical_builder_sections_is_unmodified(self) -> None:
        builder = self.code + self.pinned
        spliced, modified = splice(builder, self.pinned, DIR)
        self.assertFalse(modified)
        self.assertEqual(spliced, self.code + self.pinned)

    def test_splice_when_builder_edited_the_task_file(self) -> None:
        edited = _new_file(TASK, TEST_BODY + "    def test_ac3_cheat(self):\n        pass\n")
        builder = self.code + _new_file(INIT, "") + edited
        spliced, modified = splice(builder, self.pinned, DIR)
        self.assertTrue(modified)
        self.assertEqual(spliced, self.code + self.pinned)
        self.assertNotIn(b"cheat", spliced)

    def test_splice_when_builder_deleted_or_added_acceptance_files(self) -> None:
        spliced, modified = splice(self.code, self.pinned, DIR)  # builder patch lacks them
        self.assertTrue(modified)
        self.assertEqual(spliced, self.code + self.pinned)
        extra = _new_file(f"{DIR}/helpers.py", "z = 1\n") + _binary(f"{DIR}/c.pyc")
        spliced, modified = splice(self.code + self.pinned + extra, self.pinned, DIR)
        self.assertTrue(modified)
        self.assertEqual(spliced, self.code + self.pinned)

    def test_splice_with_only_acceptance_in_builder_patch(self) -> None:
        spliced, modified = splice(self.pinned, self.pinned, DIR)
        self.assertFalse(modified)
        self.assertEqual(spliced, self.pinned)

    def test_splice_keeps_non_acceptance_binary_sections_intact(self) -> None:
        binary = _binary("assets/logo.png")
        spliced, modified = splice(binary + self.pinned, self.pinned, DIR)
        self.assertFalse(modified)
        self.assertEqual(spliced, binary + self.pinned)

    def test_splice_adds_missing_trailing_newline_before_pinned(self) -> None:
        # The appended sections must still start on a fresh line.
        broken = self.code.rstrip(b"\n")
        spliced, _ = splice(broken, self.pinned, DIR)
        self.assertTrue(spliced.endswith(self.pinned))
        self.assertEqual(spliced[: -len(self.pinned)], broken + b"\n")


class ArtifactAndLineBreakTests(unittest.TestCase):
    def test_drop_artifacts_removes_pycache_and_pyc_only(self) -> None:
        keep = _new_file(TASK, TEST_BODY)
        patch = (
            keep
            + _binary(f"{DIR}/__pycache__/test_task_7.cpython-312.pyc")
            + _new_file(f"{DIR}/__pycache__/x.py", "a = 1\n")
            + _binary(f"{DIR}/stray.pyc")
            + _new_file("pycache_notes.md", "x\n")
        )
        self.assertEqual(
            acceptance.drop_artifacts(patch), keep + _new_file("pycache_notes.md", "x\n")
        )

    def test_splice_ignores_bytecode_and_accepts_the_raw_author_patch(self) -> None:
        pinned = _new_file(TASK, TEST_BODY.replace("(1, 1)", "(1, 1)  # reformatted"))
        raw = _new_file(TASK, TEST_BODY)
        code = _new_file("app/x.py", "y = 1\n")
        pyc = _binary(f"{DIR}/__pycache__/t.pyc")
        spliced, modified = splice(code + raw + pyc, pinned, DIR, unmodified=(raw,))
        self.assertFalse(modified)
        self.assertEqual(spliced, code + pinned)
        _, modified = splice(code + raw + pyc, pinned, DIR)
        self.assertTrue(modified)

    def test_carriage_return_does_not_fake_a_section_header(self) -> None:
        patch = _new_file("a.py", "x = 1\n")
        patch = patch.replace(b"+x = 1\n", b"+x = 1\rdiff --git a/tests/acceptance/y.py b/x\n")
        sections = split_sections(patch)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].paths, ("a.py",))
        self.assertEqual(strip_acceptance(patch, DIR), patch)
        # a header after a real newline still splits
        self.assertEqual(len(split_sections(patch + _new_file("b.py", "y\n"))), 2)

    def test_added_test_counting_ignores_carriage_returns(self) -> None:
        body = b"+x = 1\r+    def test_fake(self):\n+    def test_real(self):\n"
        patch = f"diff --git a/{TASK} b/{TASK}\nnew file mode 100644\n@@ -0,0 +1 @@\n".encode() + body
        self.assertEqual(count_added_tests(patch, TASK), 1)


class CountAndValidateTests(unittest.TestCase):
    def test_count_added_tests_only_counts_added_def_test(self) -> None:
        patch = _new_file(TASK, TEST_BODY) + _new_file("other.py", "def test_nope(): pass\n")
        self.assertEqual(count_added_tests(patch, TASK), 2)
        context_line = (
            f"diff --git a/{TASK} b/{TASK}\nindex 1..2 100644\n--- a/{TASK}\n+++ b/{TASK}\n"
            "@@ -1,2 +1,3 @@\n     def test_old(self):\n+    def test_new(self):\n-    def test_gone(self):\n"
        ).encode()
        self.assertEqual(count_added_tests(context_line, TASK), 1)

    def test_valid_author_patch(self) -> None:
        patch = _new_file(INIT, "") + _new_file(TASK, TEST_BODY)
        self.assertEqual(validate_author_patch(patch, profile=PROFILE, task_id=7), (TASK, 2))
        only_task = _new_file(TASK, TEST_BODY)
        self.assertEqual(validate_author_patch(only_task, profile=PROFILE, task_id=7)[1], 2)

    def test_invalid_author_patches(self) -> None:
        good = _new_file(TASK, TEST_BODY)
        cases = {
            "empty": b"",
            "whitespace": b"  \n",
            "other path": good + _new_file("app/x.py", "y = 1\n"),
            "other acceptance file": good + _new_file(f"{DIR}/helpers.py", "y = 1\n"),
            "other task id": _new_file(f"{DIR}/test_task_8.py", TEST_BODY),
            "missing task file": _new_file(INIT, ""),
            "duplicate": good + good,
            "modified not new": _modify(TASK, "a", "def test_x(self):"),
            "symlink mode": _new_file(TASK, TEST_BODY, mode="120000"),
            "exec mode": _new_file(TASK, TEST_BODY, mode="100755"),
            "binary": _binary(TASK),
            "non-empty init": good + _new_file(INIT, "x = 1\n"),
            "no tests": _new_file(TASK, "import unittest\n"),
            "unparseable": b"diff --git nonsense\nnew file mode 100644\n",
            "deleted": _delete(TASK),
            "rename": (
                _new_file(TASK, TEST_BODY)
                + f"diff --git a/{INIT} b/{INIT}\nsimilarity index 100%\n"
                f"rename from {INIT}\nrename to {INIT}\n".encode()
            ),
            "copy": (
                _new_file(TASK, TEST_BODY).replace(
                    b"new file mode 100644\n", b"new file mode 100644\ncopy from x.py\n"
                )
            ),
        }
        for name, patch in cases.items():
            with self.assertRaises(AcceptanceError, msg=name):
                validate_author_patch(patch, profile=PROFILE, task_id=7)

    def test_oversized_patch_rejected(self) -> None:
        big = _new_file(TASK, "# " + "x" * acceptance.MAX_AUTHOR_PATCH_BYTES + "\n" + TEST_BODY)
        with self.assertRaises(AcceptanceError):
            validate_author_patch(big, profile=PROFILE, task_id=7)


class GateReportTests(unittest.TestCase):
    def test_shape_and_caps(self) -> None:
        report = gate_report(
            status="failed",
            total=100,
            passed=3,
            failed=[f"t{i}" for i in range(80)],
            fixups=2,
            acceptance_modified=True,
            author_usage={"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 9},
            fixup_usage={"input_tokens": 7, "output_tokens": 1},
            author_model="gpt-6-luna",
            fixup_model="gpt-6-luna",
        )
        self.assertEqual(
            set(report),
            {
                "status", "total", "passed", "failed", "fixups", "acceptance_modified",
                "author_input_tokens", "author_output_tokens", "fixup_input_tokens",
                "fixup_output_tokens", "author_model", "fixup_model",
            },
        )  # fmt: skip
        self.assertEqual(len(report["failed"]), 50)
        self.assertEqual(report["author_input_tokens"], 10)
        self.assertEqual(report["fixup_output_tokens"], 1)

    def test_invariants_are_enforced_whatever_the_child_said(self) -> None:
        report = gate_report(
            status="failed",
            total=500,
            passed=900,
            failed=["a", "a", "b\nc\x00d", "", "x" * 500, *[f"t{i}" for i in range(200)]],
        )
        self.assertEqual(report["total"], 100)
        self.assertEqual(report["passed"], 100)
        self.assertEqual(len(report["failed"]), 50)
        self.assertEqual(report["failed"][:2], ["a", "bcd"])
        self.assertEqual(len(set(report["failed"])), 50)
        self.assertTrue(all(len(item) <= 200 for item in report["failed"]))
        small = gate_report(status="failed", total=2, passed=0, failed=["a", "b", "c", "d"])
        self.assertEqual(small["failed"], ["a", "b"])  # never more ids than tests
        self.assertEqual(gate_report(status="failed", total=0, failed=["a"])["status"], "error")
        self.assertEqual(gate_report(status="failed", total=0)["failed"], [])
        self.assertEqual(gate_report(status="passed", total=3, passed=-4)["passed"], 0)

    def test_profile_table(self) -> None:
        self.assertEqual(
            acceptance.profile_for("Asadtop4ik/agent-qa"),
            {"profile": "unittest", "dir": "tests/acceptance"},
        )
        self.assertIsNone(acceptance.profile_for("Asadtop4ik/task-manager"))


if __name__ == "__main__":
    unittest.main()
