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
    """"complex" always routes to the complex model, and so does
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


def compose_implement_prompt(base_prompt: str, work: Work, *, complex_route: bool) -> str:
    parts = [base_prompt]
    note = _relevant_files_note(work.relevant_files)
    if note:
        parts.append(note)
    parts.append(EFFICIENCY_RULES)
    if complex_route:
        parts.append(ORCHESTRATOR_RULES)
    return "\n".join(parts)


def compose_correction_prompt(base_prompt: str, *, complex_route: bool) -> str:
    parts = [base_prompt, EFFICIENCY_RULES]
    if complex_route:
        parts.append(ORCHESTRATOR_RULES)
    return "\n".join(parts)
