"""Install the public-repo publisher token in the server API env via stdin."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

from env_file_lock import locked_env_file

ENV_PATH = Path("/srv/stack/env/task-manager.env")
KEY = "GITHUB_PUBLIC_AGENT_TOKEN"


def update_env(path: Path, token: str) -> None:
    if not re.fullmatch(r"github_pat_[A-Za-z0-9_]{20,}", token):
        raise ValueError("expected a fine-grained GitHub token")
    # Locked so a concurrent ops/sync_agent_svc_credentials.py run (which also
    # reads and rewrites this same file) can't interleave a read with this write.
    with locked_env_file(path):
        stat = path.stat()
        lines = path.read_text().splitlines()
        kept = [line for line in lines if not line.startswith(f"{KEY}=")]
        kept.append(f"{KEY}={token}")
        temporary: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=".taskmgr-public-token-", dir=path.parent
            )
            temporary = Path(raw_path)
            with os.fdopen(descriptor, "w") as output:
                output.write("\n".join(kept) + "\n")
            os.chmod(temporary, 0o600)
            os.chown(temporary, stat.st_uid, stat.st_gid)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def main() -> None:
    token = sys.stdin.read(512).strip()
    update_env(ENV_PATH, token)
    print("Public agent token stored in the server env; flag remains unchanged.")


if __name__ == "__main__":
    main()
