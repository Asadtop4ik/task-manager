from __future__ import annotations

import email.message
import io
import json
import os
import socket
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agent_svc import main as main_module
from agent_svc.config import ConfigError, build_settings, load_config, load_secrets
from agent_svc.main import SdNotifier, build_arg_parser, main, self_check

from .support import build_test_context, make_github_remote
from .test_implement import _work as _implement_work

TRUSTED_DIR = Path(__file__).resolve().parents[2] / "backend" / "app" / "services"


def _http_error(status: int, payload: dict | None = None) -> urllib.error.HTTPError:
    body = json.dumps(payload).encode() if payload is not None else b""
    return urllib.error.HTTPError(
        "https://tasks.example.test/api/v1/agent-runs/lease",
        status,
        "err",
        email.message.Message(),
        io.BytesIO(body),
    )


class FakeResponse:
    def __init__(self, body: bytes = b"", *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.headers: dict[str, str] = {}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            data, self.body = self.body, b""
        else:
            data, self.body = self.body[:size], self.body[size:]
        return data


class FakeOpener:
    def __init__(self, results: list[object]) -> None:
        self._results = list(results)
        self.requests: list[object] = []

    def __call__(self, request: object, timeout: float | None = None) -> object:
        self.requests.append(request)
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _FakeCommandResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeCommandRunner:
    """Always "allows" the probed sudo rule; records every argv it was given."""

    def __init__(self, *, returncode: int = 0, stdout: str = "") -> None:
        self._returncode = returncode
        self._stdout = stdout
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> _FakeCommandResult:
        self.calls.append(list(argv))
        return _FakeCommandResult(returncode=self._returncode, stdout=self._stdout)


def _write_secrets(directory: Path) -> None:
    for name, value in (
        ("agent_svc_token", "svc-token"),
        ("callback_token", "callback-token"),
        ("intake_worker_token", "intake-token"),
        ("github_agent_token", "agent-token"),
        ("github_public_agent_token", "public-token"),
        ("github_qa_token", "qa-token"),
    ):
        (directory / name).write_text(value)


class SdNotifierTests(unittest.TestCase):
    def test_no_notify_socket_is_a_silent_no_op(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTIFY_SOCKET", None)
            notifier = SdNotifier()
            self.assertFalse(notifier.notify("READY=1"))

    def test_concrete_socket_path_receives_the_message(self) -> None:
        with TemporaryDirectory() as tmp:
            socket_path = str(Path(tmp) / "notify.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.bind(socket_path)
            try:
                notifier = SdNotifier(socket_path)
                self.assertTrue(notifier.notify("READY=1"))
                server.settimeout(2.0)
                data, _addr = server.recvfrom(1024)
                self.assertEqual(data, b"READY=1")
                notifier.close()
            finally:
                server.close()

    def test_abstract_namespace_prefix_is_translated(self) -> None:
        notifier = SdNotifier("@agent-svc-notify")
        self.assertEqual(notifier._address, "\0agent-svc-notify")
        # Nothing is listening; this must not raise even if delivery fails.
        notifier.notify("WATCHDOG=1")
        notifier.close()

    def test_reads_from_notify_socket_env_when_no_path_given(self) -> None:
        with TemporaryDirectory() as tmp:
            socket_path = str(Path(tmp) / "notify.sock")
            with patch.dict(os.environ, {"NOTIFY_SOCKET": socket_path}):
                notifier = SdNotifier()
                self.assertEqual(notifier._address, socket_path)
                notifier.close()


class ArgParserTests(unittest.TestCase):
    def test_run_subcommand(self) -> None:
        args = build_arg_parser().parse_args(["run"])
        self.assertEqual(args.command, "run")

    def test_self_check_subcommand_with_fetch_mirrors_flag(self) -> None:
        args = build_arg_parser().parse_args(["self-check", "--fetch-mirrors"])
        self.assertEqual(args.command, "self-check")
        self.assertTrue(args.fetch_mirrors)

    def test_self_check_default_flag_is_false(self) -> None:
        args = build_arg_parser().parse_args(["self-check"])
        self.assertFalse(args.fetch_mirrors)


class SelfCheckTests(unittest.TestCase):
    def _settings(
        self,
        tmp: Path,
        *,
        libexec_has_child: bool = False,
        libexec_has_image_state: bool = False,
    ):
        _write_secrets(tmp)
        config = load_config(tmp / "absent-config.json")
        config = {
            **config,
            "trusted_dir": str(TRUSTED_DIR),
            "state_dir": str(tmp / "state"),
            "work_root": str(tmp / "work"),
            "mirrors_dir": str(tmp / "mirrors"),
            "runs_dir": str(tmp / "runs"),
            "libexec_dir": str(tmp / "libexec"),
            "tools_dir": str(tmp / "tools"),
        }
        (tmp / "state").mkdir(mode=0o750)
        (tmp / "work").mkdir(mode=0o750)
        (tmp / "libexec").mkdir()
        (tmp / "tools").mkdir()
        (tmp / "tools" / "some-tool").write_text("v1")
        if libexec_has_child:
            (tmp / "libexec" / "codex_child.py").write_text("# stub\n")
        if libexec_has_image_state:
            (tmp / "libexec" / "image_state.py").write_text("# stub\n")
        secrets = load_secrets(tmp)
        return build_settings(config, secrets)

    def test_reports_missing_codex_child_without_crashing(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = self._settings(tmp, libexec_has_child=False)
            opener = FakeOpener(
                [FakeResponse(b"", status=200), FakeResponse(b"[]", status=200)]
            )
            stream = io.StringIO()
            code = self_check(settings, None, None, opener=opener, stream=stream)
            output = stream.getvalue()
            self.assertIn("FAIL codex_child missing", output)
            self.assertEqual(code, 1)

    def test_all_checks_report_ok(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = self._settings(
                tmp, libexec_has_child=True, libexec_has_image_state=True
            )
            opener = FakeOpener(
                [FakeResponse(b"", status=200), FakeResponse(b"[]", status=200)]
            )
            runner = _FakeCommandRunner(returncode=0)
            stream = io.StringIO()
            code = self_check(
                settings, None, None, opener=opener, command_runner=runner, stream=stream
            )
            output = stream.getvalue()
            self.assertIn("OK ready reachable", output)
            self.assertIn("OK api_auth", output)
            self.assertIn("OK catalog", output)
            self.assertIn("OK codex_child sudo rule allows this invocation", output)
            self.assertIn("OK image_state sudo rule allows this invocation", output)
            self.assertIn("OK tools some-tool", output)
            self.assertIn("OK mirror:Asadtop4ik/task-manager", output)
            self.assertEqual(code, 0)

    def test_never_calls_lease_and_ready_hits_the_bare_origin(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = self._settings(
                tmp, libexec_has_child=True, libexec_has_image_state=True
            )
            opener = FakeOpener(
                [FakeResponse(b"", status=200), FakeResponse(b"[]", status=200)]
            )
            runner = _FakeCommandRunner(returncode=0)
            self_check(
                settings,
                None,
                None,
                opener=opener,
                command_runner=runner,
                stream=io.StringIO(),
            )
            urls = [request.full_url for request in opener.requests]
            self.assertEqual(urls[0], "https://tasks.standart-eko.uz/ready")
            for url in urls:
                self.assertNotIn("/lease", url)
            methods = {request.get_method() for request in opener.requests}
            self.assertEqual(methods, {"GET"})

    def test_sudo_probes_use_list_mode_never_execute_the_script(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = self._settings(
                tmp, libexec_has_child=True, libexec_has_image_state=True
            )
            opener = FakeOpener(
                [FakeResponse(b"", status=200), FakeResponse(b"[]", status=200)]
            )
            runner = _FakeCommandRunner(returncode=0)
            self_check(
                settings,
                None,
                None,
                opener=opener,
                command_runner=runner,
                stream=io.StringIO(),
            )
            # codex_child (prepare), codex_child_discussion (-g task-diag-client), image_state.
            self.assertEqual(len(runner.calls), 3)
            for call in runner.calls:
                self.assertIn("-l", call)  # `sudo -n -l`: report the rule, never execute it
                self.assertNotIn("--version", call)  # codex_child.py has no such subcommand
            discussion_call = next(call for call in runner.calls if "discussion" in call)
            self.assertIn("-g", discussion_call)
            self.assertIn("task-diag-client", discussion_call)
            prepare_call = next(call for call in runner.calls if "prepare" in call)
            self.assertNotIn("-g", prepare_call)

    def test_image_state_probe_matches_the_pinned_sudoers_invocation(self) -> None:
        # ops/agent-svc.sudoers pins exactly `/usr/bin/python3 -I
        # .../image_state.py *`: the probe must include `-I` right after the
        # interpreter (matching codex_child's own prefix) or `sudo -n -l`
        # would report the rule as denied even though it is actually granted.
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = self._settings(
                tmp, libexec_has_child=True, libexec_has_image_state=True
            )
            opener = FakeOpener(
                [FakeResponse(b"", status=200), FakeResponse(b"[]", status=200)]
            )
            runner = _FakeCommandRunner(returncode=0)
            self_check(
                settings,
                None,
                None,
                opener=opener,
                command_runner=runner,
                stream=io.StringIO(),
            )
            image_state_calls = [
                call for call in runner.calls if "image_state.py" in " ".join(call)
            ]
            self.assertEqual(len(image_state_calls), 1)
            call = image_state_calls[0]
            python_index = call.index("/usr/bin/python3")
            self.assertEqual(call[python_index + 1], "-I")
            # A real catalog container name is used as the probe's trailing
            # arg (the trusted catalog's first configured container), not a
            # placeholder string.
            self.assertEqual(call[-1], "qurbot-web")

    def test_config_and_secret_errors_are_reported_without_crashing(self) -> None:
        stream = io.StringIO()
        code = self_check(
            None, ConfigError("bad config"), ConfigError("missing: x"), stream=stream
        )
        output = stream.getvalue()
        self.assertIn("FAIL config", output)
        self.assertIn("FAIL secrets", output)
        self.assertIn("FAIL catalog settings unavailable", output)
        self.assertEqual(code, 1)

    def test_api_auth_reports_failure_when_callback_token_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = self._settings(
                tmp, libexec_has_child=True, libexec_has_image_state=True
            )
            opener = FakeOpener(
                [FakeResponse(b"", status=200), _http_error(401, {"detail": "bad token"})]
            )
            runner = _FakeCommandRunner(returncode=0)
            stream = io.StringIO()
            code = self_check(
                settings, None, None, opener=opener, command_runner=runner, stream=stream
            )
            self.assertIn("FAIL api_auth", stream.getvalue())
            self.assertEqual(code, 1)

    def test_sudo_rule_denied_is_reported_as_failure(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = self._settings(
                tmp, libexec_has_child=True, libexec_has_image_state=True
            )
            opener = FakeOpener(
                [FakeResponse(b"", status=200), FakeResponse(b"[]", status=200)]
            )
            runner = _FakeCommandRunner(returncode=1, stdout="")
            stream = io.StringIO()
            code = self_check(
                settings, None, None, opener=opener, command_runner=runner, stream=stream
            )
            self.assertIn("FAIL codex_child", stream.getvalue())
            self.assertIn("FAIL image_state", stream.getvalue())
            self.assertEqual(code, 1)


class MainDispatchTests(unittest.TestCase):
    def test_missing_credentials_prints_error_and_returns_1(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CREDENTIALS_DIRECTORY", None)
                code = main(["--config", str(Path(tmp) / "absent.json"), "run"])
            self.assertEqual(code, 1)

    def test_self_check_runs_even_without_credentials(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CREDENTIALS_DIRECTORY", None)
                code = main(["--config", str(Path(tmp) / "absent.json"), "self-check"])
            self.assertEqual(code, 1)  # secrets missing, but it must not crash


class CodeLaneHandlersTests(unittest.TestCase):
    def test_implement_and_correction_are_wired(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_github_remote(root / "remote.git")
            ctx = build_test_context(root, github_remote=root / "remote.git")
            handlers = main_module._code_lane_handlers(ctx)
            self.assertEqual(set(handlers), {"implement", "correction", "review"})

    def test_review_kind_dispatches_to_the_review_handler(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_github_remote(root / "remote.git")
            ctx = build_test_context(root, github_remote=root / "remote.git")
            work = _implement_work(kind="review")
            with patch.object(main_module, "handle_review") as handle_review:
                main_module._code_lane_handlers(ctx)["review"](work)
            handle_review.assert_called_once()
            called_ctx, called_work, cancel = handle_review.call_args.args
            self.assertIs(called_ctx, ctx)
            self.assertIs(called_work, work)
            self.assertFalse(cancel.is_set())


class ShutdownTests(unittest.TestCase):
    def test_shutdown_sets_stop_and_cancels_every_active_run(self) -> None:
        import signal
        import threading

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_github_remote(root / "remote.git")
            ctx = build_test_context(root, github_remote=root / "remote.git")
            run_a_cancel = threading.Event()
            run_b_cancel = threading.Event()
            ctx.cancel_registry.register(run_a_cancel)
            ctx.cancel_registry.register(run_b_cancel)
            stop = threading.Event()

            main_module._shutdown(ctx, stop, signal.SIGTERM)

            self.assertTrue(stop.is_set())
            self.assertTrue(run_a_cancel.is_set())
            self.assertTrue(run_b_cancel.is_set())


if __name__ == "__main__":
    unittest.main()
