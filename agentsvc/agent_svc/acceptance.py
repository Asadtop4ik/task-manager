"""Pure helpers for the factory acceptance checks (test-author station,
Check Gate, splice): the per-repository profile table, the test-author output
schema, and unified-diff section handling.

Nothing here touches the network, git or the sandbox; `implement.py` does the
orchestration. A patch is split on `diff --git ` header lines (each section
runs up to the next header), which is safe for git binary patches too: a body
line of a normal diff always starts with ` `, `+`, `-`, `\\` or `@`, and a
binary-patch line starts with a base85 length character followed by base85
data (never a space), so a body line can never look like a header.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

# Agent-svc side of the Check Gate profile table. MUST match the allowlist in
# the trusted Codex child (`libexec/codex_child.py`, `check` subcommand): the
# request only ever carries the profile NAME, never a command.
PROFILES: dict[str, dict[str, str]] = {
    "Asadtop4ik/agent-qa": {"profile": "unittest", "dir": "tests/acceptance"},
}

MAX_AUTHOR_PATCH_BYTES = 200_000
MAX_FAILED_IDS = 50
MAX_GATE_TOTAL = 100
MAX_ID_CHARS = 200

# Strict structured output (`codex exec --output-schema`): every object needs
# `additionalProperties: false` and every property listed in `required`.
TEST_AUTHOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["ok", "cannot_check"]},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "criterion": {"type": "integer"},
                    "tests": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["criterion", "tests"],
            },
        },
        "unchecked": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "criterion": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["criterion", "reason"],
            },
        },
    },
    "required": ["status", "checks", "unchecked"],
}


class AcceptanceError(Exception):
    """The test-author station's output cannot be used (station is skipped)."""


def profile_for(repo: str) -> dict[str, str] | None:
    return PROFILES.get(repo)


def task_file_path(profile: dict[str, str], task_id: int) -> str:
    return f"{profile['dir']}/test_task_{task_id}.py"


def init_file_path(profile: dict[str, str]) -> str:
    return f"{profile['dir']}/__init__.py"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Section:
    """One `diff --git` section (or the text before the first one: no paths)."""

    paths: tuple[str, ...]
    data: bytes


_HEADER = b"diff --git "
_QUOTED_RE = re.compile(rb'"((?:[^"\\]|\\.)*)"')


def _header_paths(line: bytes) -> tuple[str, ...]:
    """The path(s) named by one `diff --git a/X b/Y` line (X and Y, deduplicated),
    or `()` if the header cannot be parsed."""
    rest = line[len(_HEADER) :].rstrip(b"\r\n")
    if rest.startswith(b'"') or b' "' in rest:
        quoted = [m.group(1) for m in _QUOTED_RE.finditer(rest)]
        if len(quoted) != 2 or not quoted[0].startswith(b"a/") or not quoted[1].startswith(b"b/"):
            return ()
        names = (quoted[0][2:], quoted[1][2:])
    else:
        if not rest.startswith(b"a/"):
            return ()
        # `a/P b/P` (same path both sides) is unambiguous even with spaces.
        length, odd = divmod(len(rest) - 5, 2)
        if not odd and length > 0 and rest[2 : 2 + length] == rest[5 + length :]:
            names = (rest[2 : 2 + length],)
        else:
            match = re.fullmatch(rb"a/(.+) b/(.+)", rest)
            if match is None:
                return ()
            names = (match.group(1), match.group(2))
    out: list[str] = []
    for name in names:
        text = name.decode("utf-8", "replace")
        if text not in out:
            out.append(text)
    return tuple(out)


def split_sections(patch: bytes) -> list[Section]:
    """Split `patch` on `diff --git ` header lines. `b"".join(s.data)` always
    reproduces `patch` exactly."""
    # `^` with re.MULTILINE matches only after b"\n" -- never after a lone b"\r"
    # (which `bytes.splitlines` would treat as a line break), so a carriage
    # return inside a patch line can never fake a section header.
    starts = [m.start() for m in re.finditer(rb"^diff --git ", patch, re.MULTILINE)]
    if not starts:
        return [Section((), patch)] if patch else []
    sections: list[Section] = []
    if starts[0] > 0:
        sections.append(Section((), patch[: starts[0]]))
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(patch)
        chunk = patch[start:end]
        header = chunk.split(b"\n", 1)[0] + b"\n"
        sections.append(Section(_header_paths(header), chunk))
    return sections


def _is_acceptance(section: Section, acceptance_dir: str) -> bool:
    prefix = acceptance_dir.rstrip("/") + "/"
    return bool(section.paths) and any(path.startswith(prefix) for path in section.paths)


def strip_acceptance(patch: bytes, acceptance_dir: str) -> bytes:
    """`patch` without any section touching `acceptance_dir/**`."""
    return b"".join(
        section.data
        for section in split_sections(patch)
        if not _is_acceptance(section, acceptance_dir)
    )


def acceptance_sections(patch: bytes, acceptance_dir: str) -> bytes:
    return b"".join(
        section.data
        for section in split_sections(patch)
        if _is_acceptance(section, acceptance_dir)
    )


def _is_artifact(section: Section) -> bool:
    """Bytecode caches are build artefacts, not part of the tests."""
    return bool(section.paths) and all(
        "__pycache__" in path.split("/") or path.endswith((".pyc", ".pyo"))
        for path in section.paths
    )


def drop_artifacts(patch: bytes) -> bytes:
    """`patch` without `__pycache__/` and `*.pyc` sections."""
    return b"".join(
        section.data for section in split_sections(patch) if not _is_artifact(section)
    )


def splice(
    builder_patch: bytes,
    pinned: bytes,
    acceptance_dir: str,
    *,
    unmodified: tuple[bytes, ...] = (),
) -> tuple[bytes, bool]:
    """Drop every acceptance section from the builder's (cumulative) patch and
    append the pinned author sections. Returns `(patch, acceptance_modified)`:
    modified is True when the builder's acceptance sections (bytecode caches
    ignored) differ byte-for-byte from the pinned ones or from any of
    `unmodified` -- the other byte strings that mean "the builder left the
    locked tests alone" (the raw author patch, when preflight reformatted it
    into the pin) -- i.e. edited, deleted or extra files."""
    found = drop_artifacts(acceptance_sections(builder_patch, acceptance_dir))
    modified = found != pinned and found not in unmodified
    kept = strip_acceptance(builder_patch, acceptance_dir)
    if kept and not kept.endswith(b"\n"):
        kept += b"\n"
    return kept + pinned, modified


def count_added_tests(patch: bytes, path: str) -> int:
    """Number of `def test_...` lines ADDED to `path` by `patch`."""
    total = 0
    for section in split_sections(patch):
        if path not in section.paths:
            continue
        for line in section.data.split(b"\n"):
            if re.match(rb"\+\s*(?:async\s+)?def\s+test_\w*\s*\(", line):
                total += 1
    return total


def validate_author_patch(
    patch: bytes, *, profile: dict[str, str], task_id: int
) -> tuple[str, int]:
    """Validate the test author's patch; return `(task_file_path, test_count)`.

    Allowed: exactly the task file (a new regular file) and, optionally, an
    empty new `__init__.py` -- nothing else, no binary patch, no mode other
    than 100644. Raises `AcceptanceError` with a short reason otherwise."""
    if not patch.strip():
        raise AcceptanceError("test author produced no file changes")
    if len(patch) > MAX_AUTHOR_PATCH_BYTES:
        raise AcceptanceError("test author patch is too large")
    task_path = task_file_path(profile, task_id)
    init_path = init_file_path(profile)
    seen: set[str] = set()
    for section in split_sections(patch):
        if len(section.paths) != 1 or section.paths[0] not in (task_path, init_path):
            raise AcceptanceError(
                "test author changed an unexpected path: "
                + (", ".join(section.paths) or "unparseable diff header")
            )
        path = section.paths[0]
        if path in seen:
            raise AcceptanceError(f"test author patch lists {path} twice")
        seen.add(path)
        if b"\nGIT binary patch" in section.data or b"\nBinary files " in section.data:
            raise AcceptanceError(f"{path} is a binary change")
        # `package` runs `git diff --no-renames`, so a genuine author patch can
        # never carry these; refuse a hand-made one rather than reason about it.
        if (
            b"\nrename from " in section.data
            or b"\ncopy from " in section.data
            or b"\nrename to " in section.data
            or b"\ncopy to " in section.data
        ):
            raise AcceptanceError(f"{path} is a rename or copy")
        if b"\nnew file mode 100644\n" not in section.data:
            raise AcceptanceError(f"{path} is not a new regular file")
        if path == init_path and b"\n@@" in section.data:
            raise AcceptanceError(f"{path} must be empty")
    if task_path not in seen:
        raise AcceptanceError("test author did not write the task test file")
    count = count_added_tests(patch, task_path)
    if count < 1:
        raise AcceptanceError("test author wrote no tests")
    return task_path, count


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def clean_test_id(value: object) -> str:
    """A test id/name as safe single-line text: control characters (newlines
    included) removed, at most `MAX_ID_CHARS` characters."""
    return _CONTROL_RE.sub("", str(value)).strip()[:MAX_ID_CHARS]


def normalise_result(
    status: str, total: int, passed: int, failed: list[str] | None
) -> tuple[str, int, int, list[str]]:
    """The one place the backend's `check_gate` invariants are enforced:
    total <= 100, passed <= total, failed = distinct clean ids, at most
    min(50, total) of them, and a `failed` status without a single test is an
    `error`."""
    total = max(0, min(int(total), MAX_GATE_TOTAL))
    passed = max(0, min(int(passed), total))
    ids: list[str] = []
    for item in failed or []:
        name = clean_test_id(item)
        if name and name not in ids:
            ids.append(name)
    ids = ids[: min(MAX_FAILED_IDS, total)]
    if status == "failed" and total == 0:
        status = "error"
    return status, total, passed, ids


def gate_report(
    *,
    status: str,
    total: int = 0,
    passed: int = 0,
    failed: list[str] | None = None,
    fixups: int = 0,
    acceptance_modified: bool = False,
    author_usage: dict[str, int] | None = None,
    fixup_usage: dict[str, int] | None = None,
    author_model: str | None = None,
    fixup_model: str | None = None,
) -> dict[str, Any]:
    """The `check_gate` object sent in the run callback; the counters are
    normalised here (see `normalise_result`), whatever the child reported."""
    author_usage = author_usage or {}
    fixup_usage = fixup_usage or {}
    status, total, passed, ids = normalise_result(status, total, passed, failed)
    return {
        "status": status,
        "total": total,
        "passed": passed,
        "failed": ids,
        "fixups": fixups,
        "acceptance_modified": acceptance_modified,
        "author_input_tokens": author_usage.get("input_tokens", 0),
        "author_output_tokens": author_usage.get("output_tokens", 0),
        "fixup_input_tokens": fixup_usage.get("input_tokens", 0),
        "fixup_output_tokens": fixup_usage.get("output_tokens", 0),
        "author_model": author_model,
        "fixup_model": fixup_model,
    }
