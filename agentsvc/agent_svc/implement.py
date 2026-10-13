"""`handle_implement`: lease work of kind "implement" through prepare -> Codex -> publish.

Every failure path reports through `api.callback` with the exact fields the
Task Manager API expects (see `backend/app/schemas/agent_run.py`
`AgentRunCallback`): `status`, `error`, `failure_phase`, `pr_url`, `head_sha`,
plus token usage. A lost lease (heartbeat `LeaseLost`, or `RunScaffold.cancel`
already set) means the API already owns the outcome, so no callback is sent.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import acceptance
from .api import Work
from .context import ServiceContext
from .http import HttpError
from .ops_requests import build_ops_note, split_trailer
from .ops_requests import validate as validate_ops_requests
from .prompts import (
    compose_acceptance_builder_block,
    compose_dispute_resume_block,
    compose_fixup_prompt,
    compose_implement_prompt,
    compose_test_author_prompt,
    compose_test_dispute_prompt,
    route_implement,
)
from .publish import (
    PublishError,
    _check_not_cancelled,
    _run_preflight,
    publish_implement,
)
from .runctx import RunScaffold
from .spec_conflict import SpecConflict, conflict_error, split_spec_conflict

_NO_CHANGES_MESSAGE = "agent produced no file changes"
_OPS_UNAVAILABLE_NOTE = "ops_unavailable"
_OPS_CALLBACK_KEYS = ("ops_requests", "ops_note")
_OPS_VALUE_PLACEHOLDER = "[ops qiymati]"
_MIN_REDACTED_VALUE_LEN = 4
_CALLBACK_GATE_KEY = "check_gate"

# Per-run time budget. The backend lease is a hard 60 minutes; every exec in a
# run (test author, builder, fix-ups) gets `min(configured, remaining -
# EXEC_RESERVE_S)` so publication (preflight, push, PR) still fits.
RUN_BUDGET_S = 3300.0
EXEC_RESERVE_S = 240.0
FIXUP_MIN_REMAINING_S = 600.0
_MIN_EXEC_TIMEOUT_S = 30.0
_MIN_AUTHOR_REMAINING_S = 900.0
# A spec conflict raised while locked acceptance tests exist first goes back to
# the test author this many times per run (the author either revises the
# disputed tests or upholds them); only an upheld or repeated conflict fails
# the run with SPEC_CONFLICT for the owner.
MAX_TEST_DISPUTES = 1
_CHECK_GRACE_S = 60.0
_CHECK_STATUSES = frozenset({"passed", "failed", "error", "unavailable"})
_MAX_OUTPUT_TAIL = 4000

_monotonic = time.monotonic


def handle_implement(ctx: ServiceContext, work: Work, cancel: threading.Event) -> None:
    with RunScaffold(ctx, work, cancel) as run:
        _run_implement(ctx, work, run)


def _run_implement(ctx: ServiceContext, work: Work, run: RunScaffold) -> None:
    deadline = _monotonic() + RUN_BUDGET_S
    if work.mode != "pr":
        _fail(ctx, work, run, "implement", "fast mode is not handled by agent-svc")
        return

    try:
        task, is_public = _validate_task(ctx, work)
    except (ValueError, TypeError) as exc:
        _fail(ctx, work, run, "implement", str(exc))
        return

    branch = ctx.trusted.agent_task.branch_name(task)

    try:
        base_sha = ctx.mirrors.fetch(work.repo_full_name, work.base_branch)
    except Exception as exc:
        ctx.logger.error(exc, event="mirror_fetch_failed", run_id=work.run_id)
        _fail(ctx, work, run, "implement", f"could not fetch the base branch: {exc}")
        return
    if run.cancel.is_set():
        return

    try:
        existing = ctx.github.get_ref(work.repo_full_name, branch)
    except Exception as exc:
        _fail(ctx, work, run, "implement", f"could not check for an existing branch: {exc}")
        return
    if existing is not None:
        _recover_existing_branch(ctx, work, run, branch)
        return

    run.stage("workspace_ready", base_sha=base_sha, branch=branch)
    if run.cancel.is_set():
        return

    try:
        image_dir, image_paths = _download_images(ctx, work, run)
    except Exception as exc:
        _fail(ctx, work, run, "implement", f"could not download task images: {exc}")
        return
    if run.cancel.is_set():
        return

    mirror_path = ctx.mirrors.mirror_path(work.repo_full_name)
    prepare_request = {
        "run_id": work.run_id,
        "repo": work.repo_full_name,
        "mirror": str(mirror_path),
        "base_sha": base_sha,
    }
    try:
        ctx.codex.prepare(dict(prepare_request))
    except Exception as exc:
        _fail(ctx, work, run, "implement", f"workspace preparation failed: {exc}")
        return
    if run.cancel.is_set():
        return

    route = route_implement(work, ctx.settings)
    base_prompt = (
        ctx.trusted.public_agent_task.build_prompt(task, qa_enabled=True)
        if is_public
        else ctx.trusted.agent_task.build_prompt(task)
    )
    # Loaded fresh, once per run (never cached across runs -- an operator may
    # edit the allowlist between two runs of the same project): used both to
    # decide whether the prompt gets ops-request instructions, and later to
    # validate whatever trailer Codex actually emitted. `None` means "missing
    # or invalid file" -- treated as "no allowlist" throughout, never a crash.
    # `settings.ops_lane_enabled=False` skips the load entirely and is
    # treated the same way (no prompt rules, nothing ever "allowed") -- see
    # `validate_ops_requests`'s own `enabled` parameter, which is what turns
    # a denied proposal's reason into "ops_disabled" rather than
    # "no_allowlist" for this case.
    ops_project_key = _ops_project_key(ctx, work)
    allowlist = (
        _load_ops_allowlist(ctx, work.run_id) if ctx.settings.ops_lane_enabled else None
    )
    ops_project = (
        allowlist.projects.get(ops_project_key)
        if allowlist is not None and ops_project_key is not None
        else None
    )

    # Factory acceptance checks: the test-author station runs only with the
    # kill switch on, a profiled repository and a non-empty acceptance list.
    # Otherwise `state.gate()` stays None and nothing below changes behavior
    # (no `check_gate` key in any callback).
    state = _RunState()
    profile = (
        acceptance.profile_for(work.repo_full_name)
        if ctx.settings.acceptance_checks_enabled and work.acceptance
        else None
    )
    station: _Station | None = None
    if profile is not None:
        state.active = True
        try:
            station = _run_author_station(
                ctx,
                work,
                run,
                state,
                profile=profile,
                base_sha=base_sha,
                deadline=deadline,
                prepare_request=prepare_request,
            )
        except _WorkspaceRestoreError as exc:
            _fail(
                ctx,
                work,
                run,
                "implement",
                f"workspace preparation failed: {exc}",
                **state.report(),
            )
            return
        if run.cancel.is_set():
            return

    prompt = compose_implement_prompt(
        base_prompt,
        work,
        complex_route=route.complex,
        ops_project=ops_project,
        spec_conflict_rules=ctx.settings.acceptance_checks_enabled,
        acceptance_block=(
            compose_acceptance_builder_block(
                task_file=station.task_file,
                task_id=work.task_id,
                acceptance=work.acceptance,
            )
            if station is not None
            else ""
        ),
    )

    try:
        _run_builder_and_publish(
            ctx,
            work,
            run,
            state,
            station=station,
            task=task,
            is_public=is_public,
            branch=branch,
            base_sha=base_sha,
            image_dir=image_dir,
            image_paths=image_paths,
            prompt=prompt,
            route=route,
            deadline=deadline,
            ops_project_key=ops_project_key,
            allowlist=allowlist,
        )
    except _RunEnded:
        return


def _run_builder_and_publish(
    ctx: ServiceContext,
    work: Work,
    run: RunScaffold,
    state: _RunState,
    *,
    station: _Station | None,
    task: dict[str, Any],
    is_public: bool,
    branch: str,
    base_sha: str,
    image_dir: Path | None,
    image_paths: list[Path],
    prompt: str,
    route: Any,
    deadline: float,
    ops_project_key: str | None,
    allowlist: Any,
) -> None:
    run.stage("codex_started")
    builder_prompt = prompt
    while True:
        result = ctx.codex.run_exec(
            {
                "run_id": work.run_id,
                "lane": "code",
                "cwd": "wt",
                "model": route.model,
                "effort": route.effort,
                "sandbox": route.sandbox,
                "multi_agent": route.multi_agent,
                "prompt": builder_prompt,
                "images": [str(path) for path in image_paths],
                "output_schema": None,
                "timeout_s": _exec_timeout(route.timeout_s, deadline),
                "idle_timeout_s": ctx.settings.idle_timeout_s,
            },
            on_event=lambda _event: None,
            heartbeat=None,
            cancel=run.cancel,
        )
        state.add_total(result.usage)
        run.stage("codex_finished")

        if run.cancel.is_set() or result.cancelled:
            raise _RunEnded

        # A spec-conflict trailer is stripped BEFORE the ops trailer is split
        # (and before anything else reads Codex's text), so the marker line can
        # never leak into a PR body or a failure reason. When present the run
        # stops here (no package, no publish) unless the test author revises
        # the disputed locked tests: then the builder runs once more.
        # The trailer is only trusted (and the rule only put in the prompt) when
        # the acceptance switch is on; switched off, marker-shaped lines are
        # still scrubbed from the text but the run continues exactly as it
        # always did.
        message, conflict = split_spec_conflict(result.final_message)
        _log_ignored_marker(ctx, work, result.final_message, message, conflict)
        if conflict is None or not ctx.settings.acceptance_checks_enabled:
            break
        resolved = (
            _resolve_test_dispute(
                ctx,
                work,
                run,
                state,
                station=station,
                conflict=conflict,
                base_sha=base_sha,
                deadline=deadline,
            )
            if station is not None
            else None
        )
        if resolved is None:
            _fail_spec_conflict(ctx, work, run, state, conflict)
            raise _RunEnded
        station = resolved[0]
        builder_prompt = (
            prompt
            + "\n"
            + compose_dispute_resume_block(task_file=station.task_file, task_id=work.task_id)
        )

    # Every downstream use of Codex's own text -- a failure reason, the "no
    # changes" note, or the eventual PR body -- must never carry the ops
    # trailer line: an env value (however harmless-looking) must never reach
    # a public PR or a Telegram message. This one call covers all of them.
    summary, raw_ops, trailer_note = split_trailer(message or "")

    # Ops block: policy access, validation, and note-building all in one
    # place, all defended together. `ctx.trusted.agent_ops_policy` is a
    # SECOND access to the trusted loader in this run (the first, inside
    # `_load_ops_allowlist` above, is already defended the same way) -- an
    # older installer without `scripts/agent_ops_policy.py` yet deployed
    # would otherwise raise `FileNotFoundError` here, AFTER Codex already
    # ran, crashing the whole run with no callback ever sent at all. Ops
    # requests are a secondary feature: any failure here must degrade to
    # "no proposals" instead of losing the run's own real outcome.
    try:
        policy_module = ctx.trusted.agent_ops_policy
        proposals, drop_note = validate_ops_requests(
            raw_ops,
            project_key=ops_project_key,
            repo_full_name=work.repo_full_name,
            allowlist=allowlist,
            policy_module=policy_module,
            enabled=ctx.settings.ops_lane_enabled,
        )
        ops_note = build_ops_note(
            trailer_note=trailer_note, drop_note=drop_note, policy_module=policy_module
        )
    except Exception as exc:
        ctx.logger.error(exc, event="ops_validation_failed", run_id=work.run_id)
        proposals = []
        ops_note = _OPS_UNAVAILABLE_NOTE

    # Defense in depth against Codex repeating a requested value in its own
    # prose, outside the trailer line `split_trailer` already removed (the
    # prompt also tells it not to) -- applied to every piece of Codex's own
    # text used below, including the early failure-reason branch just past
    # this point.
    summary = _redact_ops_values(summary, proposals)

    if result.timed_out or result.idle_killed or result.exit_code != 0:
        reason = ctx.trusted.agent_task.failure_reason(
            failure_phase="implement",
            codex_step_outcome="failure",
            load_result_text=lambda: summary or None,
            load_policy_error_text=lambda: None,
            load_fast_error_text=lambda: None,
        )
        if result.error_message and not result.final_message:
            # e.g. a usage limit or model error reported by Codex itself.
            reason = f"{reason} (Codex: {result.error_message[:300]})"
        elif not result.final_message and result.stderr_tail:
            # No Codex output at all: the child refused the request or Codex
            # died before its first event. Its last stderr line is the only
            # diagnosis (e.g. "invalid model"); without it the run showed a
            # generic failure.
            tail = ctx.redactor.redact(result.stderr_tail[-1].strip())[:300]
            reason = f"{reason} (Codex stderr: {tail})"
        ctx.logger.event(
            "implement_codex_failed",
            level="error",
            run_id=work.run_id,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            idle_killed=result.idle_killed,
            error=result.error_message[:300] or None,
            stderr_tail=[ctx.redactor.redact(line)[:300] for line in result.stderr_tail[-3:]],
        )
        reason = _redact_ops_values(reason, proposals)
        _fail(ctx, work, run, "implement", reason, **state.report())
        return

    try:
        package = ctx.codex.package({"run_id": work.run_id})
    except Exception as exc:
        _fail(
            ctx,
            work,
            run,
            "implement",
            f"packaging the patch failed: {exc}",
            **state.report(),
        )
        return

    patch_b64 = package.get("patch_b64") or ""
    patch = b""
    if patch_b64:
        try:
            patch = base64.b64decode(patch_b64, validate=True)
        except (ValueError, TypeError) as exc:
            _fail(
                ctx,
                work,
                run,
                "implement",
                f"invalid patch encoding: {exc}",
                **state.report(),
            )
            return

    # With a locked acceptance file in the workspace the cumulative package
    # always contains it; "no changes" then means no changes OUTSIDE the
    # acceptance directory.
    if station is not None:
        no_changes = not acceptance.strip_acceptance(patch, station.profile["dir"]).strip()
    else:
        no_changes = not patch_b64
    if no_changes:
        if any(proposal["policy"] == "allowed" for proposal in proposals):
            _callback(
                ctx,
                work,
                run,
                {
                    "run_id": work.run_id,
                    "status": "ops_pending",
                    "ops_requests": proposals,
                    "ops_note": ops_note,
                    **state.callback_fields(),
                },
            )
            return
        reason = _NO_CHANGES_MESSAGE
        flat_summary = " ".join(summary.split())
        if flat_summary:
            reason += "\nCodex izohi (tasdiqlanmagan): " + flat_summary[:650]
        _fail(
            ctx,
            work,
            run,
            "implement",
            reason[:900],
            ops_requests=proposals,
            ops_note=ops_note,
            **state.report(),
        )
        return

    if station is not None:
        patch = _splice_and_gate(
            ctx,
            work,
            run,
            state,
            station=station,
            base_sha=base_sha,
            patch=patch,
            deadline=deadline,
        )
        if not acceptance.strip_acceptance(patch, station.profile["dir"]).strip():
            # A fix-up reverted every non-test change.
            _fail(
                ctx,
                work,
                run,
                "implement",
                _NO_CHANGES_MESSAGE,
                ops_requests=proposals,
                ops_note=ops_note,
                **state.report(),
            )
            return

    try:
        publish_result = publish_implement(
            ctx,
            work,
            base_sha=base_sha,
            branch=branch,
            patch=patch,
            task=task,
            is_public=is_public,
            image_dir=image_dir,
            codex_summary=summary,
            cancel=run.cancel,
            report_stage=lambda name: run.stage(name, base_sha=base_sha, branch=branch),
        )
    except PublishError as exc:
        _fail(ctx, work, run, exc.phase, exc.reason, **state.report())
        return
    except Exception as exc:
        ctx.logger.error(exc, event="publish_unexpected_error", run_id=work.run_id)
        _fail(
            ctx,
            work,
            run,
            "publish",
            "Publisher failed before PR/deploy; inspect the agent-svc log.",
            **state.report(),
        )
        return

    _callback(
        ctx,
        work,
        run,
        {
            "run_id": work.run_id,
            "status": "pr_opened",
            "pr_url": publish_result.pr_url,
            "head_sha": publish_result.head_sha,
            "ops_requests": proposals,
            "ops_note": ops_note,
            **state.callback_fields(),
        },
    )


# --------------------------------------------------------------------------
# Factory acceptance checks: run state, test-author station, Check Gate.
# --------------------------------------------------------------------------


class _RunEnded(Exception):
    """The outcome was already reported (or the lease was lost): unwind."""


class _WorkspaceRestoreError(Exception):
    """`cleanup` + `prepare` could not restore a clean workspace."""


class _TestDispute(Exception):
    """A fix-up reported a spec conflict while a test dispute is still allowed."""

    def __init__(self, conflict: SpecConflict) -> None:
        super().__init__(conflict.reason)
        self.conflict = conflict


def _exec_timeout(configured: float, deadline: float) -> float:
    """`min(configured, time left before the run deadline - reserve)`, never
    below a small floor. Returns `configured` itself when it fits, so a run
    that is nowhere near its budget sends exactly the configured value."""
    available = deadline - _monotonic() - EXEC_RESERVE_S
    return max(_MIN_EXEC_TIMEOUT_S, min(configured, available))


def _add_usage(into: dict[str, int], usage: Mapping[str, Any] | None) -> None:
    for key, value in _usage_fields(usage).items():
        into[key] = into.get(key, 0) + value


@dataclass
class _RunState:
    """Accumulated usage (author + builder + fix-ups) and the Check Gate
    summary for one run. `active` is False unless the acceptance feature ran
    or was skipped for this run: then no `check_gate` is ever reported."""

    active: bool = False
    usage_total: dict[str, int] = field(default_factory=dict)
    usage_author: dict[str, int] = field(default_factory=dict)
    usage_fixup: dict[str, int] = field(default_factory=dict)
    status: str = "skipped"
    total: int = 0
    passed: int = 0
    failed: list[str] = field(default_factory=list)
    fixups: int = 0
    disputes: int = 0
    acceptance_modified: bool = False
    author_model: str | None = None
    fixup_model: str | None = None

    def add_total(self, usage: Mapping[str, Any] | None) -> None:
        _add_usage(self.usage_total, usage)

    def add_author(self, usage: Mapping[str, Any] | None) -> None:
        _add_usage(self.usage_author, usage)
        _add_usage(self.usage_total, usage)

    def add_fixup(self, usage: Mapping[str, Any] | None) -> None:
        _add_usage(self.usage_fixup, usage)
        _add_usage(self.usage_total, usage)

    def usage(self) -> dict[str, int] | None:
        return dict(self.usage_total) or None

    def gate(self) -> dict[str, Any] | None:
        if not self.active:
            return None
        return acceptance.gate_report(
            status=self.status,
            total=self.total,
            passed=self.passed,
            failed=self.failed,
            fixups=self.fixups,
            acceptance_modified=self.acceptance_modified,
            author_usage=self.usage_author,
            fixup_usage=self.usage_fixup,
            author_model=self.author_model,
            fixup_model=self.fixup_model,
        )

    def report(self) -> dict[str, Any]:
        """Keyword arguments for `_fail`."""
        return {"usage": self.usage(), "check_gate": self.gate()}

    def callback_fields(self) -> dict[str, Any]:
        """Usage fields (and `check_gate`, when active) for a callback payload."""
        fields: dict[str, Any] = dict(_usage_fields(self.usage()))
        gate = self.gate()
        if gate is not None:
            fields[_CALLBACK_GATE_KEY] = gate
        return fields


@dataclass(frozen=True)
class _Station:
    """A successful test-author station: the pinned acceptance patch."""

    profile: dict[str, str]
    task_file: str
    pinned: bytes
    test_count: int
    # The author's own (artefact-free) patch, which is what the workspace
    # holds; equals `pinned` unless preflight reformatted it.
    raw: bytes = b""


def _log_ignored_marker(
    ctx: ServiceContext,
    work: Work,
    raw: str | None,
    stripped: str,
    conflict: Any,
) -> None:
    """Marker-shaped lines were scrubbed but no conflict will be acted on."""
    if stripped == (raw or "") and conflict is None:
        return
    if conflict is not None and ctx.settings.acceptance_checks_enabled:
        return
    ctx.logger.event(
        "spec_conflict_marker_ignored",
        level="warning",
        run_id=work.run_id,
        trusted_marker=conflict is not None,
        switch_on=ctx.settings.acceptance_checks_enabled,
    )


def _fail_spec_conflict(
    ctx: ServiceContext, work: Work, run: RunScaffold, state: _RunState, conflict: Any
) -> None:
    ctx.logger.event(
        "spec_conflict",
        level="warning",
        run_id=work.run_id,
        criterion=conflict.criterion,
    )
    _fail(
        ctx,
        work,
        run,
        "implement",
        conflict_error(conflict, ctx.redactor.redact),
        **state.report(),
    )


def _restore_workspace(
    ctx: ServiceContext, work: Work, prepare_request: Mapping[str, Any]
) -> None:
    """Back to a clean `wt` at the base commit after a skipped station."""
    try:
        ctx.codex.cleanup({"run_id": work.run_id})
        ctx.codex.prepare(dict(prepare_request))
    except Exception as exc:
        ctx.logger.error(exc, event="acceptance_workspace_restore_failed", run_id=work.run_id)
        raise _WorkspaceRestoreError(str(exc)) from exc


def _parse_author_reply(text: str | None) -> dict[str, Any] | None:
    raw = (text or "").strip()
    candidates = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, RecursionError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _run_author_station(
    ctx: ServiceContext,
    work: Work,
    run: RunScaffold,
    state: _RunState,
    *,
    profile: dict[str, str],
    base_sha: str,
    deadline: float,
    prepare_request: Mapping[str, Any],
) -> _Station | None:
    """Write the locked acceptance tests in the SAME workspace the builder
    will use. Returns the pinned station, or `None` after restoring a clean
    workspace (any skip/failure: the normal flow then continues without
    acceptance checks). Raises `_WorkspaceRestoreError` if the restore fails."""
    station, reason, touched = _author_attempt(
        ctx, work, run, state, profile=profile, base_sha=base_sha, deadline=deadline
    )
    if station is not None:
        return station
    if run.cancel.is_set():
        return None
    ctx.logger.event(
        "acceptance_station_skipped",
        level="warning",
        run_id=work.run_id,
        reason=ctx.redactor.redact(reason)[:300],
    )
    state.status = "skipped"
    if touched:
        _restore_workspace(ctx, work, prepare_request)
    return None


def _author_attempt(
    ctx: ServiceContext,
    work: Work,
    run: RunScaffold,
    state: _RunState,
    *,
    profile: dict[str, str],
    base_sha: str,
    deadline: float,
) -> tuple[_Station | None, str, bool]:
    """`(station, skip_reason, workspace_touched)`: `workspace_touched` is True
    once the author exec ran, i.e. the workspace needs restoring on a skip."""
    # Before any author cost: without a `check` method on the Codex runner (an
    # older runner) the gate could never run, so do not write tests for it.
    if not callable(getattr(ctx.codex, "check", None)):
        return None, "the Codex runner has no check support", False
    remaining = deadline - _monotonic()
    if remaining < _MIN_AUTHOR_REMAINING_S:
        return None, "not enough run budget left for the test author", False
    matrix = ctx.settings.model_matrix["test_author"]
    task_file = acceptance.task_file_path(profile, work.task_id)
    state.author_model = str(matrix["model"])
    try:
        result = ctx.codex.run_exec(
            {
                "run_id": work.run_id,
                "lane": "code",
                "cwd": "wt",
                "model": matrix["model"],
                "effort": matrix["effort"],
                "sandbox": matrix["sandbox"],
                "multi_agent": bool(matrix["multi_agent"]),
                "prompt": compose_test_author_prompt(
                    work, task_file=task_file, acceptance_dir=profile["dir"]
                ),
                "images": [],
                "output_schema": acceptance.TEST_AUTHOR_SCHEMA,
                "timeout_s": _exec_timeout(
                    float(ctx.settings.timeouts["test_author"]), deadline
                ),
                "idle_timeout_s": ctx.settings.idle_timeout_s,
            },
            on_event=lambda _event: None,
            heartbeat=None,
            cancel=run.cancel,
        )
    except Exception as exc:
        ctx.logger.error(exc, event="acceptance_author_exec_failed", run_id=work.run_id)
        return None, f"test author exec failed: {type(exc).__name__}", True
    state.add_author(result.usage)
    if run.cancel.is_set() or result.cancelled:
        return None, "cancelled", True
    if result.timed_out or result.idle_killed or result.exit_code != 0:
        return None, "test author exec did not finish", True

    reply = _parse_author_reply(result.final_message)
    if reply is None or reply.get("status") != "ok":
        status = reply.get("status") if reply is not None else "unparseable"
        return None, f"test author reported {status!s}"[:200], True

    try:
        package = ctx.codex.package({"run_id": work.run_id})
        # Bytecode caches the author left behind are artefacts, not violations.
        raw = acceptance.drop_artifacts(
            base64.b64decode(package.get("patch_b64") or "", validate=True)
        )
        acceptance.validate_author_patch(raw, profile=profile, task_id=work.task_id)
        _text, pinned = _run_preflight(ctx, work, patch=raw, base_sha=base_sha)
        if pinned != raw:
            # Preflight reformatted the tests: what it returns is the pin (and
            # must itself be a valid author patch).
            task_path, test_count = acceptance.validate_author_patch(
                pinned, profile=profile, task_id=work.task_id
            )
        else:
            task_path, test_count = acceptance.validate_author_patch(
                raw, profile=profile, task_id=work.task_id
            )
    except acceptance.AcceptanceError as exc:
        return None, str(exc), True
    except PublishError as exc:
        return None, f"author patch failed preflight: {exc.reason}", True
    except Exception as exc:
        ctx.logger.error(exc, event="acceptance_author_package_failed", run_id=work.run_id)
        return None, f"author patch handling failed: {type(exc).__name__}", True

    ctx.logger.event(
        "acceptance_authored",
        run_id=work.run_id,
        task_id=work.task_id,
        sha256=acceptance.sha256_hex(pinned),
        raw_sha256=acceptance.sha256_hex(raw),
        preflight_reformatted=pinned != raw,
        paths=[task_path],
        tests=test_count,
        unchecked=len(reply.get("unchecked") or []),
    )
    run.stage("acceptance_authored", base_sha=base_sha)
    return (
        _Station(
            profile=profile,
            task_file=task_path,
            pinned=pinned,
            test_count=test_count,
            raw=raw,
        ),
        "",
        True,
    )


def _resolve_test_dispute(
    ctx: ServiceContext,
    work: Work,
    run: RunScaffold,
    state: _RunState,
    *,
    station: _Station,
    conflict: SpecConflict,
    base_sha: str,
    deadline: float,
) -> tuple[_Station, bytes] | None:
    """Send a spec conflict raised against the locked tests back to the test
    author once. Returns `(revised station, workspace patch)` when the author
    revised the tests into a valid acceptance patch, else `None` (the conflict
    stands: upheld tests, no dispute left, no budget, or an unusable
    revision). Raises `_RunEnded` on cancel."""
    if state.disputes >= MAX_TEST_DISPUTES:
        return None
    if deadline - _monotonic() < _MIN_AUTHOR_REMAINING_S:
        _log_dispute(ctx, work, conflict, "no_budget")
        return None
    state.disputes += 1
    profile = station.profile
    acceptance_dir = profile["dir"]
    try:
        before = base64.b64decode(
            ctx.codex.package({"run_id": work.run_id}).get("patch_b64") or "", validate=True
        )
    except Exception as exc:
        ctx.logger.error(exc, event="acceptance_dispute_package_failed", run_id=work.run_id)
        return None
    if acceptance.drop_artifacts(
        acceptance.acceptance_sections(before, acceptance_dir)
    ) not in (
        station.raw,
        station.pinned,
    ):
        # The disputing side already edited the locked tests: never let the
        # author build on that.
        _log_dispute(ctx, work, conflict, "tests_already_modified")
        return None
    matrix = ctx.settings.model_matrix["test_author"]
    try:
        result = ctx.codex.run_exec(
            {
                "run_id": work.run_id,
                "lane": "code",
                "cwd": "wt",
                "model": matrix["model"],
                "effort": matrix["effort"],
                "sandbox": matrix["sandbox"],
                "multi_agent": bool(matrix["multi_agent"]),
                "prompt": compose_test_dispute_prompt(
                    work,
                    task_file=station.task_file,
                    acceptance_dir=acceptance_dir,
                    criterion=conflict.criterion,
                    reason=ctx.redactor.redact(conflict.reason),
                ),
                "images": [],
                "output_schema": acceptance.TEST_AUTHOR_SCHEMA,
                "timeout_s": _exec_timeout(
                    float(ctx.settings.timeouts["test_author"]), deadline
                ),
                "idle_timeout_s": ctx.settings.idle_timeout_s,
            },
            on_event=lambda _event: None,
            heartbeat=None,
            cancel=run.cancel,
        )
    except Exception as exc:
        ctx.logger.error(exc, event="acceptance_dispute_exec_failed", run_id=work.run_id)
        return None
    state.add_author(result.usage)
    if run.cancel.is_set() or result.cancelled:
        raise _RunEnded
    if result.timed_out or result.idle_killed or result.exit_code != 0:
        _log_dispute(ctx, work, conflict, "author_exec_failed")
        return None
    reply = _parse_author_reply(result.final_message)
    if reply is None or reply.get("status") != "ok":
        _log_dispute(ctx, work, conflict, "author_not_ok")
        return None
    try:
        after = base64.b64decode(
            ctx.codex.package({"run_id": work.run_id}).get("patch_b64") or "", validate=True
        )
    except Exception as exc:
        ctx.logger.error(exc, event="acceptance_dispute_package_failed", run_id=work.run_id)
        return None
    # The author may only touch its own test file: the implementation must be
    # byte-for-byte what the builder left.
    code_before = acceptance.drop_artifacts(
        acceptance.strip_acceptance(before, acceptance_dir)
    )
    code_after = acceptance.drop_artifacts(acceptance.strip_acceptance(after, acceptance_dir))
    if code_before != code_after:
        _log_dispute(ctx, work, conflict, "author_touched_code")
        return None
    raw = acceptance.drop_artifacts(acceptance.acceptance_sections(after, acceptance_dir))
    if raw == acceptance.drop_artifacts(
        acceptance.acceptance_sections(before, acceptance_dir)
    ):
        _log_dispute(ctx, work, conflict, "upheld")
        return None
    try:
        acceptance.validate_author_patch(raw, profile=profile, task_id=work.task_id)
        _text, pinned = _run_preflight(ctx, work, patch=raw, base_sha=base_sha)
        task_path, test_count = acceptance.validate_author_patch(
            pinned, profile=profile, task_id=work.task_id
        )
    except acceptance.AcceptanceError as exc:
        _log_dispute(ctx, work, conflict, "invalid_revision", detail=str(exc))
        return None
    except PublishError as exc:
        _log_dispute(ctx, work, conflict, "revision_failed_preflight", detail=exc.reason)
        return None
    except Exception as exc:
        ctx.logger.error(exc, event="acceptance_dispute_package_failed", run_id=work.run_id)
        return None
    _log_dispute(
        ctx,
        work,
        conflict,
        "revised",
        sha256=acceptance.sha256_hex(pinned),
        tests=test_count,
    )
    run.stage("acceptance_authored", base_sha=base_sha)
    return (
        _Station(
            profile=profile,
            task_file=task_path,
            pinned=pinned,
            test_count=test_count,
            raw=raw,
        ),
        after,
    )


def _log_dispute(
    ctx: ServiceContext, work: Work, conflict: SpecConflict, outcome: str, **fields: Any
) -> None:
    if "detail" in fields:
        fields["detail"] = ctx.redactor.redact(str(fields["detail"]))[:300]
    ctx.logger.event(
        "acceptance_dispute",
        level="info" if outcome == "revised" else "warning",
        run_id=work.run_id,
        criterion=conflict.criterion,
        outcome=outcome,
        **fields,
    )


def _splice_and_gate(
    ctx: ServiceContext,
    work: Work,
    run: RunScaffold,
    state: _RunState,
    *,
    station: _Station,
    base_sha: str,
    patch: bytes,
    deadline: float,
) -> bytes:
    """Splice the pinned acceptance sections into the builder's patch, run the
    Check Gate and (on failure, budget permitting) up to `max_fixups` fix-ups.
    Returns the final spliced patch to publish -- the PR is opened whether or
    not the gate passed. Raises `_RunEnded` on cancel or a spec conflict that
    the test author upheld (a revised dispute re-runs the gate instead)."""
    acceptance_dir = station.profile["dir"]
    patch, modified = acceptance.splice(
        patch, station.pinned, acceptance_dir, unmodified=(station.raw,)
    )
    state.acceptance_modified = state.acceptance_modified or modified
    if modified:
        ctx.logger.event(
            "acceptance_modified_by_builder", level="warning", run_id=work.run_id
        )
    max_fixups = ctx.settings.max_fixups
    while True:
        outcome = _check_once(
            ctx, work, run, station=station, base_sha=base_sha, patch=patch, deadline=deadline
        )
        if run.cancel.is_set():
            raise _RunEnded
        state.status = outcome["status"]
        state.total = outcome["total"]
        state.passed = outcome["passed"]
        state.failed = outcome["failed"]
        if outcome["status"] == "passed":
            run.stage("check_gate_passed", base_sha=base_sha)
            return patch
        if outcome["status"] != "failed":
            # error / unavailable / skipped: no fix-ups.
            return patch
        if state.fixups >= max_fixups or deadline - _monotonic() < FIXUP_MIN_REMAINING_S:
            run.stage("check_gate_failed", base_sha=base_sha)
            return patch
        state.fixups += 1
        try:
            new_patch = _run_fixup(
                ctx,
                work,
                run,
                state,
                station=station,
                outcome=outcome,
                deadline=deadline,
            )
        except _TestDispute as dispute:
            resolved = _resolve_test_dispute(
                ctx,
                work,
                run,
                state,
                station=station,
                conflict=dispute.conflict,
                base_sha=base_sha,
                deadline=deadline,
            )
            if resolved is None:
                _fail_spec_conflict(ctx, work, run, state, dispute.conflict)
                raise _RunEnded from None
            station, workspace_patch = resolved
            # The fix-up's code plus the revised tests, gated again.
            new_patch, modified = acceptance.splice(
                workspace_patch, station.pinned, acceptance_dir, unmodified=(station.raw,)
            )
            state.acceptance_modified = state.acceptance_modified or modified
        if new_patch is None:
            run.stage("check_gate_failed", base_sha=base_sha)
            return patch
        patch = new_patch


def _check_once(
    ctx: ServiceContext,
    work: Work,
    run: RunScaffold,
    *,
    station: _Station,
    base_sha: str,
    patch: bytes,
    deadline: float,
) -> dict[str, Any]:
    acceptance_dir = station.profile["dir"]
    # The whole call (timeout + grace) must still leave the exec reserve.
    timeout_s = min(
        float(ctx.settings.timeouts["check"]),
        deadline - _monotonic() - EXEC_RESERVE_S - _CHECK_GRACE_S,
    )
    if timeout_s < _MIN_EXEC_TIMEOUT_S:
        return _check_outcome("skipped", detail="not enough run budget left for the check")
    request = {
        "run_id": work.run_id,
        "repo": work.repo_full_name,
        "mirror": str(ctx.mirrors.mirror_path(work.repo_full_name)),
        "base_sha": base_sha,
        "patch_b64": base64.b64encode(acceptance.strip_acceptance(patch, acceptance_dir)).decode(
            "ascii"
        ),
        "pinned_b64": base64.b64encode(station.pinned).decode("ascii"),
        "profile": station.profile["profile"],
        "task_id": work.task_id,
        "expected_tests": station.test_count,
        "timeout_s": timeout_s,
    }
    try:
        reply = ctx.codex.check(request, timeout_s=timeout_s + _CHECK_GRACE_S, cancel=run.cancel)
    except Exception as exc:
        # `CodexChildError` (a child refusal) and anything else alike: the
        # gate is a best-effort extra, never a reason to lose the run.
        ctx.logger.error(exc, event="check_gate_call_failed", run_id=work.run_id)
        return _check_outcome("error", detail=type(exc).__name__)
    outcome = _check_outcome_from_reply(reply)
    ctx.logger.event(
        "check_gate_result",
        run_id=work.run_id,
        status=outcome["status"],
        total=outcome["total"],
        passed=outcome["passed"],
        failed=outcome["failed"][: acceptance.MAX_FAILED_IDS],
        detail=ctx.redactor.redact(outcome["detail"])[:300],
    )
    return outcome


def _nonneg_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _check_outcome(
    status: str,
    *,
    total: int = 0,
    passed: int = 0,
    failed: list[str] | None = None,
    output_tail: str = "",
    detail: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "total": total,
        "passed": passed,
        "failed": failed or [],
        "output_tail": output_tail,
        "detail": detail,
    }


def _check_outcome_from_reply(reply: Any) -> dict[str, Any]:
    if not isinstance(reply, Mapping):
        return _check_outcome("error", detail="unusable check reply")
    status = reply.get("status")
    if status not in _CHECK_STATUSES:
        return _check_outcome("error", detail="unknown check status")
    failed_raw = reply.get("failed")
    failed = (
        [item for item in failed_raw if isinstance(item, str)]
        if isinstance(failed_raw, list)
        else []
    )
    tail = reply.get("output_tail")
    detail = reply.get("detail")
    # Same normalisation the reported `check_gate` gets, so a bogus "failed"
    # (zero tests) is an error here too and never triggers a fix-up.
    status, total, passed, failed = acceptance.normalise_result(
        str(status), _nonneg_int(reply.get("total")), _nonneg_int(reply.get("passed")), failed
    )
    return _check_outcome(
        status,
        total=total,
        passed=passed,
        failed=failed,
        output_tail=tail[-_MAX_OUTPUT_TAIL:] if isinstance(tail, str) else "",
        detail=detail[:300] if isinstance(detail, str) else "",
    )


def _run_fixup(
    ctx: ServiceContext,
    work: Work,
    run: RunScaffold,
    state: _RunState,
    *,
    station: _Station,
    outcome: Mapping[str, Any],
    deadline: float,
) -> bytes | None:
    """One fix-up exec in the same workspace, then re-package and re-splice.
    Returns the new spliced patch, or `None` if the fix-up produced nothing
    usable (the caller keeps the previous patch). Raises `_RunEnded` on
    cancel or a spec conflict."""
    matrix = ctx.settings.model_matrix["fixup"]
    state.fixup_model = str(matrix["model"])
    try:
        result = ctx.codex.run_exec(
            {
                "run_id": work.run_id,
                "lane": "code",
                "cwd": "wt",
                "model": matrix["model"],
                "effort": matrix["effort"],
                "sandbox": matrix["sandbox"],
                "multi_agent": bool(matrix["multi_agent"]),
                "prompt": compose_fixup_prompt(
                    work,
                    task_file=station.task_file,
                    task_id=work.task_id,
                    # Test output comes from running repository code: redact
                    # it here, whatever the child already did.
                    failed=[ctx.redactor.redact(name) for name in outcome["failed"]],
                    output_tail=ctx.redactor.redact(outcome["output_tail"]),
                ),
                "images": [],
                "output_schema": None,
                "timeout_s": _exec_timeout(float(ctx.settings.timeouts["fixup"]), deadline),
                "idle_timeout_s": ctx.settings.idle_timeout_s,
            },
            on_event=lambda _event: None,
            heartbeat=None,
            cancel=run.cancel,
        )
    except Exception as exc:
        ctx.logger.error(exc, event="fixup_exec_failed", run_id=work.run_id)
        return None
    state.add_fixup(result.usage)
    if run.cancel.is_set() or result.cancelled:
        raise _RunEnded
    stripped, conflict = split_spec_conflict(result.final_message)
    _log_ignored_marker(ctx, work, result.final_message, stripped, conflict)
    if conflict is not None:
        if state.disputes < MAX_TEST_DISPUTES:
            raise _TestDispute(conflict)
        _fail_spec_conflict(ctx, work, run, state, conflict)
        raise _RunEnded
    if result.timed_out or result.idle_killed or result.exit_code != 0:
        ctx.logger.event(
            "fixup_codex_failed",
            level="warning",
            run_id=work.run_id,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            idle_killed=result.idle_killed,
        )
        return None
    try:
        package = ctx.codex.package({"run_id": work.run_id})
        patch = base64.b64decode(package.get("patch_b64") or "", validate=True)
    except Exception as exc:
        ctx.logger.error(exc, event="fixup_package_failed", run_id=work.run_id)
        return None
    acceptance_dir = station.profile["dir"]
    if not acceptance.strip_acceptance(patch, acceptance_dir).strip():
        return None
    patch, modified = acceptance.splice(
        patch, station.pinned, acceptance_dir, unmodified=(station.raw,)
    )
    state.acceptance_modified = state.acceptance_modified or modified
    return patch


def _validate_task(ctx: ServiceContext, work: Work) -> tuple[dict[str, Any], bool]:
    is_public = is_public_repo(ctx, work.repo_full_name)
    raw: dict[str, Any] = {
        "task_id": work.task_id,
        "run_id": work.run_id,
        "title": work.title,
        "description": work.description,
        "base_branch": work.base_branch,
        "mode": work.mode,
        "repo_full_name": work.repo_full_name,
        "task_revision": work.task_revision,
    }
    if is_public:
        task = ctx.trusted.public_agent_task.parse_public_task(raw, qa_enabled=True)
    else:
        task = ctx.trusted.agent_task.parse_task(raw)
    return dict(task), is_public


def is_public_repo(ctx: ServiceContext, repo_full_name: str) -> bool:
    """Legacy `public_agent_task.approved_repositories`: the 3 public repos
    PLUS agent-qa when QA is enabled -- never by GitHub visibility alone.
    agent-qa is `private=True` in the catalog (its own token/visibility is
    private), but `qa_only` repos get the same PUBLIC validator/prompt path
    (the extra AGENTS.md/CLAUDE.md/.github//.codex//.agents/ path blocks
    `public_agent_task.check_diff` adds) as the 3 genuinely public repos,
    exactly like the trusted GitHub Actions publisher does. `ctx.api` already
    proved `repo_full_name` is one of `ctx.catalog`'s approved repos before
    handing us this `Work`, so there is no separate "is QA enabled" flag to
    consult here -- if agent-svc leased work for agent-qa at all, QA is
    enabled.
    """
    info = ctx.catalog.get(repo_full_name)
    return info is not None and (not info.private or info.qa_only)


def _ops_project_key(ctx: ServiceContext, work: Work) -> str | None:
    """The allowlist project key for `work.repo_full_name`, or `None` if
    (implausibly, since `parse_work` already proved this repo is in the
    approved catalog) it has none. The allowlist itself keys projects by
    this same `project_key`, never by `repo_full_name` directly."""
    info = ctx.catalog.get(work.repo_full_name)
    return info.project_key if info is not None else None


def _load_ops_allowlist(ctx: ServiceContext, run_id: str) -> Any:
    """Load the ops-requests allowlist fresh for this one run (never cached
    across runs -- an operator may edit it between two runs). Missing or
    invalid -> `None`: every caller treats that exactly like "no allowlist
    entry for this project" (no ops rules in the prompt, every proposal
    denied with `policy_reason="no_allowlist"`); this must never crash an
    implement run over a secondary feature. Logged once, here, on failure.

    A separate, private function (rather than inlining `ctx.trusted.
    agent_ops_policy.load_allowlist(...)` at each call site) so tests can
    substitute a canned `Allowlist` with `unittest.mock.patch.object` instead
    of needing a real root-owned file on disk (`require_root_owned=True` is
    always passed in production, per the spec, and would refuse any file a
    non-root test process could create).
    """
    try:
        return ctx.trusted.agent_ops_policy.load_allowlist(
            ctx.settings.ops_allowlist_path, ctx.catalog.repos, require_root_owned=True
        )
    except Exception as exc:
        ctx.logger.error(exc, event="ops_allowlist_load_failed", run_id=run_id)
        return None


def _redact_ops_values(text: str, proposals: list[dict[str, Any]]) -> str:
    """Defense in depth: Codex might repeat a requested value in its own
    prose -- outside the trailer line itself, which `split_trailer` already
    removes -- e.g. "I'm asking the owner to add 5339875840 as an admin".
    Replace every occurrence of a proposal's `value` (allowed or denied)
    with a fixed placeholder before this text ever reaches a PR body or a
    Telegram message. Only values at least `_MIN_REDACTED_VALUE_LEN`
    characters long are redacted, so a short, generic value (a single
    digit, "on"/"off", ...) never mangles ordinary prose that happens to
    contain the same short substring by coincidence.
    """
    if not text or not proposals:
        return text
    values: set[str] = set()
    for proposal in proposals:
        value = proposal.get("value")
        if isinstance(value, str) and len(value) >= _MIN_REDACTED_VALUE_LEN:
            values.add(value)
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, _OPS_VALUE_PLACEHOLDER)
    return text


def _download_images(
    ctx: ServiceContext, work: Work, run: RunScaffold
) -> tuple[Path | None, list[Path]]:
    if work.image_count <= 0:
        return None, []
    target = run.run_dir / "images"
    paths = ctx.trusted.agent_images.download_images(
        work.run_id,
        ctx.settings.callback_token,
        target,
        api_base=ctx.settings.api_base_url,
    )
    image_dir = target / "agent-images"
    _make_images_readable_by_agent_codex(image_dir, paths)
    return image_dir, list(paths)


def _make_images_readable_by_agent_codex(image_dir: Path, paths: list[Path]) -> None:
    """`agent_images.download_images` (trusted, runs as agent-svc) creates
    `image_dir` mode 0700 and each file mode 0600 -- agent-svc only.
    agent-codex (group `agentwork`) must be able to READ these: they are
    handed straight to `codex exec --image <path>`, which runs as
    agent-codex. `image_dir`'s GROUP is already `agentwork` (inherited via
    the setgid bit on `run_dir/images`, its parent -- see
    `repos.make_run_dir` -- POSIX propagates a setgid directory's group to
    everything created under it); only the permission bits need relaxing.
    """
    image_dir.chmod(0o2750)
    for path in paths:
        path.chmod(0o640)


def _pr_matches_this_run(
    pr: Mapping[str, Any], repo: str, branch: str, base_branch: str
) -> bool:
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    return (
        head.get("ref") == branch
        and (head.get("repo") or {}).get("full_name", "").lower() == repo.lower()
        and base.get("ref") == base_branch
    )


def _recover_existing_branch(
    ctx: ServiceContext, work: Work, run: RunScaffold, branch: str
) -> None:
    """The branch this run would push already exists: a previous attempt got
    at least as far as `branch_pushed` before agent-svc crashed, was killed,
    or otherwise never got to report back. Never push to it (see the branch-
    exists guard above `_run_implement`); instead prove it is genuinely THIS
    run's own commit (the exact `Agent-Run-ID` trailer, not merely a branch
    that happens to share the name) and recover from wherever publication
    actually stopped: reopen the PR if one is missing (a crash between push
    and PR creation), or just report the existing one.
    """
    trailer = f"Agent-Run-ID: {work.run_id}"
    try:
        head_sha = ctx.github.get_ref(work.repo_full_name, branch)
        if head_sha is None:
            raise ValueError("branch disappeared during recovery")
        message = ctx.github.commit_message(work.repo_full_name, head_sha)
        if trailer not in message.splitlines():
            _fail(ctx, work, run, "implement", "branch already exists")
            return

        pr = ctx.github.find_open_pull_by_head(work.repo_full_name, branch)
        if pr is not None:
            if not _pr_matches_this_run(pr, work.repo_full_name, branch, work.base_branch):
                _fail(ctx, work, run, "implement", "branch already exists")
                return
            pr_url = pr.get("html_url")
        else:
            # The push succeeded but agent-svc never got to (or failed to)
            # open the PR. Do it now instead of failing a run whose branch
            # is already live and correctly attributed.
            _check_not_cancelled(ctx, work, run.cancel)
            created = ctx.github.create_pull(
                work.repo_full_name,
                head=branch,
                base=work.base_branch,
                title=f"Task #{work.task_id}: Codex change",
                body=(
                    f"Task Manager task #{work.task_id}\n\n"
                    "Recovered after an interrupted publish: the branch was already "
                    "pushed by this exact run. Review the diff and CI results before "
                    "merging.\n"
                ),
            )
            number = created.get("number")
            pr_url = created.get("html_url") or (
                f"https://github.com/{work.repo_full_name}/pull/{number}"
            )

        _callback(
            ctx,
            work,
            run,
            {
                "run_id": work.run_id,
                "status": "pr_opened",
                "pr_url": pr_url,
                "head_sha": head_sha,
            },
        )
        return
    except Exception as exc:
        ctx.logger.error(exc, event="branch_recovery_failed", run_id=work.run_id)
    _fail(ctx, work, run, "implement", "branch already exists")


def _usage_fields(usage: Mapping[str, Any] | None) -> dict[str, int]:
    if not usage:
        return {}
    fields: dict[str, int] = {}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            fields[key] = value
    return fields


def _fail(
    ctx: ServiceContext,
    work: Work,
    run: RunScaffold,
    phase: str,
    reason: str,
    *,
    usage: Mapping[str, Any] | None = None,
    ops_requests: list[dict[str, Any]] | None = None,
    ops_note: str | None = None,
    check_gate: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "run_id": work.run_id,
        "status": "failed",
        "error": reason[:900],
        "failure_phase": phase,
    }
    payload.update(_usage_fields(usage))
    if check_gate is not None:
        payload[_CALLBACK_GATE_KEY] = check_gate
    # Attached whenever there is anything ops-related to report at all --
    # not just a non-empty proposal list, but also a note-only outcome (a
    # trailer was present but turned out ambiguous/invalid/all-dropped: see
    # `_run_implement`'s ops block and `ops_requests.build_ops_note`).
    # Every other `_fail` call site (no trailer, nothing dropped) is
    # unaffected and keeps sending exactly the payload it always has.
    if ops_requests or ops_note:
        payload["ops_requests"] = ops_requests or []
        payload["ops_note"] = ops_note
    _callback(ctx, work, run, payload)


def _callback(
    ctx: ServiceContext, work: Work, run: RunScaffold, payload: Mapping[str, Any]
) -> None:
    if run.cancel.is_set():
        return
    run.deliver(lambda: _post_callback(ctx, work, payload))


def _post_callback(ctx: ServiceContext, work: Work, payload: Mapping[str, Any]) -> None:
    """One callback delivery attempt, with one extra layer `run.deliver`
    itself does not have: if the payload carries ops fields (or the
    `check_gate` summary -- an older backend without that field rejects it)
    and the backend rejects it with a 400/422 (a malformed `ops_requests`/
    `ops_note` -- which, thanks to the checks in `ops_requests.py`, should
    never happen, but a defense this cheap is worth having anyway), resend
    ONCE without those fields rather than losing the run's own real outcome
    (`pr_opened`/`ops_pending`/`failed`) over a secondary feature. The
    rejection is logged by status code only -- never the response body,
    which could otherwise echo back attacker-influenced field values
    (`kind`/`op`) into the log.
    """
    try:
        ctx.api.callback(work.run_id, work.lease_id, dict(payload))
    except HttpError as exc:
        if exc.status not in (400, 422):
            raise
        # Resend steps, least destructive first: only `check_gate` (an older
        # backend that does not know the field), then everything optional.
        steps: list[tuple[str, ...]] = []
        if _CALLBACK_GATE_KEY in payload:
            steps.append((_CALLBACK_GATE_KEY,))
        if any(key in payload for key in _OPS_CALLBACK_KEYS):
            steps.append((*_OPS_CALLBACK_KEYS, _CALLBACK_GATE_KEY))
        if not steps:
            raise
        failure: HttpError = exc
        for drop in steps:
            ctx.logger.event(
                "ops_callback_rejected_retrying_without_ops",
                level="warning",
                run_id=work.run_id,
                status=failure.status,
                dropping=list(drop),
            )
            stripped = {key: value for key, value in payload.items() if key not in drop}
            if stripped.get("status") == "ops_pending" and "ops_requests" not in stripped:
                # `ops_pending` is only valid with at least one stored
                # proposal; without them the backend would reject it too and
                # the run would never be told anything. Report the no-patch
                # outcome as a failure.
                stripped["status"] = "failed"
                stripped["error"] = (
                    "Agent faqat ops so‘rovi qaytardi, lekin so‘rov qabul qilinmadi."
                )
            try:
                ctx.api.callback(work.run_id, work.lease_id, stripped)
                return
            except HttpError as retry_exc:
                if retry_exc.status not in (400, 422):
                    raise
                failure = retry_exc
        raise failure from exc
