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

API = "https://tasks.standart-eko.uz/api/v1/agent-runs"
GITHUB = "https://api.github.com"
MAX_RESPONSE = 1024 * 1024
TIMEOUT = 15


@dataclass(frozen=True)
class Target:
    branch: str
    images: dict[str, str]
    healthy: frozenset[str]


TARGETS = {
    "muradjanov-dev/qurbot": Target(
        "master",
        {
            "qurbot-web": "ghcr.io/muradjanov-dev/qurbot",
            "qurbot-worker": "ghcr.io/muradjanov-dev/qurbot",
        },
        frozenset({"qurbot-web"}),
    ),
    "muradjanov-dev/kans-shop": Target(
        "main",
        {
            "kans-api": "ghcr.io/muradjanov-dev/kans-shop-api",
            "kans-frontend": "ghcr.io/muradjanov-dev/kans-shop-frontend",
        },
        frozenset({"kans-api"}),
    ),
    "muradjanov-dev/ketoshop": Target(
        "master",
        {"ketoshop": "ghcr.io/muradjanov-dev/ketoshop"},
        frozenset({"ketoshop"}),
    ),
}


def _request(
    url: str,
    token: str,
    *,
    token_header: str,
    data: dict[str, Any] | None = None,
) -> urllib.request.Request:
    payload = json.dumps(data, separators=(",", ":")).encode() if data is not None else None
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
    repo = raw.get("repo_full_name")
    target = TARGETS.get(repo)
    if target is None or raw.get("base_branch") != target.branch:
        raise ValueError("run is outside approved public repositories")
    run_id = str(UUID(str(raw.get("run_id"))))
    pr_url = raw.get("pr_url")
    prefix = f"https://github.com/{repo}/pull/"
    if not isinstance(pr_url, str) or not pr_url.startswith(prefix) or not pr_url[len(prefix):].isdigit():
        raise ValueError("invalid PR reference")
    if raw.get("status") not in {"pr_ready", "merged"}:
        raise ValueError("invalid pending status")
    if not isinstance(raw.get("notified"), bool):
        raise ValueError("invalid notification state")
    sha = raw.get("merged_sha")
    if raw["status"] == "merged" and not _valid_sha(sha):
        raise ValueError("merged run lacks its commit")
    return {"run_id": run_id, "repo": repo, "target": target, "pr_number": pr_url[len(prefix):],
            "status": raw["status"], "sha": sha, "notified": raw["notified"]}


class ExternalDeployMonitor:
    def __init__(
        self,
        *,
        callback_token: str,
        github_token: str,
        opener: Callable[..., Any] | None = None,
        command_runner: Callable[..., Any] | None = None,
    ) -> None:
        if not callback_token.strip() or not github_token.strip():
            raise ValueError("monitor credentials are required")
        self.callback_token = callback_token
        self.github_token = github_token
        self.opener = opener or urllib.request.urlopen
        self.command_runner = command_runner or subprocess.run

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

    def _github(self, path: str) -> Any:
        return self._json(
            _request(
                f"{GITHUB}{path}",
                f"Bearer {self.github_token}",
                token_header="Authorization",
            )
        )

    def _successful_deploy_run(self, repo: str, branch: str, sha: str) -> str | None:
        result = self._github(
            f"/repos/{repo}/actions/workflows/deploy.yml/runs?event=push&per_page=20&head_sha={sha}"
        )
        for run in result.get("workflow_runs", []):
            if (
                run.get("head_sha") == sha
                and run.get("head_branch") == branch
                and run.get("event") == "push"
                and run.get("status") == "completed"
                and run.get("conclusion") == "success"
                and isinstance(run.get("id"), int)
            ):
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
            if container in target.healthy and (state.get("Health") or {}).get("Status") != "healthy":
                return False
        return True

    def run_once(self) -> tuple[int, int]:
        pending = self._internal("/external-pending")
        if not isinstance(pending, list):
            raise ValueError("invalid pending run list")
        merged = deployed = 0
        for raw in pending[:50]:
            record = _record(raw)
            if not record["notified"]:
                # Deliver the PR/merge notice before advancing to the next state.
                continue
            repo = record["repo"]
            target = record["target"]
            if record["status"] == "pr_ready":
                pr = self._github(f"/repos/{repo}/pulls/{record['pr_number']}")
                sha = pr.get("merge_commit_sha")
                if pr.get("merged") and _valid_sha(sha):
                    self._internal(f"/{record['run_id']}/merged", {"sha": sha})
                    merged += 1
                continue
            sha = record["sha"]
            run_url = self._successful_deploy_run(repo, target.branch, sha)
            if run_url and self._production_matches(target, sha):
                self._internal(
                    f"/{record['run_id']}/deployed",
                    {"sha": sha, "github_run_url": run_url},
                )
                deployed += 1
        return merged, deployed


def main() -> None:
    monitor = ExternalDeployMonitor(
        callback_token=os.environ["AGENT_CALLBACK_TOKEN"],
        github_token=os.environ["GITHUB_AGENT_TOKEN"],
    )
    try:
        merged, deployed = monitor.run_once()
    except Exception as error:
        print(f"external deploy monitor failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"external agent runs: {merged} merged, {deployed} deployed")


if __name__ == "__main__":
    main()
