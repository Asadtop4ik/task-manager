"""Container image/health snapshot. Runs as root via sudo.

argv is a list of docker container names, nothing else. Each name must be one
of the container names the trusted catalog (`agent_repos.py`) lists; anything
else is refused before any docker command runs. Prints one JSON object on
stdout: ``{name: {"image": <Config.Image or null>, "running": <bool>,
"health": <str or null>}}``.

Contract with the trusted catalog module: this script loads
`backend/app/services/agent_repos.py` (by file path, from `trusted_dir` --
the installer keeps this pointed at the active release) and expects it to
expose:
  - `REPOSITORIES`: a tuple of `AgentRepository` dataclass instances
  - `QA_REPOSITORY`: one more `AgentRepository` instance
Each `AgentRepository` has an `images: tuple[tuple[str, str], ...]` field of
(container_name, image_repo) pairs; private repos have an empty `images`
tuple. The allowed set is every container_name across `(*REPOSITORIES,
QA_REPOSITORY)`. `trusted_dir` is a plain path constant so tests can point it
at a fixture module shaped the same way instead of the real catalog.

Like `codex_child.py`, this script's production invocation is pinned in
sudoers to run isolated: `/usr/bin/python3 -I /opt/agent-svc/libexec/image_state.py <container-names...>`.
`-I` is the caller's responsibility (whatever builds that sudo command), not
this script's -- there is nothing here to change for it, but the sudoers
entry and any wrapper must include it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import uuid
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
    # A unique module name per load: this is a standalone file load (not a
    # real package import), so there is no reason to alias it to whatever
    # "agent_repos" might already mean in sys.modules, and a fixed name would
    # make repeated loads (e.g. across tests) collide.
    module_name = f"agent_repos_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _catalog_container_names(module: ModuleType) -> frozenset[str]:
    repositories = getattr(module, "REPOSITORIES", None)
    qa_repository = getattr(module, "QA_REPOSITORY", None)
    if repositories is None or qa_repository is None:
        raise RuntimeError("agent_repos module does not expose REPOSITORIES/QA_REPOSITORY")
    names: set[str] = set()
    for repo in (*repositories, qa_repository):
        images = getattr(repo, "images", ())
        for entry in images:
            if isinstance(entry, (tuple, list)) and entry and isinstance(entry[0], str):
                names.add(entry[0])
    return frozenset(names)


def _docker_inspect(name: str, *, runner: Runner) -> dict[str, Any] | None:
    try:
        completed = runner(
            [
                DOCKER_BINARY,
                "inspect",
                "--type",
                "container",
                "--format",
                "{{json .}}",
                "--",
                name,
            ],
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
