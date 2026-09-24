from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_images import MAX_IMAGE_BYTES, download_images


RUN_ID = "00000000-0000-0000-0000-000000000123"
TOKEN = "test-callback-token"


class FakeResponse:
    def __init__(self, body: bytes, content_type: str) -> None:
        self.body = body
        self.headers = {"Content-Type": content_type}
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body) - self.offset
        result = self.body[self.offset : self.offset + size]
        self.offset += len(result)
        return result


def listing(*records: dict[str, object]) -> FakeResponse:
    return FakeResponse(json.dumps(list(records)).encode(), "application/json")


def image(body: bytes, mime: str) -> FakeResponse:
    return FakeResponse(body, mime)


def request_url(call) -> str:
    return call.args[0].full_url


def request_token(call) -> str | None:
    request = call.args[0]
    return request.get_header("X-agent-callback-token")


class AgentImageDownloadTests(unittest.TestCase):
    def test_downloads_multiple_allowed_images_with_auth_and_safe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            responses = [
                listing(
                    {"id": 9, "mime": "image/png", "size": 4},
                    {"id": 10, "mime": "image/webp", "size": None},
                ),
                image(b"png!", "image/png; charset=binary"),
                image(b"webp-data", "image/webp"),
            ]
            with patch("agent_images.urllib.request.urlopen", side_effect=responses) as urlopen:
                paths = download_images(
                    task_json=json.dumps({"run_id": RUN_ID}),
                    token=TOKEN,
                    runner_temp=temp,
                )

            self.assertEqual([path.name for path in paths], ["image-9.png", "image-10.webp"])
            self.assertEqual(paths[0].read_bytes(), b"png!")
            self.assertEqual(paths[1].read_bytes(), b"webp-data")
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in paths))
            self.assertEqual(
                Path(temp, "agent-image-paths.txt").read_text().splitlines(),
                [str(path) for path in paths],
            )
            self.assertEqual(urlopen.call_count, 3)
            self.assertTrue(all(request_token(call) == TOKEN for call in urlopen.call_args_list))
            self.assertTrue(request_url(urlopen.call_args_list[0]).endswith(f"/agent-runs/{RUN_ID}/images"))
            self.assertTrue(request_url(urlopen.call_args_list[1]).endswith(f"/agent-runs/{RUN_ID}/images/9"))
            self.assertTrue(request_url(urlopen.call_args_list[2]).endswith(f"/agent-runs/{RUN_ID}/images/10"))

    def test_no_images_writes_an_empty_manifest_and_makes_no_download_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with patch("agent_images.urllib.request.urlopen", return_value=listing()) as urlopen:
                paths = download_images(
                    task_json=json.dumps({"run_id": RUN_ID}),
                    token=TOKEN,
                    runner_temp=temp,
                )

            self.assertEqual(paths, [])
            self.assertEqual(urlopen.call_count, 1)
            self.assertEqual(Path(temp, "agent-image-paths.txt").read_text(), "")

    def test_rejects_too_many_images_before_downloading_any(self) -> None:
        records = [
            {"id": index, "mime": "image/png", "size": 1}
            for index in range(1, 5)
        ]
        with tempfile.TemporaryDirectory() as temp:
            with patch("agent_images.urllib.request.urlopen", return_value=listing(*records)) as urlopen:
                with self.assertRaisesRegex(ValueError, "more than three"):
                    download_images(
                        task_json=json.dumps({"run_id": RUN_ID}),
                        token=TOKEN,
                        runner_temp=temp,
                    )
            self.assertEqual(urlopen.call_count, 1)

    def test_rejects_invalid_metadata_before_fetching_images(self) -> None:
        invalid_records = (
            {"id": True, "mime": "image/png", "size": 1},
            {"id": 1, "mime": "image/gif", "size": 1},
            {"id": 1, "mime": "image/png", "size": MAX_IMAGE_BYTES + 1},
            {"id": 1, "mime": "image/png", "size": True},
        )
        for record in invalid_records:
            with self.subTest(record=record), tempfile.TemporaryDirectory() as temp:
                with patch("agent_images.urllib.request.urlopen", return_value=listing(record)) as urlopen:
                    with self.assertRaises(ValueError):
                        download_images(
                            task_json=json.dumps({"run_id": RUN_ID}),
                            token=TOKEN,
                            runner_temp=temp,
                        )
                self.assertEqual(urlopen.call_count, 1)

    def test_rejects_mime_mismatch_size_mismatch_and_oversized_download(self) -> None:
        scenarios = (
            ({"id": 1, "mime": "image/png", "size": 1}, image(b"x", "image/jpeg"), "MIME"),
            ({"id": 1, "mime": "image/png", "size": 2}, image(b"x", "image/png"), "size"),
            (
                {"id": 1, "mime": "image/png", "size": None},
                image(b"x" * (MAX_IMAGE_BYTES + 1), "image/png"),
                "exceeds 20 MB",
            ),
        )
        for record, body, error in scenarios:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temp:
                with patch(
                    "agent_images.urllib.request.urlopen",
                    side_effect=[listing(record), body],
                ):
                    with self.assertRaisesRegex(ValueError, error):
                        download_images(
                            task_json=json.dumps({"run_id": RUN_ID}),
                            token=TOKEN,
                            runner_temp=temp,
                        )
                self.assertFalse(Path(temp, "agent-images").exists())

    def test_rejects_bad_run_id_and_missing_token_before_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with patch("agent_images.urllib.request.urlopen") as urlopen:
                for task_json, token in (
                    (json.dumps({"run_id": "not-a-uuid"}), TOKEN),
                    (json.dumps({"run_id": RUN_ID}), " "),
                ):
                    with self.subTest(task_json=task_json, token=token):
                        with self.assertRaises(ValueError):
                            download_images(task_json=task_json, token=token, runner_temp=temp)
                urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
