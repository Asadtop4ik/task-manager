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

from agent_svc.config import ConfigError, build_settings, load_config, load_secrets
from agent_svc.main import SdNotifier, build_arg_parser, main, self_check

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

    def __call__(self, request: object, timeout: float | None = None) -> object:
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
    def __init__(self, *, stdout: str) -> None:
        self._stdout = stdout

    def __call__(self, *args: object, **kwargs: object) -> _FakeCommandResult:
        return _FakeCommandResult(returncode=0, stdout=self._stdout)


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
    def _settings(self, tmp: Path, *, libexec_has_child: bool = False):
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
        secrets = load_secrets(tmp)
        return build_settings(config, secrets)

    def test_reports_missing_codex_child_without_crashing(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = self._settings(tmp, libexec_has_child=False)
            opener = FakeOpener([_http_error(422, {"detail": "invalid lane"})])
            stream = io.StringIO()
            code = self_check(settings, None, None, opener=opener, stream=stream)
            output = stream.getvalue()
            self.assertIn("FAIL codex_child missing", output)
            self.assertEqual(code, 1)

    def test_reachable_api_and_catalog_and_tools_report_ok(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = self._settings(tmp, libexec_has_child=True)
            opener = FakeOpener([_http_error(422, {"detail": "invalid lane"})])
            runner = _FakeCommandRunner(stdout="codex_child 0.1")
            stream = io.StringIO()
            code = self_check(
                settings, None, None, opener=opener, command_runner=runner, stream=stream
            )
            output = stream.getvalue()
            self.assertIn("OK api reachable", output)
            self.assertIn("OK catalog", output)
            self.assertIn("OK codex_child", output)
            self.assertIn("OK tools some-tool", output)
            self.assertIn("OK mirror:Asadtop4ik/task-manager", output)
            self.assertEqual(code, 0)

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

    def test_unexpected_api_status_is_a_failure(self) -> None:
        with TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            settings = self._settings(tmp, libexec_has_child=True)
            opener = FakeOpener([FakeResponse(b"", status=204)])  # lease "accepted" — bad
            runner = _FakeCommandRunner(stdout="ok")
            stream = io.StringIO()
            code = self_check(
                settings, None, None, opener=opener, command_runner=runner, stream=stream
            )
            self.assertIn("FAIL api lease accepted an unknown lane", stream.getvalue())
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


if __name__ == "__main__":
    unittest.main()
