"""Shared flock helper for scripts that read-modify-write a shared env file.

Used by ops/sync_agent_svc_credentials.py and ops/update_public_agent_token.py,
which both read and rewrite /srv/stack/env/task-manager.env and must not
interleave a read with the other's write.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked_env_file(path: Path) -> Iterator[None]:
    """Hold an exclusive lock for the duration of a read-modify-write on `path`.

    Locks a fixed sibling `<path>.lock` file (root 0600) rather than `path`
    itself: `path` is replaced via atomic rename (a new inode each time it
    changes), and flock()ing a path that gets renamed out from under you does
    not serialize against the next writer. A lock file that is never replaced
    avoids that hazard.
    """
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
