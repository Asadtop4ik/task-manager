"""Client for the agent-svc <-> Task Manager API contract (v1).

Endpoints and header names follow `agent-svc-contract.md`. Lease responses are
parsed into a validated `Work` dataclass; an unknown or malformed payload
raises `InvalidWork` rather than being handed to a lane half-trusted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn
from uuid import UUID

from .http import HttpError, HttpResponse, JsonHttp
from .log import Logger

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_RELEVANT_FILE_RE = re.compile(r"[A-Za-z0-9_./-]{1,200}")
_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]{1,200}")
_KINDS = frozenset({"implement", "review", "correction"})
_COMPLEXITIES = frozenset({"simple", "complex"})
_MAX_RELEVANT_FILES = 12

STAGES = frozenset(
    {
        "leased",
        "workspace_ready",
        "codex_started",
        "codex_finished",
        "patch_validated",
        "preflight_passed",
        "branch_pushed",
        "review_started",
        "correction_pushed",
        "retrying",
    }
)


class InvalidResponse(ValueError):
    """The Task Manager API returned a response that does not match the contract."""


class InvalidWork(InvalidResponse):
    """A lease response failed validation. `run_id` is set when it was parseable."""

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


class LeaseLost(Exception):
    """A lease-bound call returned 409: the lease was cancelled, expired, or mismatched."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Work:
    run_id: str
    kind: Literal["implement", "review", "correction"]
    lease_id: str
    lease_until: datetime
    attempts: int
    attempt_index: int
    task_id: int
    task_revision: int | str
    repo_full_name: str
    base_branch: str
    mode: Literal["pr"]
    title: str
    description: str
    image_count: int
    complexity: Literal["simple", "complex"] | None
    relevant_files: tuple[str, ...]
    branch: str | None
    pr_url: str | None
    pr_number: int | None
    head_sha: str | None
    action_id: str | None
    instruction: str | None
    expected_head_sha: str | None


def _bad_int(value: Any, *, minimum: int) -> bool:
    return isinstance(value, bool) or not isinstance(value, int) or value < minimum


def _valid_branch(value: str) -> bool:
    return bool(
        _BRANCH_RE.fullmatch(value)
        and ".." not in value
        and "//" not in value
        and not value.startswith(("/", "-"))
        and not value.endswith(("/", ".lock", "."))
    )


def _valid_lease_id(value: Any) -> str | None:
    """Return the canonical uuid4 string form of `value`, or None if invalid.

    The contract sets `lease_id` via `uuid4()`; validating this strictly (not
    just "non-empty string") before it is ever placed into an
    `X-Agent-Lease-ID` header keeps a malformed or hostile value out of an
    HTTP header entirely.
    """
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_work(payload: Any, catalog: Mapping[str, str]) -> Work:
    """Validate one `AgentWorkOut` lease response against the contract."""
    if not isinstance(payload, dict):
        raise InvalidWork("lease response is not an object")

    run_id_raw = payload.get("run_id")
    try:
        run_id = str(UUID(str(run_id_raw)))
    except (ValueError, AttributeError, TypeError):
        raise InvalidWork("lease response has an invalid run_id") from None

    def fail(message: str) -> NoReturn:
        raise InvalidWork(message, run_id=run_id)

    kind = payload.get("kind")
    if kind not in _KINDS:
        fail("lease response has an invalid kind")
    lease_id = _valid_lease_id(payload.get("lease_id"))
    if lease_id is None:
        fail("lease response has an invalid lease_id")
    lease_until = _parse_iso(payload.get("lease_until"))
    if lease_until is None:
        fail("lease response has an invalid lease_until")
    attempts = payload.get("attempts")
    attempt_index = payload.get("attempt_index")
    if _bad_int(attempts, minimum=0) or _bad_int(attempt_index, minimum=0):
        fail("lease response has invalid attempt counters")
    assert isinstance(attempts, int) and isinstance(attempt_index, int)
    task_id = payload.get("task_id")
    if _bad_int(task_id, minimum=1):
        fail("lease response has an invalid task_id")
    assert isinstance(task_id, int)
    task_revision = payload.get("task_revision")
    if isinstance(task_revision, bool) or not isinstance(task_revision, (int, str)):
        fail("lease response has an invalid task_revision")
    if isinstance(task_revision, int) and task_revision < 1:
        fail("lease response has an invalid task_revision")
    if isinstance(task_revision, str) and not task_revision.strip():
        fail("lease response has an invalid task_revision")
    repo_full_name = payload.get("repo_full_name")
    base_branch = payload.get("base_branch")
    if not isinstance(repo_full_name, str) or catalog.get(repo_full_name) != base_branch:
        fail("lease response repository is not in the approved catalog")
    assert isinstance(base_branch, str)
    mode = payload.get("mode")
    if mode != "pr":
        # `!fast` runs always stay on the GitHub executor per the contract;
        # a local lease response claiming mode="fast" is either a backend
        # bug or a hostile payload, either way not something to act on.
        fail("lease response has an invalid mode (local execution is pr-mode only)")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 255:
        fail("lease response has an invalid title")
    description = payload.get("description")
    if not isinstance(description, str) or len(description) > 8000:
        fail("lease response has an invalid description")
    image_count = payload.get("image_count")
    if _bad_int(image_count, minimum=0):
        fail("lease response has an invalid image_count")
    assert isinstance(image_count, int)
    complexity = payload.get("complexity")
    if complexity is not None and complexity not in _COMPLEXITIES:
        fail("lease response has an invalid complexity")
    relevant_files_raw = payload.get("relevant_files")
    if (
        not isinstance(relevant_files_raw, list)
        or len(relevant_files_raw) > _MAX_RELEVANT_FILES
    ):
        fail("lease response has invalid relevant_files")
    relevant_files: list[str] = []
    for item in relevant_files_raw:
        if (
            not isinstance(item, str)
            or not _RELEVANT_FILE_RE.fullmatch(item)
            or ".." in item
            or item.startswith("/")
        ):
            fail("lease response has an unsafe relevant_files entry")
        relevant_files.append(item)
    branch = payload.get("branch")
    if branch is not None and (not isinstance(branch, str) or not _valid_branch(branch)):
        fail("lease response has an invalid branch")
    pr_url = payload.get("pr_url")
    if pr_url is not None:
        prefix = f"https://github.com/{repo_full_name}/pull/"
        if (
            not isinstance(pr_url, str)
            or not pr_url.startswith(prefix)
            or not pr_url[len(prefix) :].isdigit()
        ):
            fail("lease response has an invalid pr_url")
    pr_number = payload.get("pr_number")
    if pr_number is not None and _bad_int(pr_number, minimum=1):
        fail("lease response has an invalid pr_number")
    head_sha = payload.get("head_sha")
    if head_sha is not None and (
        not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha)
    ):
        fail("lease response has an invalid head_sha")
    expected_head_sha = payload.get("expected_head_sha")
    if expected_head_sha is not None and (
        not isinstance(expected_head_sha, str) or not _SHA_RE.fullmatch(expected_head_sha)
    ):
        fail("lease response has an invalid expected_head_sha")
    action_id = payload.get("action_id")
    if action_id is not None and not isinstance(action_id, str):
        fail("lease response has an invalid action_id")
    instruction = payload.get("instruction")
    if instruction is not None and not isinstance(instruction, str):
        fail("lease response has an invalid instruction")

    return Work(
        run_id=run_id,
        kind=kind,
        lease_id=lease_id,
        lease_until=lease_until,
        attempts=attempts,
        attempt_index=attempt_index,
        task_id=task_id,
        task_revision=task_revision,
        repo_full_name=repo_full_name,
        base_branch=base_branch,
        mode=mode,
        title=title.strip(),
        description=description,
        image_count=image_count,
        complexity=complexity,
        relevant_files=tuple(relevant_files),
        branch=branch,
        pr_url=pr_url,
        pr_number=pr_number,
        head_sha=head_sha,
        action_id=action_id,
        instruction=instruction,
        expected_head_sha=expected_head_sha,
    )


def _detail(exc: HttpError) -> str:
    try:
        parsed = json.loads(exc.snippet)
    except ValueError:
        return exc.snippet
    if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
        return parsed["detail"]
    return exc.snippet


def _reraise_lease_conflict(exc: HttpError) -> NoReturn:
    if exc.status == 409:
        raise LeaseLost(_detail(exc)) from exc
    raise exc


def _require_lease_id(lease_id: str) -> str:
    """Validate a caller-supplied lease_id as a uuid before it reaches a header."""
    valid = _valid_lease_id(lease_id)
    if valid is None:
        raise ValueError(f"invalid lease_id: {lease_id!r}")
    return valid


class TaskManagerApi:
    def __init__(
        self,
        base_url: str,
        svc_token: str,
        callback_token: str,
        intake_token: str,
        *,
        http: JsonHttp,
        catalog: Mapping[str, str],
        logger: Logger | None = None,
    ) -> None:
        if not svc_token.strip() or not callback_token.strip():
            raise ValueError("agent-svc API requires its service and callback tokens")
        self._base = base_url.rstrip("/") + "/agent-runs"
        self._svc_token = svc_token
        self._callback_token = callback_token
        # Reserved for a future intake-lease integration; not used by any
        # method below yet.
        self._intake_token = intake_token
        self._http = http
        self._catalog = catalog
        self._logger = logger

    def _svc_call(
        self, method: str, path: str, *, lease_id: str | None = None, body: Any = None
    ) -> HttpResponse:
        extra = (("X-Agent-Lease-ID", lease_id),) if lease_id is not None else ()
        request = self._http.build_request(
            method,
            f"{self._base}{path}",
            auth_header=("X-Agent-Svc-Token", self._svc_token),
            extra_headers=extra,
            body=_encode(body),
            content_type="application/json" if body is not None else None,
        )
        return self._http.send(request)

    def _callback_call(
        self, method: str, path: str, *, lease_id: str | None = None, body: Any = None
    ) -> HttpResponse:
        extra = (("X-Agent-Lease-ID", lease_id),) if lease_id is not None else ()
        request = self._http.build_request(
            method,
            f"{self._base}{path}",
            auth_header=("X-Agent-Callback-Token", self._callback_token),
            extra_headers=extra,
            body=_encode(body),
            content_type="application/json" if body is not None else None,
        )
        return self._http.send(request)

    def lease(self, lane: str) -> Work | None:
        response = self._svc_call("POST", "/lease", body={"lane": lane})
        if response.status == 204:
            return None
        return parse_work(self._http.json(response), self._catalog)

    def lease_code(self) -> Work | None:
        return self.lease("code")

    def heartbeat(self, run_id: str, lease_id: str) -> datetime:
        lease_id = _require_lease_id(lease_id)
        try:
            response = self._svc_call(
                "POST",
                f"/{run_id}/heartbeat",
                lease_id=lease_id,
                body={"lease_id": lease_id},
            )
        except HttpError as exc:
            _reraise_lease_conflict(exc)
        payload = self._http.json(response)
        lease_until = (
            _parse_iso(payload.get("lease_until")) if isinstance(payload, dict) else None
        )
        if lease_until is None:
            raise InvalidResponse("heartbeat response has an invalid lease_until")
        return lease_until

    def stage(
        self, run_id: str, lease_id: str, stage: str, *, error: str | None = None
    ) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage!r}")
        lease_id = _require_lease_id(lease_id)
        try:
            self._svc_call(
                "POST",
                f"/{run_id}/stage",
                lease_id=lease_id,
                body={"lease_id": lease_id, "stage": stage, "error": error},
            )
        except HttpError as exc:
            _reraise_lease_conflict(exc)

    def callback(self, run_id: str, lease_id: str, payload: Mapping[str, Any]) -> Any:
        lease_id = _require_lease_id(lease_id)
        try:
            response = self._callback_call(
                "POST", f"/{run_id}/callback", lease_id=lease_id, body=dict(payload)
            )
        except HttpError as exc:
            _reraise_lease_conflict(exc)
        return None if response.status == 204 else self._http.json(response)

    def review_result(self, run_id: str, lease_id: str, payload: Mapping[str, Any]) -> Any:
        lease_id = _require_lease_id(lease_id)
        try:
            response = self._callback_call(
                "POST", f"/{run_id}/review-result", lease_id=lease_id, body=dict(payload)
            )
        except HttpError as exc:
            _reraise_lease_conflict(exc)
        return None if response.status == 204 else self._http.json(response)

    def action_result(self, run_id: str, lease_id: str, payload: Mapping[str, Any]) -> Any:
        lease_id = _require_lease_id(lease_id)
        try:
            response = self._callback_call(
                "POST", f"/{run_id}/action-result", lease_id=lease_id, body=dict(payload)
            )
        except HttpError as exc:
            _reraise_lease_conflict(exc)
        return None if response.status == 204 else self._http.json(response)

    def status(self, run_id: str) -> Any:
        response = self._callback_call("GET", f"/{run_id}/status")
        return self._http.json(response)

    def ci_pending(self, after_id: int = 0) -> Any:
        response = self._callback_call("GET", f"/ci-pending?after_id={after_id}")
        return self._http.json(response)

    def external_pending(self, after_id: int = 0) -> Any:
        response = self._callback_call("GET", f"/external-pending?after_id={after_id}")
        return self._http.json(response)

    def ci_result(
        self, run_id: str, *, sha: str, conclusion: str, github_run_url: str | None = None
    ) -> Any:
        response = self._callback_call(
            "POST",
            f"/{run_id}/ci-result",
            body={"sha": sha, "conclusion": conclusion, "github_run_url": github_run_url},
        )
        return None if response.status == 204 else self._http.json(response)

    def merged(self, run_id: str, *, sha: str) -> Any:
        response = self._callback_call("POST", f"/{run_id}/merged", body={"sha": sha})
        return None if response.status == 204 else self._http.json(response)

    def deployed(self, run_id: str, *, sha: str, github_run_url: str) -> Any:
        response = self._callback_call(
            "POST",
            f"/{run_id}/deployed",
            body={"sha": sha, "github_run_url": github_run_url},
        )
        return None if response.status == 204 else self._http.json(response)


def _encode(body: Any) -> bytes | None:
    if body is None:
        return None
    return json.dumps(body, ensure_ascii=False).encode("utf-8")
