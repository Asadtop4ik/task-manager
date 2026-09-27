"""`ServiceContext`: every dependency a lane handler needs, wired from `Settings`.

`build_context` is the single place that assembles the redactor, logger, HTTP
client, trusted catalog, GitHub client, mirrors, Task Manager API client,
journal, Codex runner, and trusted-script loader from one `Settings` object —
`main.run` and the handler tests both build a `ServiceContext` this way so
there is exactly one wiring to keep correct.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import repos
from .api import TaskManagerApi
from .codex import CodexRunner
from .config import Settings
from .github import GitHubClient, build_token_selector
from .http import JsonHttp
from .journal import Journal
from .log import Logger, Redactor
from .trusted import TrustedModules


class CancelRegistry:
    """Every currently active run's `cancel` Event.

    A lane thread mid-implement/correction only ever checks its OWN
    `cancel` Event (set by its own heartbeat losing the lease); a service
    shutdown (SIGTERM/SIGINT) has no other way to ask every in-flight run to
    stop cooperatively instead of running to its own timeout. `RunScaffold`
    registers on `__enter__` and unregisters on `__exit__`; `main.run`'s
    shutdown path calls `cancel_all()`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: set[threading.Event] = set()

    def register(self, cancel: threading.Event) -> None:
        with self._lock:
            self._events.add(cancel)

    def unregister(self, cancel: threading.Event) -> None:
        with self._lock:
            self._events.discard(cancel)

    def cancel_all(self) -> None:
        with self._lock:
            events = list(self._events)
        for event in events:
            event.set()


@dataclass(frozen=True)
class ServiceContext:
    settings: Settings
    logger: Logger
    redactor: Redactor
    api: TaskManagerApi
    github: GitHubClient
    mirrors: repos.MirrorManager
    journal: Journal
    codex: CodexRunner
    catalog: repos.Catalog
    trusted: TrustedModules
    cancel_registry: CancelRegistry = field(default_factory=CancelRegistry)


def token_selector(settings: Settings, catalog: repos.Catalog) -> Callable[[str], str]:
    return build_token_selector(
        dispatch_repo=catalog.dispatch_repo or "",
        qa_repo=catalog.qa_repo,
        public_repos=catalog.public_repos,
        agent_token=settings.github_agent_token,
        qa_token=settings.github_qa_token,
        public_token=settings.github_public_agent_token,
    )


def _build_codex_runner(settings: Settings) -> CodexRunner:
    """Split `settings.codex_child_prefix` into `CodexRunner`'s prefix/interpreter.

    `codex_child_prefix` (see `config.py`) is the *full* argv prefix through
    the interpreter (e.g. `sudo -n -u agent-codex -- /usr/bin/python3`), the
    same convention `main.self_check` already uses verbatim. `CodexRunner`
    instead wants the sudo prefix and the interpreter as two separate
    arguments, so the last element becomes `python_bin` and the rest becomes
    `command_prefix` — for the default prefix this reproduces exactly
    `CodexRunner()`'s own hardcoded defaults.
    """
    prefix = list(settings.codex_child_prefix)
    kwargs: dict[str, Any] = {"libexec_dir": settings.libexec_dir}
    if prefix:
        kwargs["command_prefix"] = prefix[:-1]
        kwargs["python_bin"] = prefix[-1]
    return CodexRunner(**kwargs)


def build_context(settings: Settings) -> ServiceContext:
    # Trusted scripts (e.g. `agent_task.check_diff`'s own `git diff`/`git
    # ls-files` calls) read `os.environ` directly with no explicit override;
    # setting these here, once, at process startup is what actually hardens
    # those calls the same way every git subprocess `agent_svc` spawns
    # directly already is (no system/global git config an attacker-writable
    # HOME or /etc could otherwise supply).
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"

    redactor = Redactor(settings.secret_values())
    logger = Logger(redactor)
    http = JsonHttp(redactor=redactor)
    catalog = repos.load_catalog(settings.trusted_dir)
    token_for = token_selector(settings, catalog)
    github_client = GitHubClient(token_for=token_for, http=http)
    mirrors = repos.MirrorManager(
        settings.mirrors_dir,
        token_for,
        approved_branches=catalog.approved_pairs(),
        redactor=redactor,
    )
    api = TaskManagerApi(
        settings.api_base_url,
        settings.agent_svc_token,
        settings.callback_token,
        settings.intake_worker_token,
        http=http,
        catalog=catalog.approved_pairs(),
        logger=logger,
    )
    journal = Journal(settings.runs_dir, logger=logger)
    codex = _build_codex_runner(settings)
    trusted = TrustedModules(settings.trusted_dir)
    return ServiceContext(
        settings=settings,
        logger=logger,
        redactor=redactor,
        api=api,
        github=github_client,
        mirrors=mirrors,
        journal=journal,
        codex=codex,
        catalog=catalog,
        trusted=trusted,
    )
