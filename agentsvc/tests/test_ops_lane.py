from __future__ import annotations

import io
import json
import subprocess
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_svc.api import OpsWork
from agent_svc.log import Logger, Redactor
from agent_svc.ops import APPLY_TIMEOUT_S, OPS_APPLY_COMMAND, OpsLane
from agent_svc.trusted import TrustedModules

from .support import copy_trusted_dir

RUN_ID = "00000000-0000-4000-8000-000000000001"
REQUEST_UUID = "44444444-4444-4444-4444-444444444444"
LEASE_ID = "55555555-5555-5555-5555-555555555555"
# The spec's own golden vector for exactly this (run_id, project_key, kind,
# key, op, value) tuple -- if this ever stops matching `request_hash`'s
# output, either that function or this fixture drifted.
GOLDEN_HASH = "6f374a3b95d9eb840a36960be9eb88a702042b509bacce9e08911f55ee4b74ed"

ALLOWLIST_DATA = {
    "version": 1,
    "projects": {
        "qurbot": {
            "repo_full_name": "muradjanov-dev/qurbot",
            "stack": "qurbot",
            "env_file": "/srv/stack/env/qurbot.env",
            "services": ["qurbot-web", "qurbot-worker"],
            "containers": [
                ["qurbot-web", "ghcr.io/muradjanov-dev/qurbot"],
                ["qurbot-worker", "ghcr.io/muradjanov-dev/qurbot"],
            ],
            "ready_url": None,
            "keys": {
                "ADMIN_TG_IDS": {
                    "format": "json_int_list",
                    "ops": ["list_add", "list_remove"],
                    "item_re": "[1-9][0-9]{4,14}",
                    "max_items": 50,
                    "protected_items": [917456291],
                    "description": "Telegram admin IDs",
                }
            },
        }
    },
}


def _logger() -> Logger:
    return Logger(Redactor([]), stream=io.StringIO())


def _work(**overrides: Any) -> OpsWork:
    base: dict[str, Any] = dict(
        ops_id=1,
        request_uuid=REQUEST_UUID,
        run_id=RUN_ID,
        lease_id=LEASE_ID,
        lease_until=datetime.now(UTC),
        project_key="qurbot",
        repo_full_name="muradjanov-dev/qurbot",
        kind="env_set",
        key="ADMIN_TG_IDS",
        op="list_add",
        value="5339875840",
        request_hash=GOLDEN_HASH,
        deployed_sha=None,
        attempts=0,
    )
    base.update(overrides)
    return OpsWork(**base)


class _FakeOpsApi:
    def __init__(self, *, leases: list[Any] | None = None) -> None:
        self._leases = list(leases or [])
        self.results: list[dict[str, Any]] = []

    def lease_ops(self) -> OpsWork | None:
        assert self._leases, "lease_ops called with no queued lease in this test"
        result = self._leases.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def ops_result(self, ops_id: int, lease_id: str, payload: dict[str, Any]) -> None:
        self.results.append({"ops_id": ops_id, "lease_id": lease_id, **payload})


class _StubPolicyModule:
    """The real trusted `agent_ops_policy` module for everything except
    `load_allowlist`, whose file-permission checks (root-owned, ...) a
    non-root test process can never satisfy -- see its own module
    docstring. `load_allowlist_fn` is a zero-arg callable: return an
    `Allowlist` or raise, exactly like the real function would for a given
    file."""

    def __init__(self, real_module: Any, load_allowlist_fn: Any) -> None:
        self._real = real_module
        self._load_allowlist_fn = load_allowlist_fn

    def load_allowlist(self, path: Any, repositories: Any, *, require_root_owned: bool = True):
        return self._load_allowlist_fn()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _RecordingRunner:
    def __init__(self, *, raise_timeout: bool = False) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._raise_timeout = raise_timeout

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        if self._raise_timeout:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class _ApplyingRunner(_RecordingRunner):
    """Simulates a real `systemctl start agent-ops-apply.service` call: by
    the time it returns (a oneshot unit's "start" job blocks until it
    finishes), the root helper has already written its result file at
    `results_dir/<request_id>.json`. `result_request_id` (default:
    `request_id`) is the file's OWN `request_id` field -- set it to a
    different value than `request_id` to simulate a result file the lane
    must refuse to trust (right path, wrong content)."""

    def __init__(
        self,
        *,
        results_dir: Path,
        request_id: str,
        result: dict[str, Any],
        result_request_id: str | None = None,
    ) -> None:
        super().__init__()
        self._results_dir = results_dir
        self._request_id = request_id
        self._result = result
        self._result_request_id = (
            result_request_id if result_request_id is not None else request_id
        )

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        response = super().__call__(argv, **kwargs)
        self._results_dir.mkdir(parents=True, exist_ok=True)
        payload = {"request_id": self._result_request_id, **self._result}
        (self._results_dir / f"{self._request_id}.json").write_text(json.dumps(payload))
        return response


class OpsLaneTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        trusted_dir = copy_trusted_dir(root / "trusted")
        trusted = TrustedModules(trusted_dir)
        self.real_policy = trusted.agent_ops_policy
        self.repositories = trusted.agent_repos.REPOSITORIES
        self.allowlist = self.real_policy.parse_allowlist(ALLOWLIST_DATA, self.repositories)
        self.request_dir = root / "run" / "ops"
        self.results_dir = root / "var" / "results"

    def _policy(self, *, allowlist: Any = "default") -> _StubPolicyModule:
        resolved = self.allowlist if allowlist == "default" else allowlist
        if isinstance(resolved, BaseException):

            def _raise() -> Any:
                raise resolved

            return _StubPolicyModule(self.real_policy, _raise)
        return _StubPolicyModule(self.real_policy, lambda: resolved)

    def _lane(
        self,
        *,
        api: _FakeOpsApi,
        runner: Any,
        allowlist: Any = "default",
        enabled: bool = True,
        require_root_owned_result: bool = False,
    ) -> OpsLane:
        return OpsLane(
            api=api,  # type: ignore[arg-type]
            policy_module=self._policy(allowlist=allowlist),
            allowlist_path="/etc/agent-svc/ops-allowlist.json",
            catalog_repos=self.repositories,
            request_dir=str(self.request_dir),
            results_dir=str(self.results_dir),
            logger=_logger(),
            poll_s=0.0,
            enabled=enabled,
            command_runner=runner,
            require_root_owned_result=require_root_owned_result,
        )


class DisabledAndIdleTests(OpsLaneTestCase):
    def test_disabled_by_default_never_leases(self) -> None:
        lane = OpsLane(
            api=_FakeOpsApi(leases=[]),  # type: ignore[arg-type]
            policy_module=self._policy(),
            allowlist_path="x",
            catalog_repos=self.repositories,
            request_dir=str(self.request_dir),
            results_dir=str(self.results_dir),
            logger=_logger(),
            poll_s=0.0,
        )
        lane.tick()  # would assert-fail inside _FakeOpsApi.lease_ops if called

    def test_no_work_is_a_no_op(self) -> None:
        api = _FakeOpsApi(leases=[None])
        lane = self._lane(api=api, runner=_RecordingRunner())
        lane.tick()
        self.assertEqual(api.results, [])


class RevalidationTests(OpsLaneTestCase):
    def test_hash_mismatch_is_refused_without_writing_or_applying(self) -> None:
        work = _work(request_hash="0" * 64)
        api = _FakeOpsApi(leases=[work])
        runner = _RecordingRunner()
        lane = self._lane(api=api, runner=runner)
        lane.tick()

        self.assertEqual(runner.calls, [])
        self.assertFalse((self.request_dir / "request.json").exists())
        self.assertEqual(len(api.results), 1)
        self.assertEqual(api.results[0]["status"], "failed")
        self.assertEqual(api.results[0]["code"], "refused")
        self.assertIn("hash_mismatch", api.results[0]["message"])

    def test_policy_denied_is_refused_without_writing_or_applying(self) -> None:
        work = _work(
            key="NOT_LISTED",
            value="x",
            request_hash=self.real_policy.request_hash(
                run_id=RUN_ID,
                project_key="qurbot",
                kind="env_set",
                key="NOT_LISTED",
                op="list_add",
                value="x",
            ),
        )
        api = _FakeOpsApi(leases=[work])
        runner = _RecordingRunner()
        lane = self._lane(api=api, runner=runner)
        lane.tick()

        self.assertEqual(runner.calls, [])
        self.assertEqual(api.results[0]["status"], "failed")
        self.assertEqual(api.results[0]["code"], "refused")

    def test_allowlist_load_failure_is_refused_and_never_crashes(self) -> None:
        work = _work()
        api = _FakeOpsApi(leases=[work])
        runner = _RecordingRunner()
        lane = self._lane(api=api, runner=runner, allowlist=RuntimeError("disk on fire"))
        lane.tick()  # must not raise

        self.assertEqual(runner.calls, [])
        self.assertEqual(api.results[0]["status"], "failed")
        self.assertEqual(api.results[0]["code"], "refused")


class CrashRecoveryTests(OpsLaneTestCase):
    def test_existing_result_file_is_reported_without_running_apply(self) -> None:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        (self.results_dir / f"{REQUEST_UUID}.json").write_text(
            json.dumps({"request_id": REQUEST_UUID, "code": "applied", "message": "ok"})
        )
        work = _work()
        api = _FakeOpsApi(leases=[work])
        runner = _RecordingRunner()
        lane = self._lane(api=api, runner=runner)
        lane.tick()

        self.assertEqual(runner.calls, [])  # never re-ran the apply unit
        self.assertEqual(len(api.results), 1)
        self.assertEqual(api.results[0]["status"], "applied")
        self.assertEqual(api.results[0]["code"], "applied")


class ApplyFlowTests(OpsLaneTestCase):
    def test_happy_path_writes_request_file_and_reports_applied(self) -> None:
        work = _work()
        api = _FakeOpsApi(leases=[work])
        runner = _ApplyingRunner(
            results_dir=self.results_dir,
            request_id=REQUEST_UUID,
            result={"code": "applied", "message": "done", "image_tag": "a" * 40},
        )
        lane = self._lane(api=api, runner=runner)
        lane.tick()

        self.assertEqual(len(runner.calls), 1)
        argv, kwargs = runner.calls[0]
        self.assertEqual(argv, list(OPS_APPLY_COMMAND))
        self.assertEqual(kwargs["timeout"], APPLY_TIMEOUT_S)

        request_path = self.request_dir / "request.json"
        self.assertTrue(request_path.is_file())
        written = json.loads(request_path.read_text())
        self.assertEqual(written["request_id"], REQUEST_UUID)
        self.assertEqual(written["run_id"], RUN_ID)
        self.assertEqual(written["project"], "qurbot")
        self.assertEqual(written["key"], "ADMIN_TG_IDS")
        self.assertEqual(written["op"], "list_add")
        self.assertEqual(written["value"], "5339875840")
        self.assertEqual(written["request_hash"], GOLDEN_HASH)

        self.assertEqual(len(api.results), 1)
        result = api.results[0]
        self.assertEqual(result["ops_id"], 1)
        self.assertEqual(result["lease_id"], LEASE_ID)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["code"], "applied")
        self.assertEqual(result["message"], "done")
        self.assertEqual(result["image_tag"], "a" * 40)

    def test_already_applied_maps_to_applied_status(self) -> None:
        work = _work()
        api = _FakeOpsApi(leases=[work])
        runner = _ApplyingRunner(
            results_dir=self.results_dir,
            request_id=REQUEST_UUID,
            result={"code": "already_applied"},
        )
        lane = self._lane(api=api, runner=runner)
        lane.tick()
        self.assertEqual(api.results[0]["status"], "applied")

    def test_failed_codes_map_to_failed_status(self) -> None:
        for code in (
            "bad_request",
            "refused",
            "precondition",
            "failed_rolled_back",
            "failed_rollback_failed",
        ):
            with self.subTest(code=code):
                # Clear any result/request file a previous iteration left
                # behind for this same REQUEST_UUID, or the lane would treat
                # it as crash recovery instead of running the (fresh) apply.
                result_path = self.results_dir / f"{REQUEST_UUID}.json"
                result_path.unlink(missing_ok=True)
                (self.request_dir / "request.json").unlink(missing_ok=True)

                work = _work()
                api = _FakeOpsApi(leases=[work])
                runner = _ApplyingRunner(
                    results_dir=self.results_dir,
                    request_id=REQUEST_UUID,
                    result={"code": code},
                )
                lane = self._lane(api=api, runner=runner)
                lane.tick()
                self.assertEqual(api.results[0]["status"], "failed")
                self.assertEqual(api.results[0]["code"], code)

    def test_busy_result_maps_to_retry(self) -> None:
        work = _work()
        api = _FakeOpsApi(leases=[work])
        runner = _ApplyingRunner(
            results_dir=self.results_dir, request_id=REQUEST_UUID, result={"code": "busy"}
        )
        lane = self._lane(api=api, runner=runner)
        lane.tick()
        self.assertEqual(api.results[0]["status"], "retry")
        self.assertEqual(api.results[0]["code"], "busy")

    def test_missing_result_after_apply_retries_with_no_result_code(self) -> None:
        work = _work()
        api = _FakeOpsApi(leases=[work])
        runner = _RecordingRunner()  # never writes a result file
        lane = self._lane(api=api, runner=runner)
        lane.tick()

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(api.results[0]["status"], "retry")
        self.assertEqual(api.results[0]["code"], "no_result")

    def test_wrong_request_id_in_result_is_failed_bad_request(self) -> None:
        work = _work()
        api = _FakeOpsApi(leases=[work])
        # A result file for this request_uuid, but whose OWN `request_id`
        # field names a different request -- must never be trusted as this
        # request's outcome, even though the filename matches.
        runner = _ApplyingRunner(
            results_dir=self.results_dir,
            request_id=REQUEST_UUID,
            result={"code": "applied"},
            result_request_id="99999999-9999-9999-9999-999999999999",
        )
        lane = self._lane(api=api, runner=runner)
        lane.tick()

        self.assertEqual(api.results[0]["status"], "failed")
        self.assertEqual(api.results[0]["code"], "bad_request")
        self.assertIn("request_id", api.results[0]["message"])

    def test_malformed_existing_result_is_failed_bad_request(self) -> None:
        # The file exists (right path) but is not valid JSON -- must be
        # reported as failed/bad_request, never silently treated as "not
        # ready yet" (which would spin forever re-running the apply unit).
        self.results_dir.mkdir(parents=True, exist_ok=True)
        (self.results_dir / f"{REQUEST_UUID}.json").write_text("{not json")
        work = _work()
        api = _FakeOpsApi(leases=[work])
        runner = _RecordingRunner()
        lane = self._lane(api=api, runner=runner)
        lane.tick()

        self.assertEqual(runner.calls, [])  # never ran the apply unit
        self.assertEqual(api.results[0]["status"], "failed")
        self.assertEqual(api.results[0]["code"], "bad_request")

    def test_extra_result_fields_are_forwarded_when_present(self) -> None:
        work = _work()
        api = _FakeOpsApi(leases=[work])
        runner = _ApplyingRunner(
            results_dir=self.results_dir,
            request_id=REQUEST_UUID,
            result={
                "code": "failed_rolled_back",
                "exit": 5,
                "restarted": True,
                "rolled_back": True,
                "image_tag": "c" * 40,
                "message": "verify failed; rolled back",
            },
        )
        lane = self._lane(api=api, runner=runner)
        lane.tick()

        result = api.results[0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "failed_rolled_back")
        self.assertEqual(result["exit"], 5)
        self.assertTrue(result["restarted"])
        self.assertTrue(result["rolled_back"])
        self.assertEqual(result["image_tag"], "c" * 40)
        self.assertEqual(result["message"], "verify failed; rolled back")

    def test_apply_timeout_reports_nothing_and_does_not_raise(self) -> None:
        work = _work()
        api = _FakeOpsApi(leases=[work])
        runner = _RecordingRunner(raise_timeout=True)
        lane = self._lane(api=api, runner=runner)
        lane.tick()  # must not raise

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(api.results, [])


class ConstantsTests(unittest.TestCase):
    def test_ops_apply_command_matches_the_sudoers_rule(self) -> None:
        self.assertEqual(
            OPS_APPLY_COMMAND,
            (
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/systemctl",
                "start",
                "agent-ops-apply.service",
            ),
        )


if __name__ == "__main__":
    unittest.main()
