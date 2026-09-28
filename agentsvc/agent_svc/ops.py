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
4. Otherwise: confirm the apply unit is not already running (a parallel
   `systemctl start` merges into the running job instead of starting a new
   one, which would then apply whatever request.json that OTHER run wrote);
   write `request.json` atomically under `request_dir`; run the fixed
   `OPS_APPLY_COMMAND` (a `sudo -n systemctl start agent-ops-apply.service`;
   the unit itself is the root oneshot that applies -- see
   `agentsvc/libexec/env_apply.py`, WP-D); then read the result file it
   produced and report it. `systemctl`'s own exit code is never trusted as
   proof of success on its own (it can exit 0 without the unit having done
   anything, e.g. under `ProtectProc` restrictions) -- only a genuine,
   freshly-written result file for this exact request counts.

Never logs a request's `key`/`value`: only run/ops identifiers and result
codes ever reach a log line here.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .api import InvalidOpsWork, LeaseLost, OpsWork, TaskManagerApi
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

# No sudo needed (a plain, read-only unit-state query): checked BEFORE ever
# writing `request.json`, so a parallel tick (another agent-svc process, or
# this one re-leasing after a lease-expiry race) never overwrites the
# request a currently-running apply unit is reading from.
OPS_IS_ACTIVE_COMMAND: tuple[str, ...] = (
    "/usr/bin/systemctl",
    "is-active",
    "--quiet",
    "agent-ops-apply.service",
)

# `ops/agent-ops-apply.service` itself is `TimeoutStartSec=900`; 960s gives
# it room to actually hit that timeout and report its own failure before we
# give up on it from this side.
APPLY_TIMEOUT_S = 960.0
_IS_ACTIVE_TIMEOUT_S = 10.0
_REQUEST_FILE_MODE = 0o640
_REQUEST_DIR_MODE = 0o750
_MAX_RESULT_BYTES = 8 * 1024
_STDERR_TAIL_CHARS = 500

# `env_apply.py`'s own fixed rollback-failure vocabulary (spec section 4:
# the helper "never prints any value" -- `rollback_message` is one of a
# small, fixed set of lowercase, underscore-separated words, never free text
# or anything derived from the env file).
_ROLLBACK_MESSAGE_RE = re.compile(r"[a-z_]{1,60}")
# Registry path component charset (matches a real OCI image reference tag
# shape closely enough to reject anything else outright); bounded well
# under the 300-char message cap so it can never itself blow the callback
# payload up.
_IMAGE_TAG_RE = re.compile(r"[A-Za-z0-9._:/@-]{1,100}")

# Full `code` vocabulary the backend's `/agent-ops/{id}/result` endpoint
# validates as a Literal, and the status/code pairing it enforces (integrator
# contract update, superseding the plainer "busy -> retry" from spec section
# 5): `applied`/`already_applied` -> status "applied"; `busy`, `timeout`,
# `no_result` -> status "retry"; every other code (`bad_request`, `refused`,
# `precondition`, `failed_rolled_back`, `failed_rollback_failed`) -> status
# "failed". `timeout`/`no_result`/the "already running" `busy` are all
# synthesized by THIS lane -- `env_apply.py` (WP-D) never persists a `busy`
# result file at all (a lock-acquisition failure exits before step 10 of its
# own apply algorithm, the one that writes `results/<id>.json`), so in
# practice a `busy` CODE reaching `_settle` from an actual result file never
# happens; it stays in this set purely for defense in depth (a future helper
# revision, or a hand-crafted result during an incident, might still use
# it). Getting either half of a status/code pair wrong (e.g. status "retry"
# with code "refused") is rejected by the backend, so every call site below
# picks a code from exactly one of these three sets, never a bare string.
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

# `ops_result` delivery retries: mirrors `RunScaffold.DELIVERY_RETRY_BACKOFF_S`
# (3 attempts total, 1s then 3s apart) -- the backend's terminal-status
# replay is idempotent, so re-sending the exact same outcome after a
# transient network blip costs nothing and saves this request from sitting
# unreported until the next tick re-leases it.
_RESULT_DELIVERY_BACKOFF_S: tuple[float, ...] = (1.0, 3.0)


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
        is_active_command: Sequence[str] = OPS_IS_ACTIVE_COMMAND,
        command_runner: Callable[..., Any] | None = None,
        apply_timeout_s: float = APPLY_TIMEOUT_S,
        require_root_owned_result: bool = True,
        sleep: Callable[[float], None] | None = None,
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
        self._is_active_command = tuple(is_active_command)
        self._run = command_runner or subprocess.run
        self._apply_timeout_s = apply_timeout_s
        self._require_root_owned_result = require_root_owned_result
        self._sleep = sleep or time.sleep

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
        except InvalidOpsWork as exc:
            # The identity we NEED to report anything back (ops_id AND a
            # validated lease_id) may or may not have parsed before the
            # failure. When it did, report failed/bad_request right now --
            # otherwise the backend's "at most one applying globally" lock
            # sits held by a row we can never act on until its 15-minute
            # lease naturally expires, blocking every OTHER ops request in
            # the meantime.
            self._logger.error(exc, event="ops_lease_invalid", lane=self.name)
            if exc.ops_id is not None and exc.lease_id is not None:
                self._send_result_raw(
                    exc.ops_id,
                    exc.lease_id,
                    {"status": "failed", "code": "bad_request", "message": str(exc)[:300]},
                )
            return
        except Exception as exc:
            self._logger.error(exc, event="ops_lease_failed", lane=self.name)
            return
        if work is None:
            return
        if self.in_backoff(work.request_uuid):
            # Unlike the code lane (where a lease simply expiring unused is
            # cheap -- it blocks only this one run), holding an ops lease
            # silently for its full window blocks EVERY other ops request
            # too (the backend allows only one `applying` globally). Report
            # retry/busy right away instead of sitting on it.
            self._logger.event(
                "leased_record_in_backoff",
                level="warning",
                lane=self.name,
                run_id=work.run_id,
                detail="this ops request recently failed and is still cooling down",
            )
            self._report_retry(work, "busy", "this ops request recently failed; cooling down")
            return
        self.isolate(work.request_uuid, lambda: self._handle(work))

    # -- one leased request --------------------------------------------

    def _handle(self, work: OpsWork) -> None:
        self._logger.event("ops_leased", lane=self.name, run_id=work.run_id)

        try:
            allowlist = self._policy_module.load_allowlist(
                self._allowlist_path, self._catalog_repos, require_root_owned=True
            )
        except self._policy_module.PolicyError as exc:
            # A genuine policy problem with the allowlist file itself
            # (missing, wrong owner, malformed JSON/schema, ...): not
            # transient, and re-trying it on its own won't fix anything an
            # operator hasn't also fixed.
            self._logger.error(exc, event="ops_allowlist_policy_error", run_id=work.run_id)
            self._report_failed(work, "refused", f"allowlist_invalid: {exc}")
            return
        except OSError as exc:
            # An I/O failure the policy module's own `os.open` wrapper
            # didn't already turn into a `PolicyError` (e.g. `os.fstat`/
            # `os.read` hitting EIO, ESTALE, ...) is far more likely a
            # transient filesystem hiccup than a real policy violation --
            # worth retrying rather than failing the request outright.
            self._logger.error(exc, event="ops_allowlist_read_failed", run_id=work.run_id)
            self._report_retry(work, "no_result", "allowlist temporarily unreadable")
            return
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

        if self._apply_unit_already_running(work):
            return

        self._request_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._request_dir, _REQUEST_DIR_MODE)
        self._write_request_file(work)

        try:
            result = self._run(
                list(self._apply_command),
                capture_output=True,
                text=True,
                timeout=self._apply_timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # Do nothing (never report `status=retry, code=timeout`), and
            # never unlink `request.json`: `agent-ops-apply.service` runs
            # under PID 1's own cgroup, not ours, so it (or a `systemctl
            # start` job still queued behind it) may still be running -- and
            # may still need to read that file -- past our own timeout. The
            # lease simply expires; a later tick (ours re-leasing it, or
            # another agent-svc process) picks up the result file above.
            self._logger.event(
                "ops_apply_timed_out", level="warning", lane=self.name, run_id=work.run_id
            )
            return
        except Exception as exc:
            self._logger.error(exc, event="ops_apply_command_failed", run_id=work.run_id)
            self._unlink_request_file()
            return

        # `systemctl start` for a oneshot unit genuinely returned (its
        # "start" job blocks until the unit finishes): the one-shot request
        # file has done its job either way, remove it now. Its exit code
        # itself is never trusted as proof the apply happened -- only a
        # freshly-written, request_id-matching result file counts (checked
        # right below).
        self._unlink_request_file()
        stderr_tail = (getattr(result, "stderr", None) or "")[-_STDERR_TAIL_CHARS:]
        self._logger.event(
            "ops_apply_command_finished",
            lane=self.name,
            run_id=work.run_id,
            detail=f"returncode={result.returncode}",
            stderr_tail=stderr_tail,
        )

        try:
            outcome = self._load_result_file(work)
        except _ResultFileError as exc:
            self._report_failed(work, "bad_request", f"result file: {exc}")
            return
        if outcome is None:
            self._report_retry(work, "no_result", "result file missing after apply")
            return
        self._settle(work, outcome)

    def _apply_unit_already_running(self, work: OpsWork) -> bool:
        """`systemctl is-active --quiet` on the apply unit, BEFORE ever
        writing `request.json`: a parallel `systemctl start` while the unit
        is already active/activating merges into that same running job
        instead of starting a fresh one, which would then apply whatever
        `request.json` happens to be on disk when it actually reads it --
        possibly a DIFFERENT request than the one that triggered it. Returns
        True (and has already reported retry/busy) when the unit is running
        or the check itself could not be completed -- callers must not
        proceed to write/apply in either case."""
        try:
            probe = self._run(
                list(self._is_active_command),
                capture_output=True,
                text=True,
                timeout=_IS_ACTIVE_TIMEOUT_S,
                check=False,
            )
        except Exception as exc:
            self._logger.error(exc, event="ops_is_active_check_failed", run_id=work.run_id)
            self._report_retry(
                work, "no_result", "could not check whether the apply unit is already running"
            )
            return True
        if probe.returncode == 0:
            self._report_retry(work, "busy", "apply unit is already running")
            return True
        return False

    # -- request.json ----------------------------------------------------

    def _write_request_file(self, work: OpsWork) -> None:
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

    def _unlink_request_file(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self._request_dir / "request.json").unlink()

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
        result_hash = result.get("request_hash")
        if result_hash is not None and result_hash != work.request_hash:
            self._report_failed(work, "bad_request", "result_request_hash_mismatch")
            return
        code = result.get("code")
        if not isinstance(code, str):
            # A malformed/tampered/hand-edited result file could carry any
            # JSON type here; comparing an unhashable one (a list, a dict)
            # against the code sets below would raise instead of reporting.
            self._report_failed(
                work, "bad_request", f"result has a non-string code: {type(code).__name__}"
            )
            return
        if code in _APPLIED_CODES:
            status = "applied"
        elif code in _FAILED_CODES:
            status = "failed"
        elif code in _RETRYABLE_CODES:
            status = "retry"
        else:
            self._report_failed(work, "bad_request", f"unknown result code: {code!r}")
            return

        extra = self._extra_fields(result)
        if code == "failed_rollback_failed":
            rollback_message = result.get("rollback_message")
            if isinstance(rollback_message, str) and _ROLLBACK_MESSAGE_RE.fullmatch(
                rollback_message
            ):
                extra["message"] = rollback_message
        payload: dict[str, Any] = {"status": status, "code": code, **extra}
        self._send_result(work, payload)

    def _extra_fields(self, result: dict[str, Any]) -> dict[str, Any]:
        """The rest of `env_apply.py`'s own result shape (spec section 1:
        `{code, exit, rolled_back, restarted, image_tag, message}`, plus
        `request_hash`/`env_restored`/`rollback_message` -- `request_hash`
        is checked, never forwarded, by `_settle` above; `env_restored` is
        accepted (never rejects the result over it) but not currently
        forwarded, since the backend's `/agent-ops/{id}/result` schema does
        not list it), passed through only when present and well-typed --
        malformed/out-of-range values are dropped (or, for `exit`, sent as
        `None`) rather than forwarded as-is and risking a 422 on the field
        that is supposed to be reporting a clean outcome. Never a value."""
        extra: dict[str, Any] = {}
        if "exit" in result:
            exit_code = result["exit"]
            if (
                isinstance(exit_code, int)
                and not isinstance(exit_code, bool)
                and 0 <= exit_code <= 255
            ):
                extra["exit"] = exit_code
            else:
                extra["exit"] = None
        if isinstance(result.get("restarted"), bool):
            extra["restarted"] = result["restarted"]
        if isinstance(result.get("rolled_back"), bool):
            extra["rolled_back"] = result["rolled_back"]
        image_tag = result.get("image_tag")
        if isinstance(image_tag, str) and _IMAGE_TAG_RE.fullmatch(image_tag):
            extra["image_tag"] = image_tag
        message = result.get("message")
        if isinstance(message, str):
            extra["message"] = message[:_MAX_MESSAGE_LEN]
        return extra

    def _report_failed(self, work: OpsWork, code: str, message: str) -> None:
        self._send_result(
            work, {"status": "failed", "code": code, "message": message[:_MAX_MESSAGE_LEN]}
        )

    def _report_retry(self, work: OpsWork, code: str, message: str) -> None:
        self._send_result(
            work, {"status": "retry", "code": code, "message": message[:_MAX_MESSAGE_LEN]}
        )

    def _send_result(self, work: OpsWork, payload: dict[str, Any]) -> None:
        self._send_result_raw(work.ops_id, work.lease_id, payload, run_id=work.run_id)

    def _send_result_raw(
        self, ops_id: int, lease_id: str, payload: dict[str, Any], *, run_id: str | None = None
    ) -> None:
        """POST the result, retrying transient failures up to 3 total
        attempts (1s then 3s apart) -- the backend's terminal-status replay
        is idempotent, so re-sending the same outcome is always safe.
        `LeaseLost` is never retried (the backend already decided the
        outcome by cancelling/reassigning the lease)."""
        attempts = 1 + len(_RESULT_DELIVERY_BACKOFF_S)
        for attempt in range(attempts):
            try:
                self._api.ops_result(ops_id, lease_id, payload)
                return
            except LeaseLost as exc:
                self._logger.event(
                    "ops_result_lease_lost",
                    level="warning",
                    lane=self.name,
                    run_id=run_id,
                    detail=exc.detail,
                )
                return
            except Exception as exc:
                self._logger.error(
                    exc,
                    event="ops_result_delivery_failed",
                    lane=self.name,
                    run_id=run_id,
                    attempt=attempt + 1,
                )
                if attempt < attempts - 1:
                    self._sleep(_RESULT_DELIVERY_BACKOFF_S[attempt])
