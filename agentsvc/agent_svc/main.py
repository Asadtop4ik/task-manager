"""CLI entry point: `run` starts the lanes; `self-check` verifies the deployment."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from . import repos
from .api import TaskManagerApi
from .config import ConfigError, Settings, build_settings, load_config, load_secrets
from .github import GitHubClient, build_token_selector
from .http import HttpError, JsonHttp
from .journal import Journal
from .lanes import ChatLane, CodeLane, WatchLoop
from .log import Logger, Redactor
from .recovery import recover

_JOIN_TIMEOUT_S = 10.0


class SdNotifier:
    """A minimal sd_notify(3) client: READY=1 / WATCHDOG=1 over an AF_UNIX datagram."""

    def __init__(self, socket_path: str | None = None) -> None:
        path = socket_path if socket_path is not None else os.environ.get("NOTIFY_SOCKET")
        self._address: str | None = None
        self._sock: socket.socket | None = None
        if path:
            address = "\0" + path[1:] if path.startswith("@") else path
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self._address = address

    def notify(self, message: str) -> bool:
        if self._sock is None or self._address is None:
            return False
        try:
            self._sock.sendto(message.encode("utf-8"), self._address)
        except OSError:
            return False
        return True

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()


def _token_selector(settings: Settings, catalog: repos.Catalog) -> Callable[[str], str]:
    return build_token_selector(
        dispatch_repo=catalog.dispatch_repo or "",
        qa_repo=catalog.qa_repo,
        public_repos=catalog.public_repos,
        agent_token=settings.github_agent_token,
        qa_token=settings.github_qa_token,
        public_token=settings.github_public_agent_token,
    )


def build_context(
    settings: Settings,
) -> tuple[Logger, TaskManagerApi, GitHubClient, repos.MirrorManager, repos.Catalog]:
    redactor = Redactor(settings.secret_values())
    logger = Logger(redactor)
    http = JsonHttp(redactor=redactor)
    catalog = repos.load_catalog(settings.trusted_dir)
    token_for = _token_selector(settings, catalog)
    github_client = GitHubClient(token_for=token_for, http=http)
    mirrors = repos.MirrorManager(settings.mirrors_dir, token_for)
    api = TaskManagerApi(
        settings.api_base_url,
        settings.agent_svc_token,
        settings.callback_token,
        settings.intake_worker_token,
        http=http,
        catalog=catalog.approved_pairs(),
        logger=logger,
    )
    return logger, api, github_client, mirrors, catalog


def run(settings: Settings) -> int:
    logger, api, _github_client, _mirrors, _catalog = build_context(settings)
    stop = threading.Event()

    def handle_signal(signum: int, _frame: Any) -> None:
        logger.event("signal_received", level="info", detail=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    journal = Journal(settings.runs_dir, logger=logger)
    for entry in recover(journal, api, logger):
        logger.event(
            "recovery_resume_pending", level="warning", run_id=entry.run_id, stage=entry.stage
        )

    code_lane = CodeLane(
        api=api,
        handlers={},
        logger=logger,
        poll_s=settings.poll_interval_s,
        enabled=settings.code_lane_enabled,
    )
    chat_lane = ChatLane(
        api=api,
        logger=logger,
        poll_s=settings.poll_interval_s,
        enabled=settings.chat_lane_enabled,
    )
    watch_loop = WatchLoop(
        checks=[],
        logger=logger,
        poll_s=settings.watch_poll_interval_s,
        enabled=settings.watch_enabled,
    )
    threads = [
        threading.Thread(
            target=code_lane.run_forever, args=(stop,), name="code-lane", daemon=True
        ),
        threading.Thread(
            target=chat_lane.run_forever, args=(stop,), name="chat-lane", daemon=True
        ),
        threading.Thread(
            target=watch_loop.run_forever, args=(stop,), name="watch-loop", daemon=True
        ),
    ]
    for thread in threads:
        thread.start()

    notifier = SdNotifier()
    try:
        notifier.notify("READY=1")
        logger.event("service_started", level="info")
        while not stop.is_set():
            notifier.notify("WATCHDOG=1")
            stop.wait(settings.sd_watchdog_interval_s)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=_JOIN_TIMEOUT_S)
        notifier.close()
        logger.event("service_stopped", level="info")
    return 0


def _emit(
    lines: list[str], ok_flags: list[bool], passed: bool, name: str, detail: str
) -> None:
    ok_flags.append(passed)
    lines.append(f"{'OK' if passed else 'FAIL'} {name} {detail}")


def self_check(
    settings: Settings | None,
    config_error: Exception | None,
    secrets_error: Exception | None,
    *,
    dry_run_mirrors: bool = True,
    command_runner: Callable[..., Any] | None = None,
    opener: Callable[..., Any] | None = None,
    stream: TextIO | None = None,
) -> int:
    out = stream if stream is not None else sys.stdout
    lines: list[str] = []
    ok_flags: list[bool] = []
    runner = command_runner or subprocess.run

    _emit(
        lines,
        ok_flags,
        config_error is None,
        "config",
        "loaded" if config_error is None else f"{type(config_error).__name__}: {config_error}",
    )
    _emit(
        lines,
        ok_flags,
        secrets_error is None,
        "secrets",
        (
            "present"
            if secrets_error is None
            else f"{type(secrets_error).__name__}: {secrets_error}"
        ),
    )

    catalog: repos.Catalog | None = None
    if settings is None:
        _emit(lines, ok_flags, False, "catalog", "settings unavailable")
    else:
        try:
            catalog = repos.load_catalog(settings.trusted_dir)
            _emit(
                lines, ok_flags, True, "catalog", f"{len(catalog.repos)} repositories loaded"
            )
        except Exception as exc:
            _emit(lines, ok_flags, False, "catalog", f"{type(exc).__name__}: {exc}")

        for name, path in (
            ("state_dir", settings.state_dir),
            ("work_root", settings.work_root),
        ):
            try:
                mode = oct(os.stat(path).st_mode & 0o777)
                _emit(lines, ok_flags, True, name, f"{path} mode={mode}")
            except OSError as exc:
                _emit(lines, ok_flags, False, name, f"{path}: {exc}")

        if secrets_error is None:
            try:
                redactor = Redactor(settings.secret_values())
                http = JsonHttp(redactor=redactor, opener=opener)
                probe = TaskManagerApi(
                    settings.api_base_url,
                    settings.agent_svc_token,
                    settings.callback_token,
                    settings.intake_worker_token,
                    http=http,
                    catalog=catalog.approved_pairs() if catalog is not None else {},
                )
                try:
                    probe.lease("check")
                except HttpError as exc:
                    if exc.status in (400, 422):
                        _emit(lines, ok_flags, True, "api", f"reachable (HTTP {exc.status})")
                    else:
                        _emit(lines, ok_flags, False, "api", f"unexpected HTTP {exc.status}")
                else:
                    _emit(lines, ok_flags, False, "api", "lease accepted an unknown lane")
            except Exception as exc:
                _emit(lines, ok_flags, False, "api", f"{type(exc).__name__}: {exc}")
        else:
            _emit(lines, ok_flags, False, "api", "secrets unavailable")

        child_path = Path(settings.libexec_dir) / "codex_child.py"
        if not child_path.is_file():
            _emit(lines, ok_flags, False, "codex_child", f"missing: {child_path}")
        else:
            try:
                argv = [*settings.codex_child_prefix, str(child_path), "--version"]
                result = runner(argv, capture_output=True, text=True, timeout=10, check=False)
                if result.returncode == 0:
                    _emit(
                        lines, ok_flags, True, "codex_child", (result.stdout or "ok").strip()
                    )
                else:
                    detail = (
                        result.stderr or result.stdout or f"exit {result.returncode}"
                    ).strip()
                    _emit(lines, ok_flags, False, "codex_child", detail[:200])
            except Exception as exc:
                _emit(lines, ok_flags, False, "codex_child", f"{type(exc).__name__}: {exc}")

        if catalog is not None:
            token_for = _token_selector(settings, catalog)
            mirrors = repos.MirrorManager(settings.mirrors_dir, token_for)
            for item in catalog.repos:
                check_name = f"mirror:{item.full_name}"
                try:
                    if dry_run_mirrors:
                        mirror_path = mirrors.ensure(item.full_name)
                        _emit(lines, ok_flags, True, check_name, f"ready at {mirror_path}")
                    else:
                        sha = mirrors.fetch(item.full_name, item.branch)
                        _emit(lines, ok_flags, True, check_name, sha)
                except Exception as exc:
                    _emit(lines, ok_flags, False, check_name, f"{type(exc).__name__}: {exc}")

        tools_dir = Path(settings.tools_dir)
        if not tools_dir.is_dir():
            _emit(lines, ok_flags, False, "tools", f"missing: {tools_dir}")
        else:
            found = sorted(item.name for item in tools_dir.iterdir())
            _emit(lines, ok_flags, True, "tools", ", ".join(found) if found else "none found")

    for line in lines:
        print(line, file=out)
    return 0 if all(ok_flags) else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-svc")
    parser.add_argument(
        "--config", default=None, help="path to the non-secret JSON config file"
    )
    parser.add_argument(
        "--credentials-dir",
        default=None,
        help="override $CREDENTIALS_DIRECTORY (local testing only)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the agent-svc lanes until terminated")
    check_parser = sub.add_parser(
        "self-check", help="verify the deployment without leasing any work"
    )
    check_parser.add_argument(
        "--fetch-mirrors",
        action="store_true",
        help="actually fetch each catalog mirror instead of only ensuring it exists",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    config: dict[str, Any] | None = None
    config_error: ConfigError | None = None
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        config_error = exc

    secrets: dict[str, str] | None = None
    secrets_error: ConfigError | None = None
    try:
        secrets = load_secrets(args.credentials_dir)
    except ConfigError as exc:
        secrets_error = exc

    settings: Settings | None = None
    if config is not None and secrets is not None:
        settings = build_settings(config, secrets)

    if args.command == "self-check":
        return self_check(
            settings, config_error, secrets_error, dry_run_mirrors=not args.fetch_mirrors
        )

    if settings is None:
        print(
            f"agent-svc: cannot start: config_error={config_error!r} "
            f"secrets_error={secrets_error!r}",
            file=sys.stderr,
        )
        return 1
    return run(settings)
