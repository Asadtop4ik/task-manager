"""Validate a dispatched task, prepare a Codex prompt, and report its result.

Task text is always data. The workflow reads generated files and environment
values; it never interpolates the task text into shell source.
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID


def _task() -> dict[str, object]:
    raw = json.loads(os.environ["TASK_JSON"])
    if not isinstance(raw, dict):
        raise TypeError("task payload must be an object")
    task_id = raw.get("task_id")
    if not isinstance(task_id, int) or task_id < 1:
        raise ValueError("invalid task ID")
    run_id = str(UUID(str(raw.get("run_id"))))
    title = raw.get("title")
    description = raw.get("description")
    base = raw.get("base_branch")
    if not isinstance(title, str) or not title.strip() or len(title) > 255:
        raise ValueError("invalid title")
    if not isinstance(description, str) or len(description) > 12000:
        raise ValueError("invalid description")
    if not isinstance(base, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", base):
        raise ValueError("invalid base branch")
    if base.startswith("-") or ".." in base or base.endswith("/"):
        raise ValueError("invalid base branch")
    return {
        "task_id": task_id,
        "run_id": run_id,
        "title": title.strip(),
        "description": description.strip(),
        "base_branch": base,
    }


def _write_env(name: str, value: str) -> None:
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def prepare() -> None:
    task = _task()
    branch = f"codex/task-{task['task_id']}-{task['run_id']}"
    temp = Path(os.environ["RUNNER_TEMP"])
    prompt = (
        "Work on the Task Manager repository. Follow AGENTS.md.\n"
        "Implement only the requested behavior and run relevant checks.\n"
        "Do not edit AGENTS.md, .github/, authentication, migrations, or deploy files.\n"
        "If the task needs a business decision, explain exactly what is missing.\n"
        "Do not push, open a PR, deploy, or read credentials. A later workflow step handles GitHub.\n"
        f"Task #{task['task_id']}: {task['title']}\n"
        f"Description:\n{task['description']}\n"
    )
    (temp / "agent-prompt.txt").write_text(prompt, encoding="utf-8")
    (temp / "agent-pr-body.md").write_text(
        f"Task Manager task #{task['task_id']}\n\n"
        f"Requested: {task['title']}\n\n{task['description']}\n\n"
        "Created by Codex. Review the diff and CI results before merging.\n",
        encoding="utf-8",
    )
    _write_env("AGENT_BRANCH", branch)
    _write_env("AGENT_BASE", str(task["base_branch"]))
    _write_env("AGENT_TASK_ID", str(task["task_id"]))
    _write_env("AGENT_RUN_ID", str(task["run_id"]))


def check_diff() -> None:
    tracked = (
        subprocess.check_output(["git", "diff", "--name-only", "-z"])
        .decode()
        .split("\0")
    )
    staged = (
        subprocess.check_output(["git", "diff", "--cached", "--name-only", "-z"])
        .decode()
        .split("\0")
    )
    untracked = (
        subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"]
        )
        .decode()
        .split("\0")
    )
    paths = [path for path in tracked + staged + untracked if path]
    if not paths:
        raise ValueError("agent produced no file changes")
    blocked = [
        path
        for path in paths
        if path.startswith(
            (".github/", "backend/alembic/", ".codex/", ".agents/", "scripts/")
        )
        or path
        in {
            "AGENTS.md",
            "backend/app/core/security.py",
            "backend/app/core/config.py",
            "backend/app/api/v1/agent_runs.py",
            "backend/app/db/models/agent_run.py",
        }
        or path.endswith((".env", "auth.json"))
    ]
    if blocked:
        raise ValueError(
            f"owner review required for protected paths: {', '.join(blocked)}"
        )


def callback() -> None:
    task = _task()
    repo = os.environ["GITHUB_REPOSITORY"]
    run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    success = os.environ["JOB_STATUS"] == "success"
    payload: dict[str, object] = {
        "run_id": task["run_id"],
        "status": "pr_ready" if success else "failed",
        "github_run_url": run_url,
    }
    if success:
        payload["pr_url"] = os.environ["PR_URL"]
        payload["head_sha"] = os.environ["HEAD_SHA"]
    else:
        payload["error"] = "Agent workflow failed; inspect the GitHub run."
    url = f"https://tasks.standart-eko.uz/api/v1/agent-runs/{task['run_id']}/callback"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Agent-Callback-Token": os.environ["AGENT_CALLBACK_TOKEN"],
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"agent callback returned HTTP {response.status}")


if __name__ == "__main__":
    try:
        {"prepare": prepare, "check-diff": check_diff, "callback": callback}[
            sys.argv[1]
        ]()
    except (
        KeyError,
        TypeError,
        ValueError,
        urllib.error.URLError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"agent task failed: {exc}", file=sys.stderr)
        sys.exit(1)
