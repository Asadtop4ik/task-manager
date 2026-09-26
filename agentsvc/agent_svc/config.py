"""Non-secret JSON configuration plus systemd-credential secrets for agent-svc.

Secrets are never read from the environment or the config file: only from
``$CREDENTIALS_DIRECTORY/<name>`` files (systemd ``LoadCredential``), so that a
leaked config file or process listing never reveals a token.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# github_qa_token is optional: its absence means the QA repository/lane is
# disabled (the token selector refuses it), not a configuration error. Every
# other secret is required; missing or empty means `ConfigError`.
REQUIRED_SECRET_NAMES: tuple[str, ...] = (
    "agent_svc_token",
    "callback_token",
    "intake_worker_token",
    "github_agent_token",
    "github_public_agent_token",
)
OPTIONAL_SECRET_NAMES: tuple[str, ...] = ("github_qa_token",)
SECRET_NAMES: tuple[str, ...] = REQUIRED_SECRET_NAMES + OPTIONAL_SECRET_NAMES

DEFAULT_CONFIG_PATH = "/etc/agent-svc/config.json"

DEFAULT_MODEL_MATRIX: dict[str, dict[str, Any]] = {
    "chat": {
        "model": "gpt-6-luna",
        "effort": "medium",
        "multi_agent": False,
        "sandbox": "read-only",
    },
    "intake": {
        "model": "gpt-6-luna",
        "effort": "high",
        "multi_agent": False,
        "sandbox": "read-only",
    },
    "implement_simple": {
        "model": "gpt-6-luna",
        "effort": "high",
        "multi_agent": False,
        "sandbox": "workspace-write",
    },
    "implement_complex": {
        "model": "gpt-6-sol",
        "effort": "medium",
        "multi_agent": True,
        "sandbox": "workspace-write",
    },
    "correction_simple": {
        "model": "gpt-6-luna",
        "effort": "high",
        "multi_agent": False,
        "sandbox": "workspace-write",
    },
    "correction_complex": {
        "model": "gpt-6-sol",
        "effort": "medium",
        "multi_agent": True,
        "sandbox": "workspace-write",
    },
    "review": {
        "model": "gpt-6-sol",
        "effort": "medium",
        "multi_agent": False,
        "sandbox": "read-only",
    },
}

# Seconds. "idle" is a single global idle-timeout applied to every exec call.
DEFAULT_TIMEOUTS: dict[str, int] = {
    "intake": 180,
    "implement_simple": 1200,
    "implement_complex": 2100,
    "correction": 1200,
    "review": 480,
}
DEFAULT_IDLE_TIMEOUT_S = 480

# Absolute, pinned preflight tool executables (never resolved from PATH inside
# an untrusted checkout — see scripts/agent_preflight.py `_resolve_tool`).
# Two `ruff` versions are pinned because the trusted preflight enforces a
# different version per repository (see `agent_svc/publish.py`).
DEFAULT_PREFLIGHT_TOOL_PATHS: dict[str, str] = {
    "ruff-0.16.0": "/opt/agent-svc/tools/ruff-0.16.0/bin/ruff",
    "black-26.5.1": "/opt/agent-svc/tools/black-26.5.1/bin/black",
    "ruff-0.7.4": "/opt/agent-svc/tools/ruff-0.7.4/bin/ruff",
}

# Non-secret keys and their defaults. `None` marks a value derived from other
# settings (state_dir) unless the config file overrides it.
DEFAULT_CONFIG: dict[str, Any] = {
    "api_base_url": "https://tasks.standart-eko.uz/api/v1",
    "trusted_dir": "/opt/agent-svc/trusted",
    "libexec_dir": "/opt/agent-svc/libexec",
    "tools_dir": "/opt/agent-svc/tools",
    "state_dir": "/var/lib/agent-svc",
    "work_root": "/srv/agent-svc/work",
    # Mirrors moved out of state_dir (REVISION 2): agent-codex needs to clone
    # them, so they live under the agentwork-group work tree instead of the
    # agent-svc-only state directory.
    "mirrors_dir": "/srv/agent-svc/mirrors",
    "runs_dir": None,
    "codex_child_prefix": None,
    # REVISION 2: Codex runs as its own dedicated user `agent-codex`, never as
    # `codex-runner` (the live GitHub Actions runner account, which can read
    # things Codex's sandbox must not reach).
    "codex_home_code": "/home/agent-codex/.codex-code",
    "codex_home_chat": "/home/agent-codex/.codex-chat",
    "code_lane_enabled": False,
    "chat_lane_enabled": False,
    "watch_enabled": True,
    "poll_interval_s": 5.0,
    "watch_poll_interval_s": 15.0,
    "sd_watchdog_interval_s": 20.0,
    "model_matrix": DEFAULT_MODEL_MATRIX,
    "timeouts": DEFAULT_TIMEOUTS,
    "idle_timeout_s": DEFAULT_IDLE_TIMEOUT_S,
    "preflight_tool_paths": DEFAULT_PREFLIGHT_TOOL_PATHS,
}

_BOOL_KEYS = ("code_lane_enabled", "chat_lane_enabled", "watch_enabled")
_FLOAT_KEYS = ("poll_interval_s", "watch_poll_interval_s", "sd_watchdog_interval_s")
_STR_KEYS = (
    "api_base_url",
    "trusted_dir",
    "libexec_dir",
    "tools_dir",
    "state_dir",
    "work_root",
    "mirrors_dir",
    "codex_home_code",
    "codex_home_chat",
)


class ConfigError(ValueError):
    """A config file, credentials directory, or secret set is invalid.

    Messages built by this module never include secret values, only names.
    """


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    trusted_dir: str
    libexec_dir: str
    tools_dir: str
    state_dir: str
    work_root: str
    mirrors_dir: str
    runs_dir: str
    codex_child_prefix: tuple[str, ...]
    codex_home_code: str
    codex_home_chat: str
    code_lane_enabled: bool
    chat_lane_enabled: bool
    watch_enabled: bool
    poll_interval_s: float
    watch_poll_interval_s: float
    sd_watchdog_interval_s: float
    model_matrix: Mapping[str, Mapping[str, Any]]
    timeouts: Mapping[str, int]
    idle_timeout_s: int
    preflight_tool_paths: Mapping[str, str]
    agent_svc_token: str
    callback_token: str
    intake_worker_token: str
    github_agent_token: str
    github_public_agent_token: str
    github_qa_token: str

    def secret_values(self) -> tuple[str, ...]:
        """Every loaded secret string, for building a `log.Redactor`."""
        return tuple(getattr(self, name) for name in SECRET_NAMES)


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the non-secret JSON config, merged onto defaults."""
    path = Path(config_path if config_path is not None else DEFAULT_CONFIG_PATH)
    raw: dict[str, Any] = {}
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config file {path}: {exc}") from exc
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config file {path} is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ConfigError(f"config file {path} must contain a JSON object")
        raw = parsed
    # A missing config file (default or explicitly named) simply means "use
    # every default"; only a malformed *existing* file is an error.

    secret_keys_present = sorted(set(raw) & set(SECRET_NAMES))
    if secret_keys_present:
        raise ConfigError(
            "secrets must not appear in the config file: " + ", ".join(secret_keys_present)
        )
    unknown = sorted(set(raw) - set(DEFAULT_CONFIG))
    if unknown:
        raise ConfigError("unknown config key(s): " + ", ".join(unknown))

    merged = {**DEFAULT_CONFIG, **raw}
    for key in _BOOL_KEYS:
        if not isinstance(merged[key], bool):
            raise ConfigError(f"config key {key!r} must be a boolean")
    for key in _FLOAT_KEYS:
        value = merged[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(f"config key {key!r} must be a positive number")
        merged[key] = float(value)
    for key in _STR_KEYS:
        if not isinstance(merged[key], str) or not merged[key].strip():
            raise ConfigError(f"config key {key!r} must be a non-empty string")
    if (
        not isinstance(merged["idle_timeout_s"], int)
        or isinstance(merged["idle_timeout_s"], bool)
        or merged["idle_timeout_s"] <= 0
    ):
        raise ConfigError("config key 'idle_timeout_s' must be a positive integer")
    if not isinstance(merged["model_matrix"], dict):
        raise ConfigError("config key 'model_matrix' must be an object")
    if not isinstance(merged["timeouts"], dict):
        raise ConfigError("config key 'timeouts' must be an object")
    tool_paths = merged["preflight_tool_paths"]
    if not isinstance(tool_paths, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value.strip()
        for key, value in tool_paths.items()
    ):
        raise ConfigError(
            "config key 'preflight_tool_paths' must be an object of non-empty strings"
        )

    if merged["runs_dir"] is None:
        merged["runs_dir"] = str(Path(merged["state_dir"]) / "runs")
    elif not isinstance(merged["runs_dir"], str) or not merged["runs_dir"].strip():
        raise ConfigError("config key 'runs_dir' must be a non-empty string")

    if merged["codex_child_prefix"] is None:
        # REVISION 2: sudo to the dedicated `agent-codex` user, not the
        # `codex-runner` GitHub Actions runner account. `-I` (isolated mode:
        # ignores PYTHONPATH/user site-packages/etc.) is pinned by the
        # sudoers rule itself, so it must always be present here too.
        merged["codex_child_prefix"] = [
            "/usr/bin/sudo",
            "-n",
            "-u",
            "agent-codex",
            "--",
            "/usr/bin/python3",
            "-I",
        ]
    else:
        prefix = merged["codex_child_prefix"]
        if not isinstance(prefix, list) or not all(isinstance(item, str) for item in prefix):
            raise ConfigError("config key 'codex_child_prefix' must be a list of strings")
    merged["codex_child_prefix"] = tuple(merged["codex_child_prefix"])

    return merged


def load_secrets(credentials_dir: str | Path | None = None) -> dict[str, str]:
    """Load every secret from `$CREDENTIALS_DIRECTORY/<name>` files.

    `OPTIONAL_SECRET_NAMES` (currently just `github_qa_token`) may be absent
    or empty: that means the feature it gates (the QA repo/lane) is disabled,
    so the result carries `""` for it. systemd's LoadCredential= has no
    optional form, so the installer always writes this file, empty when the
    QA token is not provisioned. Required secrets must be non-empty.
    """
    resolved = (
        credentials_dir
        if credentials_dir is not None
        else os.environ.get("CREDENTIALS_DIRECTORY")
    )
    if not resolved:
        raise ConfigError(
            "CREDENTIALS_DIRECTORY is not set; pass credentials_dir explicitly for tests"
        )
    directory = Path(resolved)
    if not directory.is_dir():
        raise ConfigError(f"credentials directory not found: {directory}")

    missing: list[str] = []
    invalid: list[str] = []
    values: dict[str, str] = {}
    for name in SECRET_NAMES:
        path = directory / name
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            if name in OPTIONAL_SECRET_NAMES:
                values[name] = ""
                continue
            missing.append(name)
            continue
        except OSError:
            invalid.append(name)
            continue
        value = raw[:-1] if raw.endswith("\n") else raw
        if not value or value.isspace():
            if name in OPTIONAL_SECRET_NAMES:
                values[name] = ""
                continue
            invalid.append(name)
            continue
        values[name] = value

    if missing:
        raise ConfigError("missing required secret(s): " + ", ".join(missing))
    if invalid:
        raise ConfigError(
            "invalid secret(s) (empty or whitespace-only): " + ", ".join(invalid)
        )
    return values


def build_settings(config: Mapping[str, Any], secrets: Mapping[str, str]) -> Settings:
    """Combine a validated config mapping and secret set into `Settings`."""
    missing_secrets = sorted(set(SECRET_NAMES) - set(secrets))
    if missing_secrets:
        raise ConfigError("missing required secret(s): " + ", ".join(missing_secrets))
    return Settings(
        api_base_url=config["api_base_url"],
        trusted_dir=config["trusted_dir"],
        libexec_dir=config["libexec_dir"],
        tools_dir=config["tools_dir"],
        state_dir=config["state_dir"],
        work_root=config["work_root"],
        mirrors_dir=config["mirrors_dir"],
        runs_dir=config["runs_dir"],
        codex_child_prefix=tuple(config["codex_child_prefix"]),
        codex_home_code=config["codex_home_code"],
        codex_home_chat=config["codex_home_chat"],
        code_lane_enabled=config["code_lane_enabled"],
        chat_lane_enabled=config["chat_lane_enabled"],
        watch_enabled=config["watch_enabled"],
        poll_interval_s=config["poll_interval_s"],
        watch_poll_interval_s=config["watch_poll_interval_s"],
        sd_watchdog_interval_s=config["sd_watchdog_interval_s"],
        model_matrix=config["model_matrix"],
        timeouts=config["timeouts"],
        idle_timeout_s=config["idle_timeout_s"],
        preflight_tool_paths=config["preflight_tool_paths"],
        agent_svc_token=secrets["agent_svc_token"],
        callback_token=secrets["callback_token"],
        intake_worker_token=secrets["intake_worker_token"],
        github_agent_token=secrets["github_agent_token"],
        github_public_agent_token=secrets["github_public_agent_token"],
        github_qa_token=secrets["github_qa_token"],
    )


def load_settings(
    config_path: str | Path | None = None,
    credentials_dir: str | Path | None = None,
) -> Settings:
    """Load non-secret config and secrets together, as the running service does."""
    config = load_config(config_path)
    secrets = load_secrets(credentials_dir)
    return build_settings(config, secrets)
