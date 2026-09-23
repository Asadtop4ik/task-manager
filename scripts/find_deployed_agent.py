"""Print the agent run ID associated with a deployed squash commit, if any."""

from __future__ import annotations

import json
import re
import sys


def find_run(prs: list[dict], sha: str, repo: str) -> str | None:
    for pr in prs:
        head = pr.get("head") or {}
        branch = head.get("ref") or ""
        match = re.fullmatch(r"codex/task-[1-9][0-9]*-([0-9a-f-]{36})", branch)
        if (
            match
            and pr.get("merged_at")
            and pr.get("merge_commit_sha") == sha
            and pr.get("base", {}).get("ref") == "main"
            and (head.get("repo") or {}).get("full_name", "").lower() == repo.lower()
        ):
            return match.group(1)
    return None


if __name__ == "__main__":
    result = find_run(json.load(sys.stdin), sys.argv[1], sys.argv[2])
    if result:
        print(result)
