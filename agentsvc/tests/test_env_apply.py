"""Tests for `libexec/env_apply.py`.

Runs the helper in-process (its `Deps` dataclass exists exactly so tests can
inject a fake docker/compose runner, a fake sleep, a fake readiness probe and
scratch paths without sudo, root, or a real docker daemon -- see the
`env_apply.py` module docstring). `agent_ops_policy.py`, `env_file_lock.py`
and `backend/app/services/agent_repos.py` are copied verbatim into a scratch
"trusted" directory per test, exactly as `install_agent_svc.sh` lays them
out, so these tests exercise the real policy module, not a stand-in.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
LIBEXEC_DIR = TESTS_DIR.parent / "libexec"
REPO_ROOT = TESTS_DIR.parent.parent
if str(LIBEXEC_DIR) not in sys.path:
    sys.path.insert(0, str(LIBEXEC_DIR))
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Must be set BEFORE `import env_apply`: `TEST_MODE` (and every module-level
# path/timing constant that honours it) is computed once at import time.
# Timings are shrunk here so the whole suite stays fast; per-test scenarios
# that need to actually observe contention/stability windows override them
# again on the specific `Deps` they build.
os.environ["AGENT_OPS_APPLY_TEST_MODE"] = "1"
os.environ.setdefault("AGENT_OPS_APPLY_TEST_LOCK_TIMEOUT_S", "0.3")
os.environ.setdefault("AGENT_OPS_APPLY_TEST_LOCK_RETRY_INTERVAL_S", "0.05")
os.environ.setdefault("AGENT_OPS_APPLY_TEST_RESTART_STABLE_WINDOW_S", "0")
os.environ.setdefault("AGENT_OPS_APPLY_TEST_READY_PROBE_INTERVAL_S", "0")

import agent_ops_policy as policy_ref  # noqa: E402
import env_apply as ea  # noqa: E402

QURBOT_REPO = "ghcr.io/muradjanov-dev/qurbot"
QURBOT_WEB_IMAGE_REPO = f"{QURBOT_REPO}"
SHA_A = "a" * 40
SHA_B = "b" * 40


def new_uuid() -> str:
    return str(uuid.uuid4())


def qurbot_project(*, dual_containers: bool = False) -> dict:
    containers = [["qurbot-web", QURBOT_REPO]]
    services = ["qurbot-web"]
    if dual_containers:
        containers.append(["qurbot-worker", QURBOT_REPO])
        services.append("qurbot-worker")
    return {
        "repo_full_name": "muradjanov-dev/qurbot",
        "stack": "qurbot",
        "env_file": "/srv/stack/env/qurbot.env",
        "services": services,
        "containers": containers,
        "ready_url": None,
        "keys": {
            "ADMIN_TG_IDS": {
                "format": "json_int_list",
                "ops": ["list_add", "list_remove"],
                "item_re": "[1-9][0-9]{4,14}",
                "max_items": 50,
                "protected_items": [917456291],
                "description": "Telegram admin IDs",
            },
            "SUPER_ADMIN_TG_IDS": {
                "format": "json_int_list",
                "ops": ["list_add"],
                "item_re": "[1-9][0-9]{4,14}",
                "max_items": 10,
                "protected_items": [],
                "description": "Telegram super-admin IDs",
            },
        },
    }


def allowlist_doc(**project_overrides: object) -> dict:
    project = qurbot_project()
    project.update(project_overrides)
    return {"version": 1, "projects": {"qurbot": project}}


def write_allowlist(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")
    os.chmod(path, 0o644)


def copy_trusted_dir(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        REPO_ROOT / "backend" / "app" / "services" / "agent_repos.py",
        dest / "agent_repos.py",
    )
    shutil.copy2(REPO_ROOT / "scripts" / "agent_ops_policy.py", dest / "agent_ops_policy.py")
    shutil.copy2(REPO_ROOT / "ops" / "env_file_lock.py", dest / "env_file_lock.py")


def make_request(
    *,
    project: str = "qurbot",
    key: str = "ADMIN_TG_IDS",
    op: str = "list_add",
    value: str = "55555555",
    request_id: str | None = None,
    run_id: str | None = None,
    request_hash: str | None = None,
) -> dict:
    request_id = request_id or new_uuid()
    run_id = run_id or new_uuid()
    if request_hash is None:
        request_hash = policy_ref.request_hash(
            run_id=run_id, project_key=project, kind="env_set", key=key, op=op, value=value
        )
    return {
        "v": 1,
        "mode": "apply",
        "request_id": request_id,
        "run_id": run_id,
        "project": project,
        "kind": "env_set",
        "key": key,
        "op": op,
        "value": value,
        "request_hash": request_hash,
    }


def write_request(path: Path, request: dict) -> None:
    path.write_text(json.dumps(request), encoding="utf-8")
    os.chmod(path, 0o640)


def container_state(
    image: str,
    *,
    running: bool = True,
    health: str | None = None,
    restart_count: int = 0,
    env: tuple[str, ...] = (),
) -> dict:
    state: dict = {"Running": running}
    if health is not None:
        state["Health"] = {"Status": health}
    return {
        "State": state,
        "RestartCount": restart_count,
        "Config": {"Image": image, "Env": list(env)},
    }


class FakeDocker:
    """Records every docker/compose argv; container state is a plain dict
    the test mutates directly (optionally via `compose_effect`, called each
    time a compose call happens, to simulate what a real restart would do to
    `Config.Image`/`Config.Env`/`RestartCount`/`Health`)."""

    def __init__(self) -> None:
        self.containers: dict[str, dict | None] = {}
        self.inspect_calls: list[str] = []
        self.compose_calls: list[dict] = []
        self.compose_returncode = 0
        self.compose_output = "compose up: ok\n"
        self.compose_effect: list[object] = []  # callables, popped in order

    def runner(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[1] == "inspect":
            name = argv[-1]
            self.inspect_calls.append(name)
            raw = self.containers.get(name)
            if raw is None:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such object")
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(raw), stderr="")
        if argv[1] == "compose":
            self.compose_calls.append(
                {
                    "argv": list(argv),
                    "cwd": kwargs.get("cwd"),
                    "env": dict(kwargs.get("env") or {}),
                }
            )
            if self.compose_effect:
                self.compose_effect.pop(0)()  # type: ignore[misc]
            return subprocess.CompletedProcess(
                argv, self.compose_returncode, stdout=self.compose_output, stderr=""
            )
        raise AssertionError(f"unexpected argv {argv}")


class EnvApplyTestCase(unittest.TestCase):
    """Scratch filesystem laid out like tmpfiles would on the real box."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="env-apply-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.trusted_dir = self.root / "trusted"
        self.state_dir = self.root / "state"
        self.log_dir = self.root / "log"
        self.env_file_root = self.root / "env"
        self.run_dir = self.root / "run"
        for path in (
            self.trusted_dir,
            self.state_dir / "results",
            self.state_dir / "backups",
            self.state_dir / "docker-config",
            self.log_dir,
            self.env_file_root,
            self.run_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        copy_trusted_dir(self.trusted_dir)
        self.allowlist_path = self.root / "ops-allowlist.json"
        write_allowlist(self.allowlist_path, allowlist_doc())
        self.request_path = self.run_dir / "request.json"
        self.docker = FakeDocker()

    def env_path(self, stack: str = "qurbot") -> Path:
        return self.env_file_root / f"{stack}.env"

    def write_env(self, lines: list[str], *, mode: int = 0o640) -> Path:
        path = self.env_path()
        path.write_text("".join(lines), encoding="utf-8")
        os.chmod(path, mode)
        return path

    def deps(self, **overrides: object) -> ea.Deps:
        base = dict(
            request_path=self.request_path,
            trusted_dir=self.trusted_dir,
            allowlist_path=self.allowlist_path,
            state_dir=self.state_dir,
            log_dir=self.log_dir,
            env_file_root=self.env_file_root,
            runner=self.docker.runner,
            sleep=lambda _s: None,
            probe=lambda _url, _timeout: True,
            logger_runner=lambda _line: None,
            agent_svc_uid=os.getuid(),
        )
        base.update(overrides)
        return ea.Deps(**base)  # type: ignore[arg-type]

    def seed_running_container(
        self, name: str = "qurbot-web", *, tag: str = SHA_A, env: tuple[str, ...] = ()
    ) -> None:
        self.docker.containers[name] = container_state(
            f"{QURBOT_REPO}:{tag}", running=True, health=None, restart_count=0, env=env
        )

    def run_apply(self) -> tuple[dict, int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result, exit_code = ea.cmd_apply(self.deps())
        return result, exit_code, buf.getvalue()


# ---------------------------------------------------------------------------
# bad_request (exit 2): the request file itself cannot be trusted.
# ---------------------------------------------------------------------------


class BadRequestTests(EnvApplyTestCase):
    def test_symlink_request_file_refused(self) -> None:
        real = self.run_dir / "real.json"
        write_request(real, make_request())
        os.symlink(real, self.request_path)
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["code"], "bad_request")
        self.assertEqual(result["message"], "symlink")

    def test_wrong_owner_refused(self) -> None:
        write_request(self.request_path, make_request())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result, exit_code = ea.cmd_apply(self.deps(agent_svc_uid=os.getuid() + 999999))
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["message"], "wrong_owner")

    def test_invalid_json_refused(self) -> None:
        self.request_path.write_text("{not json", encoding="utf-8")
        os.chmod(self.request_path, 0o640)
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["message"], "invalid_json")

    def test_extra_field_refused(self) -> None:
        request = make_request()
        request["extra"] = "x"
        write_request(self.request_path, request)
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["message"], "unknown_fields")

    def test_missing_field_refused(self) -> None:
        request = make_request()
        del request["op"]
        write_request(self.request_path, request)
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["message"], "missing_fields")

    def test_oversized_refused(self) -> None:
        # Every individual field is capped well under 8 KB by the shape
        # check, so oversize is only reachable via trailing padding in the
        # raw file -- `st_size` (checked before JSON is even parsed) must
        # catch this regardless of what the padding bytes contain.
        request = make_request(value="9" * 256)
        blob = json.dumps(request).encode("utf-8") + (b" " * 9000)
        self.request_path.write_bytes(blob)
        os.chmod(self.request_path, 0o640)
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["message"], "too_large")

    def test_bad_request_id_refused(self) -> None:
        request = make_request()
        request["request_id"] = "not-a-uuid"
        write_request(self.request_path, request)
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["message"], "bad_request_id")

    def test_bad_request_hash_shape_refused(self) -> None:
        request = make_request()
        request["request_hash"] = "not-hex"
        write_request(self.request_path, request)
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["message"], "bad_request_hash")

    def test_no_result_file_written_for_bad_request(self) -> None:
        write_request(self.request_path, {"v": 1})  # missing everything
        self.run_apply()
        self.assertEqual(list((self.state_dir / "results").iterdir()), [])


# ---------------------------------------------------------------------------
# refused (exit 3): policy/hash -- nothing touched.
# ---------------------------------------------------------------------------


class RefusedTests(EnvApplyTestCase):
    def test_hash_mismatch_refused(self) -> None:
        request = make_request()
        request["request_hash"] = "0" * 64
        write_request(self.request_path, request)
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 3)
        self.assertEqual(result["message"], "hash_mismatch")
        self.assertEqual(self.docker.compose_calls, [])

    def test_hard_denied_project_refused(self) -> None:
        request = make_request(project="task-manager")
        write_request(self.request_path, request)
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 3)
        self.assertEqual(result["message"], "project_denied")

    def test_unknown_key_refused(self) -> None:
        request = make_request(key="SOME_OTHER_FLAG", op="replace", value="x")
        request["request_hash"] = policy_ref.request_hash(
            run_id=request["run_id"],
            project_key="qurbot",
            kind="env_set",
            key="SOME_OTHER_FLAG",
            op="replace",
            value="x",
        )
        write_request(self.request_path, request)
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 3)
        self.assertEqual(result["message"], "not_allowlisted")

    def test_secret_looking_key_refused(self) -> None:
        request = make_request(key="DATABASE_URL", op="replace", value="x")
        request["request_hash"] = policy_ref.request_hash(
            run_id=request["run_id"],
            project_key="qurbot",
            kind="env_set",
            key="DATABASE_URL",
            op="replace",
            value="x",
        )
        write_request(self.request_path, request)
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 3)
        self.assertEqual(result["message"], "secret_key")

    def test_refused_result_persisted_but_not_idempotent_across_reruns(self) -> None:
        request = make_request()
        request["request_hash"] = "0" * 64
        write_request(self.request_path, request)
        self.run_apply()
        result_path = self.state_dir / "results" / f"{request['request_id']}.json"
        self.assertTrue(result_path.exists())
        # Fix the request and re-run under the SAME request_id: refused is
        # never idempotently short-circuited, so this must re-validate and
        # succeed this time (no stale caching of a refusal).
        good_request = dict(request)
        good_request["request_hash"] = policy_ref.request_hash(
            run_id=request["run_id"],
            project_key="qurbot",
            kind="env_set",
            key="ADMIN_TG_IDS",
            op="list_add",
            value="55555555",
        )
        write_request(self.request_path, good_request)
        # Already contains the target value on both sides -> `apply_op`
        # reports unchanged, and the container already reflects it -- the
        # `already_applied` fast path, not a fresh `applied`.
        self.write_env(["ADMIN_TG_IDS=[11111,55555555]\n"])
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111,55555555]",))
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["code"], "already_applied")


# ---------------------------------------------------------------------------
# precondition (exit 4): nothing changed.
# ---------------------------------------------------------------------------


class PreconditionTests(EnvApplyTestCase):
    def _write_request(self, **kwargs: object) -> dict:
        request = make_request(**kwargs)  # type: ignore[arg-type]
        write_request(self.request_path, request)
        return request

    def test_container_missing_precondition(self) -> None:
        self._write_request()
        # self.docker.containers left empty -> inspect fails.
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["message"], "container_missing")

    def test_container_not_running_precondition(self) -> None:
        self._write_request()
        self.docker.containers["qurbot-web"] = container_state(
            f"{QURBOT_REPO}:{SHA_A}", running=False
        )
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["message"], "container_not_running")

    def test_images_disagree_between_containers(self) -> None:
        write_allowlist(
            self.allowlist_path, allowlist_doc(**qurbot_project(dual_containers=True))
        )
        self._write_request()
        self.docker.containers["qurbot-web"] = container_state(f"{QURBOT_REPO}:{SHA_A}")
        self.docker.containers["qurbot-worker"] = container_state(f"{QURBOT_REPO}:{SHA_B}")
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["message"], "images_disagree")

    def test_image_not_a_pinned_sha_precondition(self) -> None:
        self._write_request()
        self.docker.containers["qurbot-web"] = container_state(f"{QURBOT_REPO}:latest")
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["message"], "image_not_pinned_sha")

    def test_key_missing_line_precondition(self) -> None:
        self._write_request()
        self.seed_running_container()
        self.write_env(["OTHER_KEY=1\n"])
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["message"], "key_missing_line")

    def test_duplicate_key_line_precondition(self) -> None:
        self._write_request()
        self.seed_running_container()
        self.write_env(["ADMIN_TG_IDS=[11111]\n", "ADMIN_TG_IDS=[22222]\n"])
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["message"], "duplicate_key_line")

    def test_case_variant_key_line_precondition(self) -> None:
        self._write_request()
        self.seed_running_container()
        self.write_env(["ADMIN_TG_IDS=[11111]\n", "admin_tg_ids=[1]\n"])
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["message"], "case_variant_key_line")

    def test_export_key_line_precondition(self) -> None:
        self._write_request()
        self.seed_running_container()
        self.write_env(["export ADMIN_TG_IDS=[11111]\n"])
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["message"], "export_key_line")

    def test_indented_key_line_precondition(self) -> None:
        self._write_request()
        self.seed_running_container()
        self.write_env(["  ADMIN_TG_IDS=[11111]\n"])
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["message"], "indented_key_line")

    def test_missing_key_for_list_op_on_super_admin(self) -> None:
        # SUPER_ADMIN_TG_IDS is a real allowlisted key that legitimately may
        # be absent from prod env (spec section 0 item 2) -- a list op on it
        # must be a precondition failure, not a crash.
        self._write_request(key="SUPER_ADMIN_TG_IDS", value="55555555")
        self.seed_running_container()
        self.write_env(["ADMIN_TG_IDS=[11111]\n"])
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 4)
        self.assertEqual(result["message"], "key_missing_line")

    def test_no_compose_call_on_any_precondition(self) -> None:
        self._write_request()
        self.seed_running_container()
        self.write_env(["OTHER=1\n"])
        self.run_apply()
        self.assertEqual(self.docker.compose_calls, [])

    def test_env_file_untouched_on_precondition(self) -> None:
        self._write_request()
        self.seed_running_container()
        original = ["ADMIN_TG_IDS=[11111]\n", "ADMIN_TG_IDS=[22222]\n"]
        self.write_env(original)
        self.run_apply()
        self.assertEqual(self.env_path().read_text(encoding="utf-8"), "".join(original))


# ---------------------------------------------------------------------------
# busy (exit 7): lock contention, never persisted.
# ---------------------------------------------------------------------------


class BusyTests(EnvApplyTestCase):
    def test_lock_contention_returns_busy_and_writes_nothing(self) -> None:
        request = make_request()
        write_request(self.request_path, request)
        self.seed_running_container()
        self.write_env(["ADMIN_TG_IDS=[11111]\n"])

        env_path = self.env_path()
        lock_path = env_path.with_name(env_path.name + ".lock")
        holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        import fcntl

        fcntl.flock(holder_fd, fcntl.LOCK_EX)
        try:
            result, exit_code, _ = self.run_apply()
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)
        self.assertEqual(exit_code, 7)
        self.assertEqual(result["code"], "busy")
        self.assertEqual(self.docker.compose_calls, [])
        self.assertEqual(list((self.state_dir / "results").iterdir()), [])
        audit_path = self.log_dir / "audit.jsonl"
        self.assertFalse(audit_path.exists())


# ---------------------------------------------------------------------------
# already_applied (exit 0, no restart).
# ---------------------------------------------------------------------------


class AlreadyAppliedTests(EnvApplyTestCase):
    def test_idempotent_list_add_with_matching_container_env_skips_restart(self) -> None:
        request = make_request(op="list_add", value="22222")
        write_request(self.request_path, request)
        self.write_env(["ADMIN_TG_IDS=[11111,22222]\n"])
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111,22222]",))
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["code"], "already_applied")
        self.assertFalse(result["restarted"])
        self.assertEqual(self.docker.compose_calls, [])
        # Never rewritten, not even byte-identically.
        before = self.env_path().read_text(encoding="utf-8")
        self.assertEqual(before, "ADMIN_TG_IDS=[11111,22222]\n")

    def test_second_run_with_same_request_id_reemits_without_any_docker_calls(self) -> None:
        request = make_request(op="list_add", value="22222")
        write_request(self.request_path, request)
        self.write_env(["ADMIN_TG_IDS=[11111,22222]\n"])
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111,22222]",))
        first, first_exit, _ = self.run_apply()

        self.docker.inspect_calls.clear()
        self.docker.compose_calls.clear()
        second, second_exit, _ = self.run_apply()
        self.assertEqual(second, first)
        self.assertEqual(second_exit, first_exit)
        self.assertEqual(self.docker.inspect_calls, [])
        self.assertEqual(self.docker.compose_calls, [])


# ---------------------------------------------------------------------------
# applied (exit 0): the full happy path.
# ---------------------------------------------------------------------------


class AppliedHappyPathTests(EnvApplyTestCase):
    def test_exact_compose_argv_cwd_and_env(self) -> None:
        request = make_request(op="list_add", value="33333")
        write_request(self.request_path, request)
        self.write_env(["ADMIN_TG_IDS=[11111]\n"])
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111]",))
        self.docker.compose_effect = [
            lambda: self.docker.containers.__setitem__(
                "qurbot-web",
                container_state(f"{QURBOT_REPO}:{SHA_A}", env=("ADMIN_TG_IDS=[11111,33333]",)),
            )
        ]
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["code"], "applied")
        self.assertTrue(result["restarted"])
        self.assertEqual(len(self.docker.compose_calls), 1)
        call = self.docker.compose_calls[0]
        self.assertEqual(
            call["argv"],
            [
                ea.DOCKER_BINARY,
                "compose",
                "-f",
                "stacks/qurbot.yml",
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--pull",
                "never",
                "--wait",
                "--wait-timeout",
                "180",
                "qurbot-web",
            ],
        )
        self.assertEqual(call["cwd"], str(ea.STACK_ROOT))
        self.assertEqual(set(call["env"]), {"PATH", "IMAGE_TAG", "DOCKER_CONFIG", "HOME"})
        self.assertEqual(call["env"]["IMAGE_TAG"], SHA_A)

    def test_other_env_lines_byte_identical(self) -> None:
        request = make_request(op="list_add", value="33333")
        write_request(self.request_path, request)
        lines = [
            "# a comment\n",
            "\n",
            "FOO=bar\n",
            "ADMIN_TG_IDS=[11111]\n",
            "BAZ='q u o t e d'\n",
        ]
        self.write_env(lines)
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111]",))
        self.docker.compose_effect = [
            lambda: self.docker.containers.__setitem__(
                "qurbot-web",
                container_state(f"{QURBOT_REPO}:{SHA_A}", env=("ADMIN_TG_IDS=[11111,33333]",)),
            )
        ]
        self.run_apply()
        new_lines = self.env_path().read_text(encoding="utf-8").splitlines(keepends=True)
        self.assertEqual(new_lines[0], lines[0])
        self.assertEqual(new_lines[1], lines[1])
        self.assertEqual(new_lines[2], lines[2])
        self.assertEqual(new_lines[3], "ADMIN_TG_IDS=[11111,33333]\n")
        self.assertEqual(new_lines[4], lines[4])

    def test_uid_gid_mode_preserved(self) -> None:
        request = make_request(op="list_add", value="33333")
        write_request(self.request_path, request)
        env_path = self.write_env(["ADMIN_TG_IDS=[11111]\n"], mode=0o640)
        before = os.stat(env_path)
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111]",))
        self.docker.compose_effect = [
            lambda: self.docker.containers.__setitem__(
                "qurbot-web",
                container_state(f"{QURBOT_REPO}:{SHA_A}", env=("ADMIN_TG_IDS=[11111,33333]",)),
            )
        ]
        self.run_apply()
        after = os.stat(env_path)
        self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
        self.assertEqual(after.st_uid, before.st_uid)
        self.assertEqual(after.st_gid, before.st_gid)

    def test_atomic_replace_leaves_no_temp_files_and_changes_inode(self) -> None:
        request = make_request(op="list_add", value="33333")
        write_request(self.request_path, request)
        env_path = self.write_env(["ADMIN_TG_IDS=[11111]\n"])
        inode_before = os.stat(env_path).st_ino
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111]",))
        self.docker.compose_effect = [
            lambda: self.docker.containers.__setitem__(
                "qurbot-web",
                container_state(f"{QURBOT_REPO}:{SHA_A}", env=("ADMIN_TG_IDS=[11111,33333]",)),
            )
        ]
        self.run_apply()
        inode_after = os.stat(env_path).st_ino
        self.assertNotEqual(inode_before, inode_after)
        leftovers = [
            p
            for p in self.env_file_root.iterdir()
            if p.name not in ("qurbot.env", "qurbot.env.lock")
        ]
        self.assertEqual(leftovers, [])

    def test_single_quoted_value_preserved(self) -> None:
        request = make_request(op="list_add", value="33333")
        write_request(self.request_path, request)
        self.write_env(["ADMIN_TG_IDS='[11111]'\n"])
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111,33333]",))
        self.docker.compose_effect = [lambda: None]
        _result, exit_code, _stdout = self.run_apply()
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            self.env_path().read_text(encoding="utf-8"), "ADMIN_TG_IDS='[11111,33333]'\n"
        )

    def test_double_quoted_value_preserved(self) -> None:
        request = make_request(op="list_add", value="33333")
        write_request(self.request_path, request)
        self.write_env(['ADMIN_TG_IDS="[11111]"\n'])
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111,33333]",))
        self.docker.compose_effect = [lambda: None]
        _result, exit_code, _stdout = self.run_apply()
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            self.env_path().read_text(encoding="utf-8"), 'ADMIN_TG_IDS="[11111,33333]"\n'
        )

    def test_healthcheck_path_used_when_present(self) -> None:
        request = make_request(op="list_add", value="33333")
        write_request(self.request_path, request)
        self.write_env(["ADMIN_TG_IDS=[11111]\n"])
        self.docker.containers["qurbot-web"] = container_state(
            f"{QURBOT_REPO}:{SHA_A}", env=("ADMIN_TG_IDS=[11111]",)
        )

        def after_restart() -> None:
            self.docker.containers["qurbot-web"] = container_state(
                f"{QURBOT_REPO}:{SHA_A}",
                health="healthy",
                env=("ADMIN_TG_IDS=[11111,33333]",),
            )

        self.docker.compose_effect = [after_restart]
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["code"], "applied")

    def test_ready_url_probe_called_when_configured(self) -> None:
        write_allowlist(
            self.allowlist_path, allowlist_doc(ready_url="http://127.0.0.1:8080/ready")
        )
        request = make_request(op="list_add", value="33333")
        write_request(self.request_path, request)
        self.write_env(["ADMIN_TG_IDS=[11111]\n"])
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111]",))
        self.docker.compose_effect = [
            lambda: self.docker.containers.__setitem__(
                "qurbot-web",
                container_state(f"{QURBOT_REPO}:{SHA_A}", env=("ADMIN_TG_IDS=[11111,33333]",)),
            )
        ]
        probed: list[str] = []
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _result, exit_code = ea.cmd_apply(
                self.deps(probe=lambda url, _timeout: probed.append(url) or True)
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(probed, ["http://127.0.0.1:8080/ready"])

    def test_ready_url_probe_failure_triggers_rollback(self) -> None:
        write_allowlist(
            self.allowlist_path, allowlist_doc(ready_url="http://127.0.0.1:8080/ready")
        )
        request = make_request(op="list_add", value="33333")
        write_request(self.request_path, request)
        self.write_env(["ADMIN_TG_IDS=[11111]\n"])
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111]",))

        def after_forward() -> None:
            self.docker.containers["qurbot-web"] = container_state(
                f"{QURBOT_REPO}:{SHA_A}", env=("ADMIN_TG_IDS=[11111,33333]",)
            )

        def after_rollback() -> None:
            self.docker.containers["qurbot-web"] = container_state(
                f"{QURBOT_REPO}:{SHA_A}", env=("ADMIN_TG_IDS=[11111]",)
            )

        self.docker.compose_effect = [after_forward, after_rollback]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result, exit_code = ea.cmd_apply(self.deps(probe=lambda _u, _t: False))
        self.assertEqual(exit_code, 5)
        self.assertEqual(result["message"], "ready_probe_failed")
        self.assertEqual(self.env_path().read_text(encoding="utf-8"), "ADMIN_TG_IDS=[11111]\n")


# ---------------------------------------------------------------------------
# failed_rolled_back (exit 5) / failed_rollback_failed (exit 6).
# ---------------------------------------------------------------------------


class RollbackOnVerifyFailureTests(EnvApplyTestCase):
    def test_verify_failure_rolls_back_and_restores_env(self) -> None:
        request = make_request(op="list_add", value="33333")
        write_request(self.request_path, request)
        self.write_env(["ADMIN_TG_IDS=[11111]\n"])
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111]",))

        def after_forward() -> None:
            # Restart "succeeds" but the new value never actually landed.
            self.docker.containers["qurbot-web"] = container_state(
                f"{QURBOT_REPO}:{SHA_A}", env=("ADMIN_TG_IDS=[11111]",)
            )

        def after_rollback() -> None:
            self.docker.containers["qurbot-web"] = container_state(
                f"{QURBOT_REPO}:{SHA_A}", env=("ADMIN_TG_IDS=[11111]",)
            )

        self.docker.compose_effect = [after_forward, after_rollback]
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 5)
        self.assertEqual(result["code"], "failed_rolled_back")
        self.assertTrue(result["rolled_back"])
        self.assertTrue(result["restarted"])
        self.assertEqual(len(self.docker.compose_calls), 2)
        self.assertEqual(self.env_path().read_text(encoding="utf-8"), "ADMIN_TG_IDS=[11111]\n")

    def test_rollback_itself_failing_is_reported_distinctly(self) -> None:
        request = make_request(op="list_add", value="33333")
        write_request(self.request_path, request)
        self.write_env(["ADMIN_TG_IDS=[11111]\n"])
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111]",))

        call_count = {"n": 0}

        def compose_returncode_switcher() -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                self.docker.containers["qurbot-web"] = container_state(
                    f"{QURBOT_REPO}:{SHA_A}", env=("ADMIN_TG_IDS=[11111]",)
                )
            else:
                self.docker.compose_returncode = 1  # rollback compose fails

        self.docker.compose_effect = [compose_returncode_switcher, compose_returncode_switcher]
        result, exit_code, _ = self.run_apply()
        self.assertEqual(exit_code, 6)
        self.assertEqual(result["code"], "failed_rollback_failed")
        self.assertFalse(result["rolled_back"])
        # The file is still restored to the old value even though the
        # restart-of-the-rollback failed -- we still hold the lock and know
        # for certain nothing else touched the line in between.
        self.assertEqual(self.env_path().read_text(encoding="utf-8"), "ADMIN_TG_IDS=[11111]\n")

    def test_compose_failure_on_first_restart_also_rolls_back(self) -> None:
        request = make_request(op="list_add", value="33333")
        write_request(self.request_path, request)
        self.write_env(["ADMIN_TG_IDS=[11111]\n"])
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111]",))
        self.docker.compose_returncode = 1
        result, exit_code, _ = self.run_apply()
        self.assertEqual(result["message"], "compose_failed")
        self.assertIn(exit_code, (5, 6))


# ---------------------------------------------------------------------------
# Sentinel secret: never leaks through any of our own output channels.
# ---------------------------------------------------------------------------


class SentinelSecretTests(EnvApplyTestCase):
    SENTINEL = "postgres://sentinel-SECRET-123"

    def test_sentinel_never_appears_in_stdout_result_or_audit(self) -> None:
        request = make_request(op="list_add", value="33333")
        write_request(self.request_path, request)
        self.write_env(
            [
                f"DATABASE_URL={self.SENTINEL}\n",
                "ADMIN_TG_IDS=[11111]\n",
            ]
        )
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111]",))
        self.docker.compose_effect = [
            lambda: self.docker.containers.__setitem__(
                "qurbot-web",
                container_state(f"{QURBOT_REPO}:{SHA_A}", env=("ADMIN_TG_IDS=[11111,33333]",)),
            )
        ]
        result, exit_code, stdout_text = self.run_apply()
        self.assertEqual(exit_code, 0)
        self.assertNotIn(self.SENTINEL, stdout_text)
        self.assertNotIn(self.SENTINEL, json.dumps(result))

        result_path = self.state_dir / "results" / f"{request['request_id']}.json"
        self.assertNotIn(self.SENTINEL, result_path.read_text(encoding="utf-8"))

        audit_path = self.log_dir / "audit.jsonl"
        self.assertNotIn(self.SENTINEL, audit_path.read_text(encoding="utf-8"))

        # The unrelated DATABASE_URL line is untouched on disk (legitimate,
        # pre-existing plaintext -- not something our own output leaked).
        self.assertIn(
            f"DATABASE_URL={self.SENTINEL}", self.env_path().read_text(encoding="utf-8")
        )

        compose_call = self.docker.compose_calls[0]
        self.assertNotIn(self.SENTINEL, json.dumps(compose_call["env"]))


# ---------------------------------------------------------------------------
# rollback CLI.
# ---------------------------------------------------------------------------


class RollbackCliTests(EnvApplyTestCase):
    def _seed_backup(self, request_id: str, *, current_value: str = "[11111,33333]") -> None:
        self.write_env([f"ADMIN_TG_IDS={current_value}\n"])
        ea.write_backup(
            self.state_dir / "backups",
            "qurbot",
            request_id,
            {
                "v": 1,
                "request_id": request_id,
                "run_id": new_uuid(),
                "project_key": "qurbot",
                "stack": "qurbot",
                "key": "ADMIN_TG_IDS",
                "quote_style": "none",
                "old_raw": "[11111]",
                "new_raw": "[11111,33333]",
                "pinned_image_tag": SHA_A,
                "services": ["qurbot-web"],
                "containers": [["qurbot-web", QURBOT_REPO]],
                "ts": "2026-01-01T00:00:00+00:00",
            },
        )

    def test_happy_path_restores_old_value_and_recreates(self) -> None:
        request_id = new_uuid()
        self._seed_backup(request_id)
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111,33333]",))

        def after_rollback() -> None:
            self.docker.containers["qurbot-web"] = container_state(
                f"{QURBOT_REPO}:{SHA_A}", env=("ADMIN_TG_IDS=[11111]",)
            )

        self.docker.compose_effect = [after_rollback]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result, exit_code = ea.cmd_rollback(self.deps(), request_id)
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["code"], "applied")
        self.assertEqual(self.env_path().read_text(encoding="utf-8"), "ADMIN_TG_IDS=[11111]\n")
        self.assertEqual(len(self.docker.compose_calls), 1)

    def test_refuses_when_current_line_changed(self) -> None:
        request_id = new_uuid()
        self._seed_backup(request_id, current_value="[99999]")  # not what the backup wrote
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result, exit_code = ea.cmd_rollback(self.deps(), request_id)
        self.assertEqual(exit_code, 3)
        self.assertEqual(result["message"], "line_changed")
        self.assertEqual(self.env_path().read_text(encoding="utf-8"), "ADMIN_TG_IDS=[99999]\n")
        self.assertEqual(self.docker.compose_calls, [])

    def test_backup_not_found(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result, exit_code = ea.cmd_rollback(self.deps(), new_uuid())
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["message"], "backup_not_found")

    def test_bad_request_id_shape(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result, exit_code = ea.cmd_rollback(self.deps(), "not-a-uuid")
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["message"], "bad_request_id")


# ---------------------------------------------------------------------------
# main() argv dispatch.
# ---------------------------------------------------------------------------


class MainDispatchTests(EnvApplyTestCase):
    def test_apply_dispatch(self) -> None:
        request = make_request(op="list_add", value="22222")
        write_request(self.request_path, request)
        self.write_env(["ADMIN_TG_IDS=[11111,22222]\n"])
        self.seed_running_container(env=("ADMIN_TG_IDS=[11111,22222]",))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = ea.main(["env_apply.py", "apply"], deps=self.deps())
        self.assertEqual(exit_code, 0)

    def test_apply_rejects_extra_argv(self) -> None:
        exit_code = ea.main(["env_apply.py", "apply", "extra"], deps=self.deps())
        self.assertEqual(exit_code, 2)

    def test_rollback_requires_request_id_flag(self) -> None:
        exit_code = ea.main(["env_apply.py", "rollback"], deps=self.deps())
        self.assertEqual(exit_code, 2)

    def test_unknown_subcommand(self) -> None:
        exit_code = ea.main(["env_apply.py", "bogus"], deps=self.deps())
        self.assertEqual(exit_code, 2)


# ---------------------------------------------------------------------------
# Test-mode gate itself (mirrors codex_child.py's own equivalent test).
# ---------------------------------------------------------------------------


class TestModeGateTests(unittest.TestCase):
    def test_disabled_without_env_var(self) -> None:
        saved = os.environ.pop("AGENT_OPS_APPLY_TEST_MODE", None)
        try:
            self.assertFalse(ea._test_mode_enabled())
        finally:
            if saved is not None:
                os.environ["AGENT_OPS_APPLY_TEST_MODE"] = saved

    def test_disabled_when_running_as_root(self) -> None:
        from unittest.mock import patch

        with (
            patch.dict(os.environ, {"AGENT_OPS_APPLY_TEST_MODE": "1"}),
            patch.object(ea.os, "geteuid", return_value=0),
        ):
            self.assertFalse(ea._test_mode_enabled())

    def test_enabled_as_non_root_with_env_var(self) -> None:
        from unittest.mock import patch

        with (
            patch.dict(os.environ, {"AGENT_OPS_APPLY_TEST_MODE": "1"}),
            patch.object(ea.os, "geteuid", return_value=501),
        ):
            self.assertTrue(ea._test_mode_enabled())


if __name__ == "__main__":
    unittest.main()
