"""Trusted publisher: validated fast branch -> non-force main push."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from agent_task import _send_status, _task, usage


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def _branch_sha() -> str:
    return _git("rev-parse", "HEAD")


def _github(path: str) -> dict:
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


def _run_state(task: dict[str, object]) -> dict:
    request = urllib.request.Request(
        "https://tasks.standart-eko.uz/api/v1/agent-runs/"
        f"{task['run_id']}/status",
        headers={"X-Agent-Callback-Token": os.environ["AGENT_CALLBACK_TOKEN"]},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _report(task: dict[str, object], status: str, sha: str) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    payload: dict[str, object] = {
        "run_id": task["run_id"],
        "status": status,
        "head_sha": sha,
        "github_run_url": run_url,
    }
    if status == "deploying":
        payload.update(usage())
    result = _send_status(payload)
    if status == "deploying" and result.get("status") == "deployed" and result.get("deployed_sha") == sha:
        # A small release can finish before this workflow receives the final
        # callback. The deployment already won the race for this exact commit.
        return
    if result.get("status") != status:
        raise RuntimeError(f"fast run changed state to {result.get('status')}")


def _wait_for_ci(task: dict[str, object], branch: str, sha: str) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    query = urllib.parse.urlencode({"branch": branch, "event": "push", "per_page": 30})
    deadline = time.monotonic() + 8 * 60
    while time.monotonic() < deadline:
        state = _run_state(task)
        if state.get("status") != "validating" or state.get("head_sha") != sha:
            raise RuntimeError("fast run was cancelled or changed while CI was running")
        runs = _github(f"repos/{repo}/actions/runs?{query}").get("workflow_runs", [])
        matching = next(
            (
                run
                for run in runs
                if run.get("name") == "CI" and run.get("head_sha") == sha
            ),
            None,
        )
        if matching and matching.get("status") == "completed":
            if matching.get("conclusion") != "success":
                raise RuntimeError("short CI failed or was skipped; production was not changed")
            print(f"validated exact fast commit {sha}")
            return
        time.sleep(5)
    raise TimeoutError("short CI did not complete in eight minutes")


def publish() -> None:
    task = _task()
    if task["mode"] != "fast" or os.environ.get("FAST_FALLBACK") != "false":
        raise ValueError("only safe fast runs can publish without a PR")
    branch = os.environ["AGENT_BRANCH"]
    if branch != f"codex/fast/task-{task['task_id']}-{task['run_id']}":
        raise ValueError("fast branch does not match task")
    sha = _branch_sha()
    _report(task, "validating", sha)
    for attempt in range(2):
        _wait_for_ci(task, branch, sha)
        _git("fetch", "origin", str(task["base_branch"]))
        if subprocess.run(
            ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"],
            check=False,
        ).returncode != 0:
            if attempt:
                raise RuntimeError("main advanced again; fast run stopped")
            _git(
                "merge",
                "--no-edit",
                "-m",
                f"Merge main into fast task #{task['task_id']}\n\nAgent-Run-ID: {task['run_id']}",
                "origin/main",
            )
            _git("push", "origin", f"HEAD:{branch}")
            sha = _branch_sha()
            _report(task, "validating", sha)
            continue
        _report(task, "publishing", sha)
        push = subprocess.run(
            ["git", "push", "origin", "HEAD:main"], capture_output=True, text=True,
            check=False,
        )
        if push.returncode == 0:
            _report(task, "deploying", sha)
            print(f"pushed validated fast commit {sha} to main")
            return
        if attempt:
            raise RuntimeError("main moved while publishing; fast run stopped")
        _git("fetch", "origin", "main")
        _git(
            "merge",
            "--no-edit",
            "-m",
            f"Merge main into fast task #{task['task_id']}\n\nAgent-Run-ID: {task['run_id']}",
            "origin/main",
        )
        _git("push", "origin", f"HEAD:{branch}")
        sha = _branch_sha()
        _report(task, "validating", sha)
    raise RuntimeError("fast publication exhausted its one main refresh")


if __name__ == "__main__":
    try:
        publish()
    except Exception as exc:
        Path(os.environ["RUNNER_TEMP"], "fast-error.txt").write_text(str(exc)[:900])
        print(f"fast publisher stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
