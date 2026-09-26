"""Loads the trusted publisher scripts by file path from `settings.trusted_dir`.

These modules (`agent_task.py`, `public_agent_task.py`, `agent_preflight.py`,
`agent_release.py`, `agent_images.py`, `agent_repos.py`) are the single source
of truth for task validation, patch policy, formatting/lint preflight, and
release/correction bookkeeping — the same code the trusted GitHub Actions
publisher runs. Handlers must call them, not reimplement their behavior.

They are loaded with `importlib.util.spec_from_file_location` against the
resolved files under `trusted_dir`, never via a package/`sys.path` import that
some other entry on `sys.path` (a worktree, a repo checkout, a task's own
untrusted content) could shadow. `trusted_dir` itself is always the flat
deployment layout `ops/install_agent_svc.sh` produces (every trusted file as a
sibling, no `scripts/`/`backend/app/services/` nesting), so it is placed at
the *front* of `sys.path` before executing each module: `public_agent_task.py`
does a bare `from agent_repos import ...` / `from agent_task import ...`
internally, and putting `trusted_dir` first guarantees those bare imports
resolve to the trusted siblings even if something else on `sys.path` (e.g. a
test's simulated malicious `scripts/` directory) already defines modules by
those names.

Each resolved file is loaded and cached at most once per process: a second
call for the same path returns the exact same module object.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType

_lock = threading.Lock()
_module_cache: dict[str, ModuleType] = {}

_FILES: dict[str, str] = {
    "agent_task": "agent_task.py",
    "public_agent_task": "public_agent_task.py",
    "agent_preflight": "agent_preflight.py",
    "agent_release": "agent_release.py",
    "agent_images": "agent_images.py",
    "agent_repos": "agent_repos.py",
}


def _ensure_trusted_dir_first_on_path(trusted_dir: Path) -> None:
    entry = str(trusted_dir)
    with _lock:
        if entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)


def _load(trusted_dir: Path, name: str) -> ModuleType:
    path = trusted_dir / _FILES[name]
    resolved = path.resolve(strict=True)
    key = str(resolved)
    with _lock:
        cached = _module_cache.get(key)
        if cached is not None:
            return cached
    _ensure_trusted_dir_first_on_path(trusted_dir.resolve())
    if name == "public_agent_task":
        # `public_agent_task.py` does a bare `from agent_repos import ...` /
        # `from agent_task import ...` at module-exec time. Python's import
        # statement checks `sys.modules` before `sys.path`, so a *different*
        # `trusted_dir` loaded earlier in this process (only possible in
        # tests -- production has exactly one) could otherwise leave a stale
        # "agent_repos"/"agent_task" cached under those bare names. Dropping
        # them here forces a fresh bare-name import that resolves against
        # *this* `trusted_dir`, now first on `sys.path`.
        with _lock:
            sys.modules.pop("agent_repos", None)
            sys.modules.pop("agent_task", None)
    spec = importlib.util.spec_from_file_location(f"agent_svc._trusted.{name}", resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load trusted module {name!r}: {resolved}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with _lock:
        _module_cache[key] = module
    return module


class TrustedModules:
    """Typed, cached accessors for the trusted scripts under one `trusted_dir`."""

    def __init__(self, trusted_dir: str | Path) -> None:
        self._dir = Path(trusted_dir)

    @property
    def agent_task(self) -> ModuleType:
        return _load(self._dir, "agent_task")

    @property
    def public_agent_task(self) -> ModuleType:
        return _load(self._dir, "public_agent_task")

    @property
    def agent_preflight(self) -> ModuleType:
        return _load(self._dir, "agent_preflight")

    @property
    def agent_release(self) -> ModuleType:
        return _load(self._dir, "agent_release")

    @property
    def agent_images(self) -> ModuleType:
        return _load(self._dir, "agent_images")

    @property
    def agent_repos(self) -> ModuleType:
        return _load(self._dir, "agent_repos")
