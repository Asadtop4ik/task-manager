"""Check a PR against the narrow automatic merge policy.

The policy runs from the repository's default branch in a workflow_run job. It
does not execute code from the pull request and never grants the PR access to
the merge token.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

REQUIRED_CHECKS = {"gate", "agent-policy"}


def allowed_files(paths: list[str]) -> bool:
    if not paths:
        return False
    return all(
        path == "README.md"
        or (path.startswith("docs/") and path.endswith(".md"))
        or (path.startswith("frontend/src/") and path.endswith(".css"))
        for path in paths
    )


def current_pr(pr: dict, run: dict, repo: str) -> bool:
    head = pr.get("head") or {}
    return bool(
        pr.get("state") == "open"
        and not pr.get("draft")
        and pr.get("mergeable_state") == "clean"
        and (pr.get("base") or {}).get("ref") == "main"
        and head.get("sha") == run.get("head_sha")
        and (head.get("repo") or {}).get("full_name", "").lower() == repo.lower()
    )


def latest_checks_pass(checks: list[dict]) -> bool:
    latest: dict[str, dict] = {}
    for check in checks:
        name = check.get("name")
        if name in REQUIRED_CHECKS and check.get("id", 0) > latest.get(name, {}).get(
            "id", 0
        ):
            latest[name] = check
    return set(latest) == REQUIRED_CHECKS and all(
        check.get("conclusion") == "success" for check in latest.values()
    )


def agent_run_id(pr: dict) -> str | None:
    branch = (pr.get("head") or {}).get("ref") or ""
    if not branch.startswith(("codex/task-", "codex/fast/task-")):
        return None
    match = re.fullmatch(r"codex/(?:fast/)?task-[1-9][0-9]*-([0-9a-f-]{36})", branch)
    return match.group(1) if match else "invalid"


def agent_ready(pr: dict, run: dict) -> bool:
    return bool(
        run.get("status") in {"pr_opened", "pr_ready"}
        and run.get("pr_url") == pr.get("html_url")
        and run.get("head_sha") == (pr.get("head") or {}).get("sha")
        and run.get("ci_status") == "success"
        and run.get("ci_verified_sha") == run.get("head_sha")
        and run.get("review_status") in {"clean", "advisory"}
        and run.get("review_sha") == run.get("head_sha")
    )


def clean_review_status(statuses: list[dict]) -> bool:
    matching = [row for row in statuses if row.get("context") == "codex-review"]
    if not matching:
        return False
    latest = max(matching, key=lambda row: row.get("id", 0))
    # Commit status rows omit `sha`; callers query /commits/{expected_sha}/statuses.
    return latest.get("state") == "success"


def _agent_allows_merge(pr: dict) -> bool:
    run_id = agent_run_id(pr)
    if run_id is None:
        return True  # A human's README/CSS PR can use the same small-change rule.
    if run_id == "invalid":
        return False
    request = urllib.request.Request(
        f"https://tasks.standart-eko.uz/api/v1/agent-runs/{run_id}/status",
        headers={"X-Agent-Callback-Token": os.environ["AGENT_CALLBACK_TOKEN"]},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return agent_ready(pr, json.load(response))


def _github(path: str) -> object:
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _files(repo: str, number: int) -> list[str]:
    paths: list[str] = []
    for page in range(1, 6):
        rows = _github(f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}")
        assert isinstance(rows, list)
        paths.extend(row["filename"] for row in rows)
        if len(rows) < 100:
            return paths
    return []  # Too many files is outside the small-change policy.


def main() -> None:
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as source:
        event = json.load(source)
    repo = os.environ["GITHUB_REPOSITORY"]
    if "workflow_run" in event:
        run = event["workflow_run"]
        if run.get("event") != "pull_request" or run.get("conclusion") != "success":
            return
        candidates = run.get("pull_requests") or []
        if len(candidates) != 1:
            return
        number = candidates[0]["number"]
        expected_sha = run.get("head_sha")
    else:
        dispatched = event.get("client_payload") or {}
        number = dispatched.get("pull_number")
        expected_sha = dispatched.get("head_sha")
    if not isinstance(number, int) or number < 1 or not isinstance(expected_sha, str):
        return
    for _ in range(3):
        pr = _github(f"repos/{repo}/pulls/{number}")
        assert isinstance(pr, dict)
        if pr.get("mergeable_state") != "unknown":
            break
        time.sleep(2)
    if not current_pr(pr, {"head_sha": expected_sha}, repo):
        return
    if not allowed_files(_files(repo, number)):
        return
    if not _agent_allows_merge(pr):
        return
    checks = _github(f"repos/{repo}/commits/{expected_sha}/check-runs?per_page=100")
    assert isinstance(checks, dict)
    if not latest_checks_pass(checks["check_runs"]):
        return
    statuses = _github(f"repos/{repo}/commits/{expected_sha}/statuses")
    assert isinstance(statuses, list)
    if not clean_review_status(statuses):
        return
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"eligible=true\nnumber={number}\nsha={expected_sha}\n")
    print(f"Eligible PR #{number} at {expected_sha}")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, OSError) as exc:
        print(
            f"auto-merge policy failed closed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
