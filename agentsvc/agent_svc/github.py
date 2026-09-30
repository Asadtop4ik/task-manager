"""GitHub REST client used by agent-svc: refs, pulls, diffs, statuses, CI runs.

Token selection is per repository, matching the trusted catalog: the private
dispatch repository and the QA repository each use their own token; every
other (public) catalog repository shares the public agent token. An
unrecognized repository is refused rather than silently using a token that
was not scoped for it. An empty `qa_token` (the optional `github_qa_token`
secret was never provisioned) is treated the same as an unrecognized
repository: the QA repo/lane is disabled rather than calling GitHub with no
credential.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import quote

from .http import HttpError, HttpResponse, JsonHttp

_API_BASE = "https://api.github.com"
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_MAX_DIFF_FETCH_BYTES = 1024 * 1024
_MAX_DIFF_CHARS = 250_000
_STATUS_STATES = frozenset({"error", "failure", "pending", "success"})


class InvalidResponse(ValueError):
    """GitHub returned a response that does not match the expected shape."""


class DiffTooLarge(ValueError):
    """The PR diff exceeds what a review prompt may carry; never truncate it."""


class UnknownRepository(ValueError):
    """No token is configured for this repository; refuse rather than guess one."""


def build_token_selector(
    *,
    dispatch_repo: str,
    qa_repo: str | None,
    public_repos: Iterable[str],
    agent_token: str,
    qa_token: str,
    public_token: str,
) -> Callable[[str], str]:
    """Build the `token_for(repo)` callable per the trusted-catalog token policy."""
    public_set = frozenset(public_repos)

    def token_for(repo: str) -> str:
        if repo == dispatch_repo:
            return agent_token
        if qa_repo is not None and repo == qa_repo:
            if not qa_token:
                # No QA credential provisioned: treat the QA repo as disabled
                # rather than authenticating with an empty token.
                raise UnknownRepository(repo)
            return qa_token
        if repo in public_set:
            return public_token
        raise UnknownRepository(repo)

    return token_for


class GitHubClient:
    def __init__(
        self,
        *,
        token_for: Callable[[str], str],
        http: JsonHttp,
        api_base: str = _API_BASE,
        log_redirect_schemes: Iterable[str] = ("https",),
    ) -> None:
        self._log_redirect_schemes = tuple(log_redirect_schemes)
        self._token_for = token_for
        self._http = http
        self._api_base = api_base.rstrip("/")

    def _call(
        self,
        method: str,
        path: str,
        *,
        repo: str,
        body: Any = None,
        accept: str = "application/vnd.github+json",
        max_bytes: int | None = None,
        timeout: float | None = None,
        keep_tail: bool = False,
        retries: int | None = None,
        deadline: float | None = None,
        redirect_schemes: Iterable[str] | None = None,
    ) -> HttpResponse:
        token = self._token_for(repo)
        request = self._http.build_request(
            method,
            f"{self._api_base}{path}",
            auth_header=("Authorization", f"Bearer {token}"),
            extra_headers=(("X-GitHub-Api-Version", "2022-11-28"),),
            body=json.dumps(body).encode("utf-8") if body is not None else None,
            content_type="application/json" if body is not None else None,
            accept=accept,
        )
        extra: dict[str, Any] = {}
        # Only passed when set, so a stand-in `send` without them keeps working.
        if retries is not None:
            extra["retries"] = retries
        if deadline is not None:
            extra["deadline"] = deadline
        if redirect_schemes is not None:
            extra["redirect_schemes"] = redirect_schemes
        return self._http.send(
            request, max_bytes=max_bytes, timeout=timeout, keep_tail=keep_tail, **extra
        )

    def get_ref(self, repo: str, branch: str) -> str | None:
        try:
            response = self._call(
                "GET", f"/repos/{repo}/git/ref/heads/{quote(branch, safe='/')}", repo=repo
            )
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise
        payload = self._http.json(response)
        sha = (payload.get("object") or {}).get("sha") if isinstance(payload, dict) else None
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise InvalidResponse("GitHub ref response has an invalid sha")
        return sha

    def create_pull(
        self, repo: str, *, head: str, base: str, title: str, body: str
    ) -> dict[str, Any]:
        response = self._call(
            "POST",
            f"/repos/{repo}/pulls",
            repo=repo,
            body={"head": head, "base": base, "title": title, "body": body},
        )
        payload = self._http.json(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("number"), int):
            raise InvalidResponse("GitHub create-pull response has no PR number")
        return payload

    def get_pull(self, repo: str, pr_number: int) -> dict[str, Any]:
        response = self._call("GET", f"/repos/{repo}/pulls/{pr_number}", repo=repo)
        payload = self._http.json(response)
        if not isinstance(payload, dict):
            raise InvalidResponse("GitHub pull response is not an object")
        return payload

    def find_open_pull_by_head(self, repo: str, branch: str) -> dict[str, Any] | None:
        """The open PR whose head is `branch`, or `None` — for branch-exists recovery."""
        owner = repo.split("/", 1)[0]
        response = self._call(
            "GET",
            f"/repos/{repo}/pulls?state=open&head={quote(owner)}:{quote(branch, safe='')}",
            repo=repo,
        )
        payload = self._http.json(response)
        if not isinstance(payload, list):
            raise InvalidResponse("GitHub pulls listing is not an array")
        return payload[0] if payload else None

    def commit_message(self, repo: str, sha: str) -> str:
        response = self._call("GET", f"/repos/{repo}/commits/{sha}", repo=repo)
        payload = self._http.json(response)
        if not isinstance(payload, dict):
            raise InvalidResponse("GitHub commit response is not an object")
        message = (payload.get("commit") or {}).get("message")
        if not isinstance(message, str):
            raise InvalidResponse("GitHub commit response has no message")
        return message

    def pull_diff(self, repo: str, pr_number: int) -> str:
        response = self._call(
            "GET",
            f"/repos/{repo}/pulls/{pr_number}",
            repo=repo,
            accept="application/vnd.github.diff",
            max_bytes=_MAX_DIFF_FETCH_BYTES,
        )
        text = response.body.decode("utf-8", "replace")
        # A truncated diff would be reviewed as if it were complete, so an
        # oversized one is refused outright (the caller posts an error review).
        if len(text) > _MAX_DIFF_CHARS:
            raise DiffTooLarge(f"diff has {len(text)} chars (limit {_MAX_DIFF_CHARS})")
        return text

    def set_status(
        self,
        repo: str,
        sha: str,
        state: str,
        context: str,
        description: str,
        *,
        target_url: str | None = None,
    ) -> None:
        if state not in _STATUS_STATES:
            raise ValueError(f"unknown status state: {state!r}")
        if not _SHA_RE.fullmatch(sha):
            raise ValueError(f"invalid commit sha: {sha!r}")
        self._call(
            "POST",
            f"/repos/{repo}/statuses/{sha}",
            repo=repo,
            body={
                "state": state,
                "context": context,
                "description": description,
                "target_url": target_url,
            },
        )

    def dispatch(self, repo: str, event_type: str, client_payload: Mapping[str, Any]) -> None:
        """POST a `repository_dispatch` event (used for `agent_review_completed`)."""
        self._call(
            "POST",
            f"/repos/{repo}/dispatches",
            repo=repo,
            body={"event_type": event_type, "client_payload": dict(client_payload)},
        )

    def list_workflow_runs(
        self, repo: str, workflow_file: str, *, event: str, head_sha: str
    ) -> list[dict[str, Any]]:
        response = self._call(
            "GET",
            f"/repos/{repo}/actions/workflows/{quote(workflow_file)}/runs"
            f"?event={quote(event)}&head_sha={quote(head_sha)}&per_page=20",
            repo=repo,
        )
        payload = self._http.json(response)
        runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
        if not isinstance(runs, list):
            raise InvalidResponse("GitHub workflow-runs response has no run list")
        return runs

    def run_jobs(self, repo: str, run_id: int) -> list[dict[str, Any]]:
        response = self._call(
            "GET", f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", repo=repo
        )
        payload = self._http.json(response)
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise InvalidResponse("GitHub run-jobs response has no job list")
        return jobs

    def latest_run_jobs(
        self,
        repo: str,
        run_id: int,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        retries: int | None = None,
    ) -> list[dict[str, Any]]:
        """Jobs of the latest attempt of a run (re-runs supersede earlier attempts)."""
        response = self._call(
            "GET",
            f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
            repo=repo,
            timeout=timeout,
            deadline=deadline,
            retries=retries,
        )
        payload = self._http.json(response)
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise InvalidResponse("GitHub run-jobs response has no job list")
        return jobs

    def job_log_tail(
        self,
        repo: str,
        job_id: int,
        *,
        tail_bytes: int,
        timeout: float | None = None,
        deadline: float | None = None,
        retries: int | None = None,
    ) -> str:
        """The last `tail_bytes` of a job's plain-text log (a cut-off first
        line is dropped).

        GitHub answers with a 302 to a short-lived signed blob URL. urllib
        follows it, and `JsonHttp.build_request` attaches the token as an
        *unredirected* header, so the Authorization header is never sent to
        the blob host; only https redirects are followed.
        """
        response = self._call(
            "GET",
            f"/repos/{repo}/actions/jobs/{job_id}/logs",
            repo=repo,
            accept="*/*",
            max_bytes=tail_bytes,
            timeout=timeout,
            keep_tail=True,
            deadline=deadline,
            retries=retries,
            redirect_schemes=self._log_redirect_schemes,
        )
        text = response.body.decode("utf-8", "replace")
        if response.truncated:
            _, newline, rest = text.partition("\n")
            text = rest if newline else ""
        return text
