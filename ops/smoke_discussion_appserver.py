"""Read a synthetic snapshot in both a fresh and resumed Codex discussion."""

from __future__ import annotations

import secrets
import subprocess
import tempfile
from pathlib import Path

from discussion_appserver import run_turn


def main() -> None:
    marker = f"SNAPSHOT_CANARY_{secrets.token_hex(8)}"
    with tempfile.TemporaryDirectory(prefix="taskmgr-snapshot-smoke-") as temporary:
        snapshot = Path(temporary) / "snapshot"
        snapshot.mkdir()
        source = snapshot / "SMOKE.txt"
        source.write_text(f"{marker}\n", encoding="utf-8")
        git_env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "Task Manager Snapshot Smoke",
            "GIT_AUTHOR_EMAIL": "snapshot-smoke@localhost",
            "GIT_COMMITTER_NAME": "Task Manager Snapshot Smoke",
            "GIT_COMMITTER_EMAIL": "snapshot-smoke@localhost",
        }
        for command in (
            ["git", "init", "--quiet", str(snapshot)],
            ["git", "-C", str(snapshot), "add", "--all"],
            ["git", "-C", str(snapshot), "commit", "--quiet", "-m", "snapshot smoke"],
        ):
            subprocess.run(command, env=git_env, check=True, capture_output=True)
        source.chmod(0o440)
        snapshot.chmod(0o550)
        prompt = (
            "Use the shell to read the file SMOKE.txt from the read-only repository "
            "snapshot. Reply with the exact marker found in that file and nothing else."
        )
        thread_id, new_answer = run_turn(
            snapshot=snapshot, thread_id=None, prompt=prompt, images=[]
        )
        if marker not in new_answer:
            raise SystemExit(
                f"fresh discussion did not return the snapshot marker: {new_answer!r}"
            )
        resumed_thread, resumed_answer = run_turn(
            snapshot=snapshot, thread_id=thread_id, prompt=prompt, images=[]
        )
        if resumed_thread != thread_id or marker not in resumed_answer:
            raise SystemExit(
                f"resumed discussion did not return the snapshot marker: {resumed_answer!r}"
            )
        print("fresh and resumed read-only snapshot turns passed")


if __name__ == "__main__":
    main()
