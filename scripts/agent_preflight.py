"""Format and lint an agent patch in the trusted publisher before opening a PR.

The publisher owns this script and installs only pinned tools. The Codex runner
does not spend its 1 CPU / 2 GiB budget reinstalling project dependencies.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def changed_python(root: Path) -> list[str]:
    raw = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only", "-z"], cwd=root
    )
    return [path for path in raw.decode().split("\0") if path.endswith(".py")]


def ensure_tools(*requirements: str) -> None:
    def present() -> bool:
        for requirement in requirements:
            name, version = requirement.split("==", 1)
            executable = shutil.which(name)
            if executable is None:
                return False
            reported = subprocess.run(
                [executable, "--version"], capture_output=True, text=True, check=True
            ).stdout
            first_line = reported.splitlines()[0] if reported else ""
            matches = (
                first_line == f"ruff {version}"
                if name == "ruff"
                else first_line.startswith(f"black, {version} ")
            )
            if not matches:
                return False
        return True

    if not present():
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *requirements,
            ],
            check=True,
        )
        if not present():
            raise RuntimeError("publisher formatter version does not match its pinned version")


def run(repo: str, root: Path) -> str:
    paths = changed_python(root)
    if repo == "muradjanov-dev/qurbot":
        if not paths:
            return "QurBot: no Python files changed"
        ensure_tools("ruff==0.7.4")
        subprocess.run(["ruff", "format", "--", *paths], cwd=root, check=True)
        subprocess.run(["ruff", "check", "."], cwd=root, check=True)
        subprocess.run(["ruff", "format", "--check", "."], cwd=root, check=True)
        subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
        return "QurBot: Ruff 0.7.4 check and format passed"

    if repo in {"Asadtop4ik/task-manager", "muradjanov-dev/kans-shop"}:
        roots = (
            ("backend", "bot") if repo == "Asadtop4ik/task-manager" else ("backend",)
        )
        changed = {
            part: [
                path.removeprefix(part + "/")
                for path in paths
                if path.startswith(part + "/")
            ]
            for part in roots
        }
        if not any(changed.values()):
            return f"{repo}: no backend or bot Python files changed"
        ensure_tools("ruff==0.16.0", "black==26.5.1")
        for part, local_paths in changed.items():
            if not local_paths:
                continue
            cwd = root / part
            subprocess.run(["black", "--", *local_paths], cwd=cwd, check=True)
            subprocess.run(["ruff", "check", "app", "tests"], cwd=cwd, check=True)
            subprocess.run(["black", "--check", "app", "tests"], cwd=cwd, check=True)
        subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
        return f"{repo}: Ruff 0.16.0 and Black 26.5.1 passed"

    if repo == "muradjanov-dev/ketoshop":
        if paths:
            subprocess.run(
                [sys.executable, "-m", "compileall", "-q", "--", *paths],
                cwd=root,
                check=True,
            )
        return "Ketoshop: Python syntax passed"
    raise ValueError("repository is outside the trusted preflight catalog")


if __name__ == "__main__":
    try:
        result = run(os.environ["AGENT_REPO"], Path(os.getcwd()))
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        failure = Path(os.environ["RUNNER_TEMP"]) / "agent-failure.txt"
        failure.write_text(
            "Trusted PR preflight failed; inspect the publisher job.", encoding="utf-8"
        )
        print(f"agent preflight failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    print(result)
    body = Path(os.environ["RUNNER_TEMP"]) / "agent-pr-body.md"
    if body.is_file():
        with body.open("a", encoding="utf-8") as output:
            output.write(f"\nTrusted publisher preflight: {result}.\n")
