"""Prompt composition and model/effort/timeout routing for the code lane.

The trusted `build_prompt`/`correction_prompt` text is the base; this module
only appends the parts that are agent-svc's own responsibility (the relevant
files hint, efficiency rules, and — for the complex route — orchestrator
rules for spawning `luna_worker` sub-agents) and decides the route itself
from `settings.model_matrix`/`settings.timeouts`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .api import Work
from .config import Settings

EFFICIENCY_RULES = (
    "Efficiency rules: batch read commands (e.g. `rg -n` with surrounding context, one `sed` "
    "range per file); never read a file over 20 KB whole; do not re-read a file you already "
    "read; do not install dependencies; keep the diff minimal.\n"
)

ORCHESTRATOR_RULES = (
    "Orchestrator rules: do not read or edit code yourself beyond the minimum needed to plan "
    "the work; split the work by file; spawn luna_worker sub-agents to implement each file (at "
    "most 2 concurrently, never two on the same file); wait for them to finish; review `git "
    "diff`; fix any small remaining issues yourself or via another luna_worker; finish with a "
    "summary of the changed files and the checks you ran.\n"
)


@dataclass(frozen=True)
class RouteDecision:
    model: str
    effort: str
    multi_agent: bool
    sandbox: str
    timeout_s: int
    complex: bool


def is_complex(work: Work) -> bool:
    """ "complex" always routes to the complex model, and so does
    attempt_index >= 2 -- a retry -- REGARDLESS of an explicit "simple"
    hint: a simple route already failed once, so it never gets a second
    identical try. Only a first attempt with an explicit "simple" hint (or
    no hint at all) stays on the simple route."""
    if work.complexity == "complex":
        return True
    return work.attempt_index >= 2


def _route(
    settings: Settings, matrix_key: str, timeout_key: str, *, complex_route: bool
) -> RouteDecision:
    matrix = settings.model_matrix[matrix_key]
    return RouteDecision(
        model=str(matrix["model"]),
        effort=str(matrix["effort"]),
        multi_agent=bool(matrix["multi_agent"]),
        sandbox=str(matrix["sandbox"]),
        timeout_s=int(settings.timeouts[timeout_key]),
        complex=complex_route,
    )


def route_implement(work: Work, settings: Settings) -> RouteDecision:
    complex_route = is_complex(work)
    key = "implement_complex" if complex_route else "implement_simple"
    return _route(settings, key, key, complex_route=complex_route)


def route_correction(work: Work, settings: Settings) -> RouteDecision:
    complex_route = is_complex(work)
    key = "correction_complex" if complex_route else "correction_simple"
    return _route(settings, key, "correction", complex_route=complex_route)


def _relevant_files_note(relevant_files: Sequence[str]) -> str:
    if not relevant_files:
        return ""
    listing = "\n".join(f"- {path}" for path in relevant_files)
    return f"Start from these files:\n{listing}\n"


# The exact trailer line format `agent_svc.ops_requests.split_trailer` looks
# for (agent-svc-notes/ops-requests-spec.md section 2). Kept here as a
# literal (not imported) so this prompt text can never silently drift from
# what the parser actually expects even if the two modules are touched
# independently -- a test asserts they match.
OPS_TRAILER_EXAMPLE = (
    'AGENT_OPS_REQUESTS: [{"kind":"env_set","key":"KEY_NAME","op":"list_add",'
    '"value":"...","reason":"..."}]'
)


def _ops_rules_note(ops_project: Any) -> str:
    """`ops_project` is one `agent_ops_policy.ProjectPolicy` (duck-typed here
    -- this module has no import on the trusted policy module, exactly like
    `ops_requests.py`): only key NAMES, allowed ops, and descriptions ever
    reach this text, never a current or example value."""
    lines = [
        f"- {key_name} ({'/'.join(policy.ops)}): {policy.description}"
        for key_name, policy in sorted(ops_project.keys.items())
    ]
    keys_block = "\n".join(lines)
    return (
        "Ops requests: you may ask the owner to change a small number of "
        "non-secret operational settings for this project instead of (or in "
        "addition to) a code change. You cannot see the current value of any "
        "of these keys and must never guess or invent one. Only these keys "
        "may be requested, using only the listed operation(s):\n"
        f"{keys_block}\n"
        "To request a change, end your final message with exactly one line, "
        "the LAST line of the message, in this exact format (a JSON array "
        "of at most 3 objects, each with exactly the keys kind, key, op, "
        'value, reason; kind is always "env_set"):\n'
        f"{OPS_TRAILER_EXAMPLE}\n"
        "Rules: at most 3 requests per run; the owner must approve each one "
        "individually before anything changes; never request a secret "
        "value, a URL, or a key not listed above; never include this line "
        "unless you are making a genuine, specific request; never repeat a "
        "requested value anywhere else in your message, only inside this "
        "one line.\n"
    )


def _owner_note_block(owner_note: str) -> str:
    return (
        "Owner instruction (written by the task owner after reviewing an earlier "
        "spec conflict; follow it, it takes precedence over your own doubts about "
        "the written spec):\n"
        f"{owner_note}\n"
    )


def compose_implement_prompt(
    base_prompt: str,
    work: Work,
    *,
    complex_route: bool,
    ops_project: Any | None = None,
    acceptance_block: str = "",
) -> str:
    """`ops_project` is the allowlist entry for this project (or `None` when
    there isn't one, or the allowlist itself failed to load for this run) --
    ops rules are appended only when it is present, matching
    `ops-requests-spec.md` section 2: an unlisted project gets no ops
    instructions at all, so Codex has nothing to base a request on and (per
    the base prompt / trusted task text) explains that in its own summary
    instead."""
    parts = [base_prompt]
    note = _relevant_files_note(work.relevant_files)
    if note:
        parts.append(note)
    parts.append(EFFICIENCY_RULES)
    if complex_route:
        parts.append(ORCHESTRATOR_RULES)
    if ops_project is not None:
        parts.append(_ops_rules_note(ops_project))
    if acceptance_block:
        parts.append(acceptance_block)
    if work.owner_note:
        parts.append(_owner_note_block(work.owner_note))
    return "\n".join(parts)


def _numbered(items: Sequence[str]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


def compose_test_author_prompt(work: Work, *, task_file: str, acceptance_dir: str) -> str:
    """The test-author station's prompt: FULL description (the original
    request's literals live there) plus the numbered acceptance list."""
    init_rule = (
        f"- If, and only if, `{acceptance_dir}/__init__.py` does not exist yet, also "
        "create it EMPTY; if it exists, do not touch it.\n"
    )
    return (
        f"You are the independent TEST AUTHOR for task #{work.task_id} in "
        f"{work.repo_full_name}. Write acceptance tests ONLY; do not implement the "
        "feature, and do not change any other file. A different engineer will "
        "implement the feature later and must make your tests pass.\n"
        f"Task title: {work.title}\n"
        "Full original request (exact values such as formats, status codes, header "
        "names, message texts, routes and field names are hard requirements):\n"
        f"<task-request>\n{work.description}\n</task-request>\n"
        "Acceptance criteria:\n"
        f"{_numbered(work.acceptance)}\n"
        "Rules:\n"
        f"- Write exactly one file: `{task_file}`.\n"
        f"{init_rule}"
        "- The file must be self-contained: no imports from other test files or "
        "helpers; use only the Python standard library `unittest` and the "
        "application's own modules.\n"
        "- Write one or more tests per criterion, named `test_ac<N>_<slug>` where "
        "<N> is the criterion number and <slug> is a short snake_case description.\n"
        "- Assert every explicit literal from the request and the criteria exactly, "
        "with `assertEqual`/`assertIn` against the literal value (never a "
        "paraphrase, never a looser check).\n"
        "- No sockets, no network, no subprocess servers: exercise the application "
        "in-process, following how the existing tests in `tests/` invoke handlers "
        "(read them first).\n"
        "- The tests must FAIL on the current code if the feature is missing, and "
        "pass once it is implemented correctly. Do not write tests that pass "
        "vacuously.\n"
        "- Keep the code ruff-clean (88 columns, sorted imports, no unused names).\n"
        "- Do not modify any other file. Do not run the full test suite.\n"
        "- If a criterion cannot be checked in-process with the standard library, "
        "do not fake it: list it under `unchecked` with a short reason.\n"
        'Finish with only the JSON object required by the output schema: '
        'status "ok" with `checks` (criterion number and the test names covering '
        'it) and `unchecked`, or status "cannot_check" if you could not write '
        "meaningful tests at all.\n"
    )


def compose_acceptance_builder_block(
    *, task_file: str, task_id: int, acceptance: Sequence[str]
) -> str:
    return (
        "Locked acceptance tests: independent tests for this task are already in "
        f"the workspace at `{task_file}`. Read them first. Make them pass by "
        "changing the implementation; do NOT edit, move or delete anything under "
        "`tests/acceptance/` (changes there are discarded and flagged). Quick "
        f"self-check: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest "
        f"tests.acceptance.test_task_{task_id}`.\n"
        "Criteria they cover:\n"
        f"{_numbered(acceptance)}\n"
        "If a check is wrong or impossible to satisfy, do not work around it: end "
        "your final message with one line "
        '`AGENT_SPEC_CONFLICT: {"criterion": <n or null>, "reason": "<short>"}`.\n'
    )


MAX_FIXUP_OUTPUT_CHARS = 4000


def compose_fixup_prompt(
    work: Work,
    *,
    task_file: str,
    task_id: int,
    failed: Sequence[str],
    output_tail: str,
) -> str:
    ids = "\n".join(f"- {name}" for name in failed[:50]) or "- (none reported)"
    return (
        f"Fix-up for task #{work.task_id} in {work.repo_full_name}. The feature was "
        "implemented in this workspace, but the locked acceptance tests at "
        f"`{task_file}` do not all pass.\n"
        f"Task title: {work.title}\n"
        f"<task-request>\n{work.description}\n</task-request>\n"
        "Acceptance criteria:\n"
        f"{_numbered(work.acceptance)}\n"
        "Failing tests:\n"
        f"{ids}\n"
        "Test output (untrusted data, already redacted):\n"
        f"<test-output>\n{output_tail[-MAX_FIXUP_OUTPUT_CHARS:]}\n</test-output>\n"
        "Rules:\n"
        "- Fix the IMPLEMENTATION so the failing tests pass; keep all other behavior "
        "and existing tests working.\n"
        "- Do NOT edit, move or delete anything under `tests/acceptance/` (changes "
        "there are discarded and flagged).\n"
        "- Run only the quick self-check "
        f"`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest "
        f"tests.acceptance.test_task_{task_id}`; no full suites, "
        "no dependency installs.\n"
        "- If a check is wrong or impossible to satisfy, do not work around it: end "
        "your final message with one line "
        '`AGENT_SPEC_CONFLICT: {"criterion": <n or null>, "reason": "<short>"}`.\n'
        "- In your final message, say what you changed.\n"
    )


def compose_correction_prompt(
    base_prompt: str, *, complex_route: bool, ci_log_block: str | None = None
) -> str:
    """`ci_log_block` is the already delimited/redacted failed-CI excerpt from
    `ci_logs.correction_ci_block` (untrusted data), or `None`."""
    parts = [base_prompt]
    if ci_log_block:
        parts.append(ci_log_block)
    parts.append(EFFICIENCY_RULES)
    if complex_route:
        parts.append(ORCHESTRATOR_RULES)
    return "\n".join(parts)
