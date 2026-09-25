"""Verify public-repo PR merges and exact production images before bot notices."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from project_catalog import QA_REPOSITORY, REPOSITORIES, public_projects

API = "https://tasks.standart-eko.uz/api/v1/agent-runs"
GITHUB = "https://api.github.com"
MAX_RESPONSE = 1024 * 1024
TIMEOUT = 15


@dataclass(frozen=True)
class Target:
    branch: str
    images: dict[str, str]
    ci_jobs: frozenset[str]


@dataclass(frozen=True)
class CiTarget:
    branch: str
    jobs: frozenset[str]
    workflow_path: str


TARGETS = {
    item.full_name: Target(item.branch, dict(item.images), frozenset(item.ci_jobs))
    for item in public_projects()
}


def ci_targets(*, qa_enabled: bool | None = None) -> dict[str, CiTarget]:
    if qa_enabled is None:
        qa_enabled = os.environ.get("AGENT_QA_ENABLED", "").lower() == "true"
    repositories = REPOSITORIES + ((QA_REPOSITORY,) if qa_enabled else ())
    return {
        item.full_name: CiTarget(
            item.branch, frozenset(item.pr_ci_jobs), item.pr_ci_workflow
        )
        for item in repositories
    }


CI_TARGETS = ci_targets()


def _request(
    url: str,
    token: str,
    *,
    token_header: str,
    data: dict[str, Any] | None = None,
) -> urllib.request.Request:
    payload = (
        json.dumps(data, separators=(",", ":")).encode() if data is not None else None
    )
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST" if data is not None else "GET",
        headers={"Accept": "application/vnd.github+json"},
    )
    request.add_unredirected_header(token_header, token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    return request


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40}", value))


def _record(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("invalid pending run")
    row_id = raw.get("id")
    if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id < 1:
        raise ValueError("invalid pending run ID")
    repo = raw.get("repo_full_name")
    target = TARGETS.get(repo)
    if target is None or raw.get("base_branch") != target.branch:
        raise ValueError("run is outside approved public repositories")
    run_id = str(UUID(str(raw.get("run_id"))))
    pr_url = raw.get("pr_url")
    prefix = f"https://github.com/{repo}/pull/"
    if (
        not isinstance(pr_url, str)
        or not pr_url.startswith(prefix)
        or not pr_url[len(prefix) :].isdigit()
    ):
        raise ValueError("invalid PR reference")
    if raw.get("status") not in {"pr_ready", "merged"}:
        raise ValueError("invalid pending status")
    if not isinstance(raw.get("notified"), bool):
        raise ValueError("invalid notification state")
    sha = raw.get("merged_sha")
    if raw["status"] == "merged" and not _valid_sha(sha):
        raise ValueError("merged run lacks its commit")
    return {
        "id": row_id,
        "run_id": run_id,
        "repo": repo,
        "target": target,
        "pr_number": pr_url[len(prefix) :],
        "status": raw["status"],
        "sha": sha,
        "notified": raw["notified"],
    }


def _ci_record(
    raw: Any, targets: dict[str, CiTarget] = CI_TARGETS
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("invalid pending CI run")
    row_id = raw.get("id")
    repo = raw.get("repo_full_name")
    target = targets.get(repo)
    if (
        isinstance(row_id, bool)
        or not isinstance(row_id, int)
        or row_id < 1
        or target is None
        or raw.get("base_branch") != target.branch
        or not target.jobs
    ):
        raise ValueError("CI run is outside approved repositories")
    run_id = str(UUID(str(raw.get("run_id"))))
    pr_url = raw.get("pr_url")
    prefix = f"https://github.com/{repo}/pull/"
    if (
        not isinstance(pr_url, str)
        or not pr_url.startswith(prefix)
        or not pr_url[len(prefix) :].isdigit()
    ):
        raise ValueError("invalid CI PR reference")
    if raw.get("status") not in {"pr_opened", "pr_ready"} or not _valid_sha(
        raw.get("head_sha")
    ):
        raise ValueError("invalid CI run status or SHA")
    if raw.get("ci_status") not in {None, "pending", "failure", "success"}:
        raise ValueError("invalid CI conclusion")
    return {
        "id": row_id,
        "run_id": run_id,
        "repo": repo,
        "pr_number": pr_url[len(prefix) :],
        "workflow_path": target.workflow_path,
        "head_sha": raw["head_sha"],
        "ci_status": raw.get("ci_status"),
        "ci_verified_sha": raw.get("ci_verified_sha"),
        "ci_url": raw.get("ci_url"),
        "required_jobs": target.jobs,
    }


class ExternalDeployMonitor:
    def __init__(
        self,
        *,
        callback_token: str,
        github_token: str,
        opener: Callable[..., Any] | None = None,
        command_runner: Callable[..., Any] | None = None,
        qa_enabled: bool | None = None,
        qa_github_token: str | None = None,
    ) -> None:
        if not callback_token.strip() or not github_token.strip():
            raise ValueError("monitor credentials are required")
        self.callback_token = callback_token
        self.github_token = github_token
        self.opener = opener or urllib.request.urlopen
        self.command_runner = command_runner or subprocess.run
        self.ci_targets = ci_targets(qa_enabled=qa_enabled)
        self.qa_github_token = (qa_github_token or "").strip()
        if "Asadtop4ik/agent-qa" in self.ci_targets and not self.qa_github_token:
            raise ValueError("QA monitor requires its repository-scoped GitHub token")

    def _json(self, request: urllib.request.Request) -> Any:
        with self.opener(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError("monitor response is too large")
        return json.loads(raw)

    def _internal(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._json(
            _request(
                f"{API}{path}",
                self.callback_token,
                token_header="X-Agent-Callback-Token",
                data=body,
            )
        )

    def _github(self, path: str, *, repo: str | None = None) -> Any:
        token = self.github_token
        if repo == "Asadtop4ik/agent-qa":
            token = self.qa_github_token
        return self._json(
            _request(
                f"{GITHUB}{path}",
                f"Bearer {token}",
                token_header="Authorization",
            )
        )

    def _latest_pr_ci(
        self, record: dict[str, Any], sha: str, branch: str
    ) -> tuple[str, str | None]:
        repo = record["repo"]
        workflow_file = record["workflow_path"].rsplit("/", 1)[-1]
        response = self._github(
            f"/repos/{repo}/actions/workflows/{workflow_file}/runs?event=pull_request&head_sha={sha}&per_page=20",
            repo=repo,
        )
        runs = sorted(
            response.get("workflow_runs", []),
            key=lambda item: item.get("id", 0),
            reverse=True,
        )
        for run in runs:
            if (
                run.get("head_sha") != sha
                or run.get("head_branch") != branch
                or run.get("event") != "pull_request"
                or run.get("path") != record["workflow_path"]
                or not isinstance(run.get("id"), int)
            ):
                continue
            if run.get("status") != "completed":
                return "pending", None
            url = f"https://github.com/{repo}/actions/runs/{run['id']}"
            if run.get("conclusion") != "success":
                return "failure", url
            jobs = self._github(
                f"/repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100",
                repo=repo,
            )
            successful = {
                job.get("name")
                for job in jobs.get("jobs", [])
                if job.get("conclusion") == "success"
            }
            return (
                "success" if record["required_jobs"].issubset(successful) else "failure"
            ), url
        return "pending", None

    def check_ci_once(self) -> int:
        """Update exact-head CI status; the API alone decides when PR-ready is notifiable."""
        updated = 0
        after_id = 0
        while True:
            pending = self._internal(f"/ci-pending?after_id={after_id}")
            if not isinstance(pending, list):
                raise ValueError("invalid pending CI list")
            for raw in pending:
                record = _ci_record(raw, self.ci_targets)
                if record["id"] <= after_id:
                    raise ValueError("pending CI cursor did not advance")
                after_id = record["id"]
                pr = self._github(
                    f"/repos/{record['repo']}/pulls/{record['pr_number']}",
                    repo=record["repo"],
                )
                if pr.get("state") == "closed" and not pr.get("merged"):
                    continue
                head = pr.get("head") or {}
                sha = head.get("sha")
                branch = head.get("ref")
                if (
                    not _valid_sha(sha)
                    or not isinstance(branch, str)
                    or (head.get("repo") or {}).get("full_name", "").lower()
                    != record["repo"].lower()
                ):
                    continue
                conclusion, url = self._latest_pr_ci(record, sha, branch)
                if (
                    record["head_sha"] == sha
                    and record["ci_status"] == conclusion
                    and record["ci_url"] == url
                    and (conclusion != "success" or record["ci_verified_sha"] == sha)
                ):
                    continue
                self._internal(
                    f"/{record['run_id']}/ci-result",
                    {
                        "sha": sha,
                        "conclusion": conclusion,
                        "github_run_url": url,
                    },
                )
                updated += 1
            if len(pending) < 50:
                break
        return updated

    def _successful_deploy_run(self, repo: str, target: Target, sha: str) -> str | None:
        result = self._github(
            f"/repos/{repo}/actions/workflows/deploy.yml/runs?event=push&per_page=20&head_sha={sha}",
            repo=repo,
        )
        for run in result.get("workflow_runs", []):
            if (
                run.get("head_sha") == sha
                and run.get("head_branch") == target.branch
                and run.get("event") == "push"
                and run.get("status") == "completed"
                and run.get("conclusion") == "success"
                and isinstance(run.get("id"), int)
            ):
                jobs = self._github(
                    f"/repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100",
                    repo=repo,
                )
                succeeded = {
                    job.get("name")
                    for job in jobs.get("jobs", [])
                    if job.get("conclusion") == "success"
                }
                if target.ci_jobs.issubset(succeeded) and "deploy" in succeeded:
                    return f"https://github.com/{repo}/actions/runs/{run['id']}"
        return None

    def _production_matches(self, target: Target, sha: str) -> bool:
        for container, image in target.images.items():
            result = self.command_runner(
                ["docker", "inspect", "--format", "{{json .}}", container],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                return False
            record = json.loads(result.stdout)
            state = record.get("State") or {}
            if record.get("Config", {}).get("Image") != f"{image}:{sha}":
                return False
            if state.get("Running") is not True:
                return False
            if (state.get("Health") or {}).get("Status") != "healthy":
                return False
        return True

    def run_once(self) -> tuple[int, int]:
        merged = deployed = 0
        after_id = 0
        while True:
            pending = self._internal(f"/external-pending?after_id={after_id}")
            if not isinstance(pending, list):
                raise ValueError("invalid pending run list")
            for raw in pending:
                record = _record(raw)
                if record["id"] <= after_id:
                    raise ValueError("pending run cursor did not advance")
                after_id = record["id"]
                if not record["notified"]:
                    # Deliver the PR/merge notice before advancing to the next state.
                    continue
                repo = record["repo"]
                target = record["target"]
                if record["status"] == "pr_ready":
                    pr = self._github(
                        f"/repos/{repo}/pulls/{record['pr_number']}", repo=repo
                    )
                    sha = pr.get("merge_commit_sha")
                    if pr.get("merged") and _valid_sha(sha):
                        self._internal(f"/{record['run_id']}/merged", {"sha": sha})
                        merged += 1
                    continue
                sha = record["sha"]
                run_url = self._successful_deploy_run(repo, target, sha)
                if run_url and self._production_matches(target, sha):
                    self._internal(
                        f"/{record['run_id']}/deployed",
                        {"sha": sha, "github_run_url": run_url},
                    )
                    deployed += 1
            if len(pending) < 50:
                break
        return merged, deployed


def main() -> None:
    monitor = ExternalDeployMonitor(
        callback_token=os.environ["AGENT_CALLBACK_TOKEN"],
        github_token=os.environ["GITHUB_AGENT_TOKEN"],
        qa_github_token=os.environ.get("GITHUB_AGENT_QA_TOKEN"),
    )
    try:
        ci_updated = monitor.check_ci_once()
        merged, deployed = monitor.run_once()
    except Exception as error:
        print(
            f"external deploy monitor failed: {type(error).__name__}", file=sys.stderr
        )
        raise SystemExit(1) from None
    print(
        f"agent CI: {ci_updated} updated; external runs: {merged} merged, {deployed} deployed"
    )


if __name__ == "__main__":
    main()
