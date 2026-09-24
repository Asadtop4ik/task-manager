"""Validate a dispatched task, prepare a Codex prompt, and report its result.

Task text is always data. The workflow reads generated files and environment
values; it never interpolates the task text into shell source.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from difflib import SequenceMatcher
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
        "Implement only the requested behavior. On this 1 CPU runner, do not install "
        "dependencies solely for checks or run full suites/builds; independent GitHub-hosted "
        "CI will do that. Dependency-management tasks may install packages to update lockfiles. "
        "Run git diff --check and quick targeted checks if existing dependencies allow, "
        "then state which checks were deferred to CI.\n"
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


_SENSITIVE_CHANGE = re.compile(
    r"\b(?:auth|authorize|permission|privilege|grant|revoke|role|owner|isOwner|"
    r"manager|executor|member|access|accessToken|apiKey|token|secret|"
    r"credential|password|login|invite|session|jwt|payment|billing|price|money|"
    r"discount|invoice|checkout|order|purchase|broadcast|bulk|mass|recipient|"
    r"send_message|sendMessage|send_photo|sendPhoto|send_media|sendMedia|"
    r"chat_id|message_id|webhook|deploy|migration|agent|codex|github|delete|"
    r"restore|is_owner|can_use_codex|canUseCodex)\b",
    re.IGNORECASE,
)
_PRESENTATION_ATTRIBUTE = re.compile(
    r'\s*(?:title|aria-label|alt|placeholder|className)="[^"]*"'
)
_UNSAFE_CSS = re.compile(
    r"(?:url\s*\(|@import|expression\s*\(|behavior\s*:|binding\s*:|data\s*:|content\s*:)",
    re.IGNORECASE,
)


def fast_needs_pr(paths: list[str], changed_fragments: list[str] | None = None) -> bool:
    """Fail closed to a PR for sensitive paths and changed code, not file count."""
    protected_prefixes = (
        ".github/", ".codex/", ".agents/", "scripts/", "backend/alembic/",
        "backend/app/core/", "backend/app/db/", "backend/app/api/",
        "backend/app/services/", "backend/app/schemas/", "bot/app/handlers/",
        "frontend/src/lib/",
    )
    protected_exact = {
        "AGENTS.md", "backend/app/api/deps.py", "backend/app/api/v1/auth.py",
        "backend/app/api/v1/agent_runs.py", "backend/app/api/v1/team.py",
        "backend/app/api/v1/users.py", "backend/app/api/v1/tasks.py",
        "bot/app/main.py", "bot/app/worker.py", "bot/app/loader.py",
        "bot/app/api.py", "bot/app/texts.py", "bot/app/config.py",
        "bot/app/callbacks.py", "bot/app/cards.py", "bot/app/parsing.py",
        "backend/app/main.py", "frontend/src/App.tsx",
        "frontend/src/lib/auth.tsx", "frontend/src/lib/api.ts",
        "frontend/src/pages/Login.tsx", "frontend/src/pages/Team.tsx",
        "frontend/src/pages/TaskDetail.tsx", "frontend/src/components/TaskRow.tsx",
    }
    fast_prefixes = (
        "backend/app/", "backend/tests/", "bot/app/", "bot/tests/",
        "frontend/src/", "frontend/public/",
    )
    protected_words = (
        "payment", "billing", "price", "money", "secret", "auth", "permission",
        "security", "migration", "deploy", "workflow", "broadcast",
        "checkout", "purchase", "order", "cart", "invoice", "customer",
        "profile", "account", "member", "access", "login", "invite",
        "role", "owner", "admin", "token", "credential", "session",
    )
    for path in paths:
        lowered = path.lower()
        if (
            path in protected_exact or path.startswith(protected_prefixes)
            or any(word in lowered for word in protected_words)
            or path.endswith((".toml", ".lock", ".yml", ".yaml"))
            or not path.startswith(fast_prefixes)
        ):
            return True
    return any(_SENSITIVE_CHANGE.search(fragment) for fragment in changed_fragments or [])


def _changed_fragments(patch: str) -> list[str]:
    """Inspect changed tokens; unchanged context on a modified line is not a change."""
    fragments: list[str] = []
    removed: list[str] = []
    added: list[str] = []

    def flush() -> None:
        if len(removed) == len(added) == 1:
            before, after = removed[0], added[0]
            changes: list[tuple[str, str]] = []
            for operation, left_start, left_end, right_start, right_end in SequenceMatcher(
                None, before, after, autojunk=False
            ).get_opcodes():
                if operation != "equal":
                    changes.append((before[left_start:left_end], after[right_start:right_end]))
            if _SENSITIVE_CHANGE.search(before + after) and not (
                changes
                and all(
                    not old and _PRESENTATION_ATTRIBUTE.fullmatch(new)
                    for old, new in changes
                )
            ):
                # A changed boolean/operator beside an unchanged permission
                # check is still a permission change. Only literal tooltip
                # attributes are inert enough to remain fast.
                fragments.extend((before, after))
            else:
                for old, new in changes:
                    fragments.extend((old, new))
        else:
            fragments.extend(removed + added)
        removed.clear()
        added.clear()

    for line in patch.splitlines():
        if line.startswith(("diff --git ", "@@")):
            flush()
        elif line.startswith(("--- ", "+++ ")):
            continue
        elif line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
        elif line.startswith(("GIT binary patch", "Binary files ")):
            fragments.append("credential")  # Unknown binary content needs review.
        elif line.startswith(("new file mode 120000", "old mode 120000", "new mode 120000")):
            fragments.append("credential")  # Symlinks need manual inspection.
    flush()
    return fragments


def _changed_code(*, cwd: str | None, untracked: list[str]) -> list[str]:
    patch = subprocess.check_output(
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--unified=0", "--binary", "HEAD"],
        cwd=cwd,
    ).decode("utf-8", errors="replace")
    fragments = _changed_fragments(patch)
    root = Path(cwd or os.getcwd())
    for relative in untracked:
        path = root / relative
        if path.is_symlink():
            fragments.append("credential")
            continue
        data = path.read_bytes()
        if b"\0" in data:
            fragments.append("credential")
        else:
            fragments.extend(data.decode("utf-8", errors="replace").splitlines())
    return fragments


def _safe_fast_patch(*, cwd: str | None, untracked: list[str]) -> bool:
    """Only unambiguous presentation edits can bypass the owner PR in the pilot.

    A denylist cannot prove that a numeric or boolean edit is unrelated to
    money or permissions. Unknown edits therefore fall back to review, with no
    arbitrary cap on the number of CSS files or static attributes changed.
    """
    if untracked:
        return False
    patch = subprocess.check_output(
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--unified=0", "HEAD"],
        cwd=cwd,
        text=True,
        errors="replace",
    )
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    if len(sections) < 2:
        return False
    for section in sections[1:]:
        lines = section.splitlines()
        if any(
            line.startswith(("new file mode ", "deleted file mode ", "GIT binary patch"))
            for line in lines
        ):
            return False
        new_path = next((line[6:] for line in lines if line.startswith("+++ b/")), None)
        if new_path is None:
            return False
        if new_path.endswith(".css"):
            if _UNSAFE_CSS.search(section):
                return False
            continue
        if not new_path.endswith(".tsx"):
            return False
        before: list[str] = []
        after: list[str] = []
        saw_hunk = False

        def safe_hunk() -> bool:
            if len(before) != 1 or len(after) != 1:
                return False
            changes = [
                (before[0][left_start:left_end], after[0][right_start:right_end])
                for operation, left_start, left_end, right_start, right_end in SequenceMatcher(
                    None, before[0], after[0], autojunk=False
                ).get_opcodes()
                if operation != "equal"
            ]
            return bool(changes) and all(
                (not old and _PRESENTATION_ATTRIBUTE.fullmatch(new))
                or (_PRESENTATION_ATTRIBUTE.fullmatch(old) and _PRESENTATION_ATTRIBUTE.fullmatch(new))
                for old, new in changes
            )

        for line in lines:
            if line.startswith("@@"):
                if saw_hunk and not safe_hunk():
                    return False
                before.clear()
                after.clear()
                saw_hunk = True
            elif saw_hunk and line.startswith("-") and not line.startswith("--- "):
                before.append(line[1:])
            elif saw_hunk and line.startswith("+") and not line.startswith("+++ "):
                after.append(line[1:])
        if not saw_hunk or not safe_hunk():
            return False
    return True


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
    # Reference screenshots live outside the checkout and must not be copied
    # into a commit or PR patch. Compare bytes rather than filenames because
    # an agent could rename the source image before adding it.
    image_dir = Path(os.environ["RUNNER_TEMP"]) / "agent-images"
    if image_dir.is_dir():
        def digest(path: Path) -> bytes:
            hasher = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(chunk)
            return hasher.digest()

        source_images = {digest(path) for path in image_dir.iterdir() if path.is_file()}
        root = Path(cwd or os.getcwd())
        for relative in paths:
            candidate = root / relative
            if candidate.is_file() and not candidate.is_symlink() and digest(candidate) in source_images:
                raise ValueError("task reference images cannot be committed")
    task = _task()
    fallback = task["mode"] == "fast" and fast_needs_pr(paths)
    if task["mode"] == "fast" and not fallback:
        fallback = fast_needs_pr(
            paths, _changed_code(cwd=cwd, untracked=[path for path in untracked if path])
        )
    if task["mode"] == "fast" and not fallback:
        fallback = not _safe_fast_patch(cwd=cwd, untracked=[path for path in untracked if path])
    _write_env(
        "FAST_FALLBACK",
        "true" if fallback else "false",
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
