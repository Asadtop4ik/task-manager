"""Container image/health snapshot. Runs as root via sudo.

argv is a list of docker container names, nothing else. Each name must be one
of the names the trusted catalog (`agent_repos.py`) lists; anything else is
refused before any docker command runs. Prints one JSON object on stdout:
``{name: {"image": <Config.Image or null>, "running": <bool>, "health": <str
or null>}}``.

Contract with the trusted catalog module: this script expects `agent_repos`
(loaded from `trusted_dir`, a repo/branch/mirror catalog owned by a different
work package) to expose the allowed container names as one of a
`CONTAINER_NAMES` or `CONTAINERS` module attribute, or a zero-argument
`container_names()` callable. If none of those exist, every name is refused
and the exit code reports the load failure. `trusted_dir` is a plain path
constant so tests can point it at a fixture module instead of the real
catalog.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

TRUSTED_DIR = Path("/opt/agent-svc/trusted")
DOCKER_BINARY = "/usr/bin/docker"
INSPECT_TIMEOUT_S = 15.0

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _load_agent_repos(trusted_dir: Path) -> ModuleType:
    module_path = trusted_dir / "agent_repos.py"
    spec = importlib.util.spec_from_file_location("agent_repos", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _catalog_container_names(module: ModuleType) -> frozenset[str]:
    for attr in ("CONTAINER_NAMES", "CONTAINERS"):
        value = getattr(module, attr, None)
        if value is not None:
            return frozenset(str(item) for item in value)
    getter = getattr(module, "container_names", None)
    if callable(getter):
        return frozenset(str(item) for item in getter())
    raise RuntimeError("agent_repos module does not expose container names")


def _docker_inspect(name: str, *, runner: Runner) -> dict[str, Any] | None:
    try:
        completed = runner(
            [DOCKER_BINARY, "inspect", "--format", "{{json .}}", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=INSPECT_TIMEOUT_S,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if completed.returncode != 0:
        return None
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _summarize(raw: dict[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {"image": None, "running": False, "health": None}
    config = raw.get("Config")
    state = raw.get("State")
    image = config.get("Image") if isinstance(config, dict) else None
    running = bool(state.get("Running")) if isinstance(state, dict) else False
    health = None
    if isinstance(state, dict):
        health_block = state.get("Health")
        if isinstance(health_block, dict):
            status = health_block.get("Status")
            health = status if isinstance(status, str) else None
    return {
        "image": image if isinstance(image, str) else None,
        "running": running,
        "health": health,
    }


def main(
    argv: list[str],
    *,
    trusted_dir: Path = TRUSTED_DIR,
    runner: Runner = subprocess.run,
) -> int:
    names = argv[1:]
    if not names:
        print(json.dumps({"reason": "no container names given"}), flush=True)
        return 2
    try:
        module = _load_agent_repos(trusted_dir)
        allowed = _catalog_container_names(module)
    except (OSError, RuntimeError, SyntaxError) as exc:
        print(json.dumps({"reason": f"cannot load trusted catalog: {exc}"}), flush=True)
        return 3
    for name in names:
        if not isinstance(name, str) or name not in allowed:
            print(json.dumps({"reason": f"unknown container: {name}"}), flush=True)
            return 3
    result: dict[str, Any] = {}
    for name in names:
        result[name] = _summarize(_docker_inspect(name, runner=runner))
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
