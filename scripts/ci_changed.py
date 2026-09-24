"""Choose CI jobs from the exact commit diff, failing closed to the full suite."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SERVICES = ("backend", "bot", "frontend")


def selected_jobs(paths: list[str] | None) -> dict[str, bool]:
    if not paths:
        return dict.fromkeys(SERVICES, True)
    selected = dict.fromkeys(SERVICES, False)
    for path in paths:
        service, separator, _ = path.partition("/")
        if separator and service in selected:
            selected[service] = True
        elif path == "README.md" or path.startswith("docs/"):
            continue
        else:
            return dict.fromkeys(SERVICES, True)
    return selected


def changed_paths(
    event: dict, event_name: str, head: str, *, cwd: str | None = None
) -> list[str] | None:
    branch = (event.get("ref") or "").removeprefix("refs/heads/")
    if event_name == "push" and branch.startswith("codex/fast/"):
        # Every rebase/merge of main must revalidate the whole agent patch,
        # not merely what changed since the previous fast-branch push.
        try:
            base = subprocess.check_output(
                ["git", "merge-base", "origin/main", head], text=True, cwd=cwd
            ).strip()
        except subprocess.CalledProcessError:
            return None
    elif event_name == "pull_request":
        base = (event.get("pull_request") or {}).get("base", {}).get("sha")
    elif event_name == "push":
        base = event.get("before")
        if base == "0" * 40:
            try:
                base = subprocess.check_output(
                    ["git", "merge-base", "origin/main", head], text=True, cwd=cwd
                ).strip()
            except subprocess.CalledProcessError:
                return None
    else:
        return None  # Scheduled/manual runs exercise the full suite.
    if not isinstance(base, str) or len(base) != 40:
        return None
    try:
        # Rename detection reports only the destination. Treat a move as a
        # deletion plus an addition so checks for both services run.
        raw = subprocess.check_output(
            ["git", "diff", "--no-renames", "--name-only", "-z", base, head],
            cwd=cwd,
        )
    except subprocess.CalledProcessError:
        return None
    return [path.decode() for path in raw.split(b"\0") if path]


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    paths = changed_paths(event, os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_SHA"])
    selected = selected_jobs(paths)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for service, enabled in selected.items():
            output.write(f"{service}={'true' if enabled else 'false'}\n")
    print(f"changed paths: {paths if paths is not None else 'unknown; full CI'}")
    print(f"selected jobs: {selected}")


if __name__ == "__main__":
    main()
