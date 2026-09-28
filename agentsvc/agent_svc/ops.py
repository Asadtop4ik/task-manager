"""`OpsLane`: leases one owner-approved env-change request at a time and
applies it through the root oneshot unit `agent-ops-apply.service`.

Design: `agent-svc-notes/ops-requests-spec.md` section 5, work package WP-C.
Its own thread in `main.run()` -- it never shares the single-Codex code
lane, and applying an env change has nothing to do with leasing/running
Codex at all. One tick:

1. `api.lease_ops()` -- at most one request `applying` globally (the
   backend enforces this with an advisory lock before handing out a lease).
2. Re-validate the leased request against the (freshly loaded) allowlist and
   recompute its `request_hash`; a policy or hash mismatch means the lease
   response cannot be trusted as-is -- report `failed`/`"refused"` and stop,
   never touch the request/result files.
3. Check for a result file already sitting under `results_dir` from a
   PREVIOUS attempt (agent-svc crashed between running the apply unit and
   reporting its result, but the unit itself finished) -- report that
   instead of running the unit again.
4. Otherwise: write `request.json` atomically under `request_dir`, run the
   fixed `OPS_APPLY_COMMAND` (a `sudo -n systemctl start
   agent-ops-apply.service`; the unit itself is the root oneshot that
   applies -- see `agentsvc/libexec/env_apply.py`, WP-D), then read the
   result file it produced and report it.

Never logs a request's `key`/`value`: only run/ops identifiers and result
codes ever reach a log line here.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .api import LeaseLost, OpsWork, TaskManagerApi
from .lanes import LoopRunner
from .log import Logger

# Sudoers (`ops/agent-svc.sudoers`, WP-D): `agent-svc ALL=(root) NOPASSWD:
# /usr/bin/systemctl start agent-ops-apply.service` -- a fixed, argv-pinned
# command with no wildcards. Injectable so tests never actually invoke sudo
# or systemd.
OPS_APPLY_COMMAND: tuple[str, ...] = (
    "/usr/bin/sudo",
    "-n",
    "/usr/bin/systemctl",
    "start",
    "agent-ops-apply.service",
)

# `ops/agent-ops-apply.service` itself is `TimeoutStartSec=600`; 660s gives
# it room to actually hit that timeout and report its own failure before we
# give up on it from this side.
APPLY_TIMEOUT_S = 660.0
_REQUEST_FILE_MODE = 0o640
_MAX_RESULT_BYTES = 8 * 1024

# Full `code` vocabulary the backend's `/agent-ops/{id}/result` endpoint
# validates as a Literal, and the status/code pairing it enforces (integrator
# contract update, superseding the plainer "busy -> retry" from spec section
# 5): `applied`/`already_applied` -> status "applied"; `busy` (from a result
# file env_apply.py itself wrote), `timeout`, `no_result` (both synthesized
# by THIS lane, never by env_apply.py) -> status "retry"; every other code
# (`bad_request`, `refused`, `precondition`, `failed_rolled_back`,
# `failed_rollback_failed`) -> status "failed". Getting either half of a
# pair wrong (e.g. status "retry" with code "refused") is rejected by the
# backend, so every call site below picks a code from exactly one of these
# three sets, never a bare string.
_APPLIED_CODES = frozenset({"applied", "already_applied"})
_FAILED_CODES = frozenset(
    {
        "bad_request",
        "refused",
        "precondition",
        "failed_rolled_back",
        "failed_rollback_failed",
    }
)
_RETRYABLE_CODES = frozenset({"busy", "timeout", "no_result"})
_MAX_MESSAGE_LEN = 300


class _ResultFileError(Exception):
    """`results/<request_uuid>.json` exists but is not a trustworthy result
    (wrong owner, not regular, too large, invalid JSON, not an object) --
    distinct from the file simply not existing yet (`_load_result_file`
    returns `None` for that instead of raising)."""


class OpsLane(LoopRunner):
    def __init__(
        self,
        *,
        api: TaskManagerApi,
        policy_module: Any,
        allowlist_path: str,
        catalog_repos: Sequence[Any],
        request_dir: str,
        results_dir: str,
        logger: Logger,
        poll_s: float,
        enabled: bool = False,
        apply_command: Sequence[str] = OPS_APPLY_COMMAND,
        command_runner: Callable[..., Any] | None = None,
        apply_timeout_s: float = APPLY_TIMEOUT_S,
        require_root_owned_result: bool = True,
    ) -> None:
        """`policy_module` is the trusted `agent_ops_policy` module
        (`ctx.trusted.agent_ops_policy`); `catalog_repos` is
        `ctx.catalog.repos`, handed to `policy_module.load_allowlist` for
        its own catalog cross-check. `require_root_owned_result` gates only
        the result file's uid-0 check (like `agent_ops_policy.load_allowlist`'s
        own `require_root_owned`) -- tests writing their own fake result file
        as a non-root user pass `False`; production never does.
        """
        super().__init__(logger=logger, poll_s=poll_s)
        self._api = api
        self._policy_module = policy_module
        self._allowlist_path = allowlist_path
        self._catalog_repos = list(catalog_repos)
        self._request_dir = Path(request_dir)
        self._results_dir = Path(results_dir)
        self._enabled = enabled
        self._apply_command = tuple(apply_command)
        self._run = command_runner or subprocess.run
        self._apply_timeout_s = apply_timeout_s
        self._require_root_owned_result = require_root_owned_result

    @property
    def name(self) -> str:
        return "ops"

    def tick(self) -> None:
        if not self._enabled:
            return
        try:
            work = self._api.lease_ops()
        except LeaseLost as exc:
            self._logger.event(
                "lease_lost", level="warning", lane=self.name, detail=exc.detail
            )
            return
        except Exception as exc:
            self._logger.error(exc, event="ops_lease_failed", lane=self.name)
            return
        if work is None:
            return
        if self.in_backoff(work.request_uuid):
            self._logger.event(
                "leased_record_in_backoff",
                level="warning",
                lane=self.name,
                run_id=work.run_id,
                detail="this ops request recently failed and is still cooling down",
            )
            return
        self.isolate(work.request_uuid, lambda: self._handle(work))

    # -- one leased request --------------------------------------------

    def _handle(self, work: OpsWork) -> None:
        self._logger.event("ops_leased", lane=self.name, run_id=work.run_id)

        try:
            allowlist = self._policy_module.load_allowlist(
                self._allowlist_path, self._catalog_repos, require_root_owned=True
            )
        except Exception as exc:
            self._logger.error(exc, event="ops_allowlist_load_failed", run_id=work.run_id)
            self._report_failed(work, "refused", "allowlist_unavailable")
            return

        ok, policy_reason = self._policy_module.validate_request(
            {"kind": work.kind, "key": work.key, "op": work.op, "value": work.value},
            allowlist,
            project_key=work.project_key,
            repo_full_name=work.repo_full_name,
        )
        if not ok:
            self._report_failed(work, "refused", policy_reason)
            return

        expected_hash = self._policy_module.request_hash(
            run_id=work.run_id,
            project_key=work.project_key,
            kind=work.kind,
            key=work.key,
            op=work.op,
            value=work.value,
        )
        if expected_hash != work.request_hash:
            self._report_failed(work, "refused", "hash_mismatch")
            return

        # Crash recovery: a previous tick may have run the apply unit and it
        # may have finished (or even started a rollback) before agent-svc
        # itself crashed or lost this lease before reporting the outcome.
        try:
            existing = self._load_result_file(work)
        except _ResultFileError as exc:
            self._report_failed(work, "bad_request", f"existing result file: {exc}")
            return
        if existing is not None:
            self._settle(work, existing)
            return

        self._write_request_file(work)

        try:
            self._run(
                list(self._apply_command),
                capture_output=True,
                text=True,
                timeout=self._apply_timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # Do nothing (never report `status=retry, code=timeout`):
            # `agent-ops-apply.service` runs under PID 1's own cgroup, not
            # ours, so it keeps running past our own timeout. The lease
            # simply expires; a later tick (ours re-leasing it, or another
            # agent-svc process) picks up the result file above.
            self._logger.event(
                "ops_apply_timed_out", level="warning", lane=self.name, run_id=work.run_id
            )
            return
        except Exception as exc:
            self._logger.error(exc, event="ops_apply_command_failed", run_id=work.run_id)
            return

        try:
            result = self._load_result_file(work)
        except _ResultFileError as exc:
            self._report_failed(work, "bad_request", f"result file: {exc}")
            return
        if result is None:
            self._report_retry(work, "no_result", "result file missing after apply")
            return
        self._settle(work, result)

    # -- request.json ----------------------------------------------------

    def _write_request_file(self, work: OpsWork) -> None:
        self._request_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "v": 1,
            "mode": "apply",
            "request_id": work.request_uuid,
            "run_id": work.run_id,
            "project": work.project_key,
            "kind": work.kind,
            "key": work.key,
            "op": work.op,
            "value": work.value,
            "request_hash": work.request_hash,
        }
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._request_dir), prefix=".tmp-", suffix=".json"
        )
        try:
            try:
                os.write(fd, data)
                os.fchmod(fd, _REQUEST_FILE_MODE)
            finally:
                os.close(fd)
            os.replace(tmp_name, self._request_dir / "request.json")
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

    # -- result file -------------------------------------------------------

    def _result_path(self, work: OpsWork) -> Path:
        return self._results_dir / f"{work.request_uuid}.json"

    def _load_result_file(self, work: OpsWork) -> dict[str, Any] | None:
        """Read `results/<request_uuid>.json`: O_NOFOLLOW, must be a regular
        file owned by uid 0 (unless overridden for tests), at most 8 KB,
        valid JSON object.

        `None` means "does not exist yet" -- an ordinary, expected state the
        caller decides what to do with (proceed to apply, or report
        `retry`/`no_result`). Every OTHER problem (wrong owner, not a
        regular file, too large, not valid JSON, not an object) means the
        file DOES exist but cannot be trusted -- raises `_ResultFileError`
        (always logged here first) so the caller reports `failed`/
        `bad_request` instead of silently treating a tampered or corrupt
        result as "not ready yet".
        """
        path = self._result_path(work)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._logger.error(exc, event="ops_result_open_failed", run_id=work.run_id)
            raise _ResultFileError("open_failed") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                self._logger.event(
                    "ops_result_not_regular", level="error", lane=self.name, run_id=work.run_id
                )
                raise _ResultFileError("not_regular")
            if self._require_root_owned_result and st.st_uid != 0:
                self._logger.event(
                    "ops_result_bad_owner", level="error", lane=self.name, run_id=work.run_id
                )
                raise _ResultFileError("bad_owner")
            raw = os.read(fd, _MAX_RESULT_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > _MAX_RESULT_BYTES:
            self._logger.event(
                "ops_result_too_large", level="error", lane=self.name, run_id=work.run_id
            )
            raise _ResultFileError("too_large")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._logger.error(exc, event="ops_result_invalid_json", run_id=work.run_id)
            raise _ResultFileError("invalid_json") from exc
        if not isinstance(parsed, dict):
            self._logger.event(
                "ops_result_not_object", level="error", lane=self.name, run_id=work.run_id
            )
            raise _ResultFileError("not_object")
        return parsed

    # -- reporting back to the backend --------------------------------------

    def _settle(self, work: OpsWork, result: dict[str, Any]) -> None:
        if result.get("request_id") != work.request_uuid:
            self._report_failed(work, "bad_request", "result_request_id_mismatch")
            return
        code = result.get("code")
        if code in _APPLIED_CODES:
            status = "applied"
        elif code in _FAILED_CODES:
            status = "failed"
        elif code in _RETRYABLE_CODES:
            status = "retry"
        else:
            self._report_failed(work, "bad_request", f"unknown result code: {code!r}")
            return
        payload: dict[str, Any] = {
            "status": status,
            "code": code,
            **self._extra_fields(result),
        }
        self._send_result(work, payload)

    def _extra_fields(self, result: dict[str, Any]) -> dict[str, Any]:
        """The rest of `env_apply.py`'s own result shape (spec section 1:
        `{code, exit, rolled_back, restarted, image_tag, message}`), passed
        through when present and well-typed -- never a value, only these
        fixed, non-secret fields."""
        extra: dict[str, Any] = {}
        if "exit" in result:
            exit_code = result["exit"]
            if exit_code is None or (
                isinstance(exit_code, int) and not isinstance(exit_code, bool)
            ):
                extra["exit"] = exit_code
        if isinstance(result.get("restarted"), bool):
            extra["restarted"] = result["restarted"]
        if isinstance(result.get("rolled_back"), bool):
            extra["rolled_back"] = result["rolled_back"]
        if isinstance(result.get("image_tag"), str):
            extra["image_tag"] = result["image_tag"]
        message = result.get("message")
        if isinstance(message, str):
            extra["message"] = message[:_MAX_MESSAGE_LEN]
        return extra

    def _report_failed(self, work: OpsWork, code: str, message: str) -> None:
        self._send_result(work, {"status": "failed", "code": code, "message": message[:300]})

    def _report_retry(self, work: OpsWork, code: str, message: str) -> None:
        self._send_result(work, {"status": "retry", "code": code, "message": message[:300]})

    def _send_result(self, work: OpsWork, payload: dict[str, Any]) -> None:
        try:
            self._api.ops_result(work.ops_id, work.lease_id, payload)
        except LeaseLost as exc:
            self._logger.event(
                "ops_result_lease_lost",
                level="warning",
                lane=self.name,
                run_id=work.run_id,
                detail=exc.detail,
            )
        except Exception as exc:
            self._logger.error(exc, event="ops_result_delivery_failed", run_id=work.run_id)
