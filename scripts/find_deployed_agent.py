"""Print the agent run ID associated with a deployed squash commit, if any."""

from __future__ import annotations

import json
import re
import sys


def find_run(prs: list[dict], sha: str, repo: str, message: str = "") -> str | None:
    for pr in prs:
        head = pr.get("head") or {}
        branch = head.get("ref") or ""
        match = re.fullmatch(r"codex/(?:fast/)?task-[1-9][0-9]*-([0-9a-f-]{36})", branch)
        if (
            match
            and pr.get("merged_at")
            and pr.get("merge_commit_sha") == sha
            and pr.get("base", {}).get("ref") == "main"
            and (head.get("repo") or {}).get("full_name", "").lower() == repo.lower()
        ):
            return match.group(1)
    for line in message.splitlines():
        match = re.fullmatch(r"Agent-Run-ID: ([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", line)
        if match:
            return match.group(1)
    return None


if __name__ == "__main__":
    message = open(sys.argv[3], encoding="utf-8").read() if len(sys.argv) > 3 else ""
    result = find_run(json.load(sys.stdin), sys.argv[1], sys.argv[2], message)
    if result:
        print(result)
