"""Validate a dispatched task, prepare a Codex prompt, and report its result.

Task text is always data. The workflow reads generated files and environment
values; it never interpolates the task text into shell source.
"""

from __future__ import annotations

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
    mode = raw.get("mode", "pr")
    if not isinstance(title, str) or not title.strip() or len(title) > 255:
        raise ValueError("invalid title")
    if not isinstance(description, str) or len(description) > 12000:
        raise ValueError("invalid description")
    if not isinstance(base, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", base):
        raise ValueError("invalid base branch")
    if base.startswith("-") or ".." in base or base.endswith("/"):
        raise ValueError("invalid base branch")
    if mode not in {"pr", "fast"}:
        raise ValueError("invalid agent mode")
    return {
        "task_id": task_id,
        "run_id": run_id,
        "title": title.strip(),
        "description": description.strip(),
        "base_branch": base,
        "mode": mode,
    }


def _write_env(name: str, value: str) -> None:
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def prepare() -> None:
    task = _task()
    prefix = "codex/fast" if task["mode"] == "fast" else "codex"
    branch = f"{prefix}/task-{task['task_id']}-{task['run_id']}"
    temp = Path(os.environ["RUNNER_TEMP"])
    prompt = (
        "Work on the Task Manager repository. Follow AGENTS.md.\n"
        "Implement only the requested behavior and run relevant checks.\n"
        "Sensitive paths require human review and will become a PR instead of direct deployment.\n"
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
    _write_env("AGENT_MODE", str(task["mode"]))


def fast_needs_pr(paths: list[str]) -> bool:
    """Fail closed to a PR for permissions, infra, data and unknown paths."""
    protected_prefixes = (
        ".github/", ".codex/", ".agents/", "scripts/", "backend/alembic/",
        "backend/app/core/", "backend/app/db/", "backend/app/api/",
        "backend/app/services/", "backend/app/schemas/", "bot/app/handlers/",
    )
    protected_exact = {
        "AGENTS.md", "backend/app/api/deps.py", "backend/app/api/v1/auth.py",
        "backend/app/api/v1/agent_runs.py", "backend/app/api/v1/team.py",
        "backend/app/api/v1/users.py", "backend/app/api/v1/tasks.py",
        "bot/app/main.py", "bot/app/worker.py", "bot/app/loader.py",
        "bot/app/api.py", "bot/app/texts.py", "bot/app/config.py",
        "bot/app/callbacks.py", "backend/app/main.py",
        "frontend/src/lib/auth.tsx", "frontend/src/lib/api.ts",
        "frontend/src/pages/Login.tsx", "frontend/src/pages/Team.tsx",
    }
    fast_prefixes = (
        "backend/app/", "backend/tests/", "bot/app/", "bot/tests/",
        "frontend/src/", "frontend/public/", "docs/",
    )
    protected_words = (
        "payment", "billing", "price", "money", "secret", "auth", "permission",
        "security", "migration", "deploy", "workflow", "broadcast",
    )
    for path in paths:
        lowered = path.lower()
        if (
            path in protected_exact or path.startswith(protected_prefixes)
            or any(word in lowered for word in protected_words)
            or path.endswith((".toml", ".lock", ".yml", ".yaml"))
            or not (path == "README.md" or path.startswith(fast_prefixes))
        ):
            return True
    return False


def check_diff(*, cwd: str | None = None) -> None:
    tracked = (
        subprocess.check_output(["git", "diff", "--no-renames", "--name-only", "-z"], cwd=cwd)
        .decode()
        .split("\0")
    )
    staged = (
        subprocess.check_output(["git", "diff", "--cached", "--no-renames", "--name-only", "-z"], cwd=cwd)
        .decode()
        .split("\0")
    )
    untracked = (
        subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=cwd
        )
        .decode()
        .split("\0")
    )
    paths = [path for path in tracked + staged + untracked if path]
    if not paths:
        raise ValueError("agent produced no file changes")
    credential_paths = [
        path
        for path in paths
        if any(part.startswith(".env") for part in Path(path).parts)
        or Path(path).name in {"auth.json", "credentials.json", "id_rsa", "id_ed25519"}
        or path.endswith((".pem", ".key"))
        or path.startswith((".ssh/", ".codex/auth/"))
    ]
    if credential_paths:
        raise ValueError(
            f"credential files cannot be committed: {', '.join(credential_paths)}"
        )
    task = _task()
    _write_env(
        "FAST_FALLBACK",
        "true" if task["mode"] == "fast" and fast_needs_pr(paths) else "false",
    )
    result = Path(os.environ["RUNNER_TEMP"]) / "agent-result.txt"
    if result.exists():
        summary = result.read_text(encoding="utf-8").strip()[:3000]
        if summary:
            with (Path(os.environ["RUNNER_TEMP"]) / "agent-pr-body.md").open(
                "a", encoding="utf-8"
            ) as body:
                body.write(f"\nCodex summary:\n\n{summary}\n")


def usage() -> dict[str, int]:
    events = Path(os.environ["RUNNER_TEMP"]) / "agent-events.jsonl"
    if not events.exists():
        return {}
    last: dict[str, int] = {}
    for line in events.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "turn.completed":
            continue
        values = event.get("usage") or {}
        last = {
            key: values[key]
            for key in ("input_tokens", "cached_input_tokens", "output_tokens")
            if isinstance(values.get(key), int) and values[key] >= 0
        }
    return last


def _send_status(payload: dict[str, object]) -> dict[str, object]:
    task = _task()
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
        return json.load(response)


def started() -> None:
    task = _task()
    repo = os.environ["GITHUB_REPOSITORY"]
    run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    result = _send_status(
        {"run_id": task["run_id"], "status": "running", "github_run_url": run_url}
    )
    if result.get("status") == "cancelled":
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write("cancelled=true\n")


def callback() -> None:
    task = _task()
    repo = os.environ["GITHUB_REPOSITORY"]
    run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    success = os.environ["JOB_STATUS"] == "success"
    if success and task["mode"] == "fast" and os.environ.get("FAST_FALLBACK") == "false":
        # The fast publisher already reported the exact branch SHA. Deployment
        # reports the final result after the image and readiness checks pass.
        return
    payload: dict[str, object] = {
        "run_id": task["run_id"],
        "status": "pr_ready" if success else "failed",
        "github_run_url": run_url,
    }
    if success:
        payload["pr_url"] = os.environ["PR_URL"]
        payload["head_sha"] = os.environ["HEAD_SHA"]
    else:
        result_file = Path(os.environ["RUNNER_TEMP"]) / "agent-result.txt"
        fast_error = Path(os.environ["RUNNER_TEMP"]) / "fast-error.txt"
        reason = fast_error.read_text(encoding="utf-8").strip() if fast_error.exists() else ""
        if not reason and os.environ.get("FAILURE_PHASE") == "implement" and result_file.exists():
            reason = result_file.read_text(encoding="utf-8").strip()
        payload["error"] = reason[:900] or (
            "Publisher failed before PR/deploy; inspect the GitHub run."
            if os.environ.get("FAILURE_PHASE") == "publish"
            else "Agent workflow failed; inspect the GitHub run."
        )
    payload.update(usage())
    _send_status(payload)


if __name__ == "__main__":
    try:
        {
            "prepare": prepare,
            "started": started,
            "check-diff": check_diff,
            "callback": callback,
        }[sys.argv[1]]()
    except (
        KeyError,
        TypeError,
        ValueError,
        urllib.error.URLError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"agent task failed: {exc}", file=sys.stderr)
        sys.exit(1)
