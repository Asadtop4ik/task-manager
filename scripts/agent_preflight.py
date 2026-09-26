"""Format and lint an agent patch in the trusted publisher before opening a PR.

The publisher owns this script and installs only pinned tools. The Codex runner
does not spend its 1 CPU / 2 GiB budget reinstalling project dependencies.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path


def changed_python(root: Path) -> list[str]:
    raw = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], cwd=root
    )
    return [path for path in raw.decode().split("\0") if path.endswith(".py")]


def _tool_version_matches(executable: str, name: str, version: str) -> bool:
    reported = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=True
    ).stdout
    first_line = reported.splitlines()[0] if reported else ""
    return (
        first_line == f"ruff {version}"
        if name == "ruff"
        else first_line.startswith(f"black, {version} ")
    )


def _resolve_tool(name: str, executable: str) -> str:
    """Resolve a local-executor ``tools`` entry to a real, executable file.

    Only ever accepts an absolute path: ``run`` invokes the lint commands
    with ``cwd=root`` -- the untrusted agent checkout -- so a relative path
    (or a bare tool name, looked up on PATH) could resolve to a completely
    different file depending on that cwd, including one an agent planted in
    its own checkout. ``resolve(strict=True)`` also collapses symlinks and
    fails closed (``RuntimeError``, not a raw ``FileNotFoundError``) when the
    path does not exist, and the file must be a regular, executable file.
    The version check and every later invocation of this tool must use this
    same resolved path, never the raw ``tools`` entry.
    """
    if not os.path.isabs(executable):
        raise RuntimeError(
            f"{name} executable must be an absolute path, got {executable!r}"
        )
    try:
        resolved = Path(executable).resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{name} executable does not exist: {executable!r}") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise RuntimeError(
            f"{name} executable is not an executable regular file: {executable!r}"
        )
    return str(resolved)


def ensure_tools(
    *requirements: str, tools: Mapping[str, str] | None = None
) -> dict[str, str] | None:
    """Make ``requirements`` (e.g. ``"ruff==0.7.4"``) available for ``run``.

    Legacy (GitHub Actions) call: ``ensure_tools(*requirements)`` keeps
    today's behavior exactly -- check PATH, pip-install the pinned versions
    when missing or mismatched, and fail if that still does not match.
    Returns ``None``.

    Local-executor call: pass ``tools`` (e.g. ``{"ruff": "/opt/agent-svc/
    tools/ruff-0.7.4/bin/ruff"}``) to use those exact executables. Each entry
    is resolved via ``_resolve_tool`` -- absolute, existing, executable --
    before its version is checked; pip is never invoked in this mode, and a
    missing/relative/mismatched executable raises ``RuntimeError``. Returns
    a ``{name: resolved_absolute_path}`` mapping that the caller (``run``)
    must use for every subsequent invocation of that tool.
    """
    if tools is not None:
        resolved: dict[str, str] = {}
        for requirement in requirements:
            name, _version = requirement.split("==", 1)
            raw_executable = tools.get(name)
            if raw_executable is None:
                raise RuntimeError(
                    f"no {name} executable was provided for the local publisher"
                )
            resolved[name] = _resolve_tool(name, raw_executable)
        for requirement in requirements:
            name, version = requirement.split("==", 1)
            if not _tool_version_matches(resolved[name], name, version):
                raise RuntimeError(
                    "publisher formatter version does not match its pinned version"
                )
        return resolved

    def present() -> bool:
        for requirement in requirements:
            name, version = requirement.split("==", 1)
            executable = shutil.which(name)
            if executable is None or not _tool_version_matches(executable, name, version):
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
            raise RuntimeError(
                "publisher formatter version does not match its pinned version"
            )
    return None


def run(repo: str, root: Path, *, tools: Mapping[str, str] | None = None) -> str:
    """Format and lint an agent patch for ``repo`` before it becomes a PR/commit.

    Legacy (GitHub Actions) call: ``run(repo, root)`` pip-installs the pinned
    tools when needed, exactly as before. Local-executor call: pass ``tools``
    (executable paths keyed by tool name) to use those exact executables and
    never invoke pip; see ``ensure_tools``. The resolved absolute path
    ``ensure_tools`` verified the version of is exactly what every command
    below invokes -- never the raw ``tools`` mapping and never a bare name --
    so a command run with ``cwd=root`` (the untrusted agent checkout) cannot
    resolve to a different, checkout-planted executable.
    """
    resolved_tools: dict[str, str] = {}

    def ensure(*requirements: str) -> None:
        if tools is None:
            ensure_tools(*requirements)
        else:
            resolved_tools.update(ensure_tools(*requirements, tools=tools))

    def exe(name: str) -> str:
        return resolved_tools[name] if tools is not None else name

    paths = changed_python(root)
    if repo == "Asadtop4ik/agent-qa":
        ensure("ruff==0.7.4")
        if paths:
            # Only safe-fix unused imports in files changed by this agent run.
            subprocess.run(
                [exe("ruff"), "check", "--fix", "--select", "F401", "--", *paths],
                cwd=root,
                check=True,
            )
            subprocess.run([exe("ruff"), "format", "--", *paths], cwd=root, check=True)
        # Config-only patches still run the whole-project checks. Auto-fixes,
        # formatting, and staging stay limited to changed Python files above.
        subprocess.run([exe("ruff"), "check", "."], cwd=root, check=True)
        subprocess.run([exe("ruff"), "format", "--check", "."], cwd=root, check=True)
        if paths:
            subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
        return "Agent QA: Ruff 0.7.4 check and format passed"
    if repo == "muradjanov-dev/qurbot":
        if not paths:
            return "QurBot: no Python files changed"
        ensure("ruff==0.7.4")
        # Restrict automatic lint edits to Ruff's safe unused-import fix on
        # files already changed by the agent; every other lint error fails closed.
        subprocess.run(
            [exe("ruff"), "check", "--fix", "--select", "F401", "--", *paths],
            cwd=root,
            check=True,
        )
        subprocess.run([exe("ruff"), "format", "--", *paths], cwd=root, check=True)
        subprocess.run([exe("ruff"), "check", "."], cwd=root, check=True)
        subprocess.run([exe("ruff"), "format", "--check", "."], cwd=root, check=True)
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
        ensure("ruff==0.16.0", "black==26.5.1")
        for part, local_paths in changed.items():
            if not local_paths:
                continue
            cwd = root / part
            subprocess.run(
                [exe("ruff"), "check", "--fix", "--select", "F401", "--", *local_paths],
                cwd=cwd,
                check=True,
            )
            subprocess.run([exe("black"), "--", *local_paths], cwd=cwd, check=True)
            subprocess.run([exe("ruff"), "check", "app", "tests"], cwd=cwd, check=True)
            subprocess.run([exe("black"), "--check", "app", "tests"], cwd=cwd, check=True)
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


def failure_reason(error: Exception, where: str = "publisher") -> str:
    """Uzbek-language failure text for ``agent-failure.txt``.

    ``where`` labels the check location; the default matches today's exact
    "PR oldi ..." (before-PR) wording used by the trusted GitHub publisher.
    A local executor can pass a different label for its own log context.
    """
    label = "PR oldi" if where == "publisher" else where
    if isinstance(error, subprocess.CalledProcessError):
        command = error.cmd
        tool = Path(
            str(command[0] if isinstance(command, (list, tuple)) else command)
        ).name
        if tool == "ruff":
            return f"{label} Ruff tekshiruvi xato berdi; faqat xavfsiz F401 avtomatik tuzatildi. GitHub logini ko‘ring."
        if tool == "black":
            return f"{label} Black tekshiruvi xato berdi. GitHub logini ko‘ring."
    return f"{label} ishonchli tekshiruv xato berdi. GitHub publisher logini ko‘ring."


if __name__ == "__main__":
    try:
        result = run(os.environ["AGENT_REPO"], Path(os.getcwd()))
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        failure = Path(os.environ["RUNNER_TEMP"]) / "agent-failure.txt"
        failure.write_text(failure_reason(error), encoding="utf-8")
        print(f"agent preflight failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
    print(result)
    body = Path(os.environ["RUNNER_TEMP"]) / "agent-pr-body.md"
    if body.is_file():
        with body.open("a", encoding="utf-8") as output:
            output.write(f"\nTrusted publisher preflight: {result}.\n")
