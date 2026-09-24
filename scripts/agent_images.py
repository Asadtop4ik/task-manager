"""Download authenticated task attachments for the trusted Codex step."""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Any
from uuid import UUID

API_BASE_URL = "https://tasks.standart-eko.uz/api/v1"
MAX_IMAGES = 3
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_LIST_BYTES = 64 * 1024
ALLOWED_MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
REQUEST_TIMEOUT_SECONDS = 30


def _authenticated_get(url: str, token: str) -> urllib.request.Request:
    request = urllib.request.Request(url, method="GET")
    # Keep the callback secret off any redirected request.
    request.add_unredirected_header("X-Agent-Callback-Token", token)
    return request


def _content_type(headers: Any) -> str:
    value = headers.get("Content-Type", "")
    return value.split(";", 1)[0].strip().lower()


def _read_json_response(response: Any) -> Any:
    if _content_type(response.headers) != "application/json":
        raise ValueError("image listing response was not JSON")
    raw = response.read(MAX_LIST_BYTES + 1)
    if len(raw) > MAX_LIST_BYTES:
        raise ValueError("image listing response is too large")
    return json.loads(raw)


def _image_metadata(value: Any) -> tuple[int, str, int | None]:
    if not isinstance(value, dict):
        raise ValueError("invalid image metadata")
    image_id = value.get("id")
    mime = value.get("mime")
    size = value.get("size")
    if isinstance(image_id, bool) or not isinstance(image_id, int) or image_id < 1:
        raise ValueError("invalid image ID")
    if not isinstance(mime, str) or mime not in ALLOWED_MIME_EXTENSIONS:
        raise ValueError("unsupported image MIME type")
    if size is not None and (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 1
        or size > MAX_IMAGE_BYTES
    ):
        raise ValueError("invalid image size")
    return image_id, mime, size


def _prepare_output_paths(runner_temp: Path) -> tuple[Path, Path]:
    image_dir = runner_temp / "agent-images"
    manifest = runner_temp / "agent-image-paths.txt"
    if image_dir.is_symlink():
        image_dir.unlink()
    elif image_dir.exists():
        if image_dir.is_dir():
            shutil.rmtree(image_dir)
        else:
            image_dir.unlink()
    manifest.unlink(missing_ok=True)
    image_dir.mkdir(mode=0o700, parents=True)
    image_dir.chmod(0o700)
    return image_dir, manifest


def download_images(
    *, task_json: str, token: str, runner_temp: str | Path
) -> list[Path]:
    """Fetch and validate task images, returning paths safe for Codex CLI args."""
    try:
        payload = json.loads(task_json)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid task payload") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid task payload")
    try:
        run_id = str(UUID(str(payload.get("run_id"))))
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid agent run ID") from exc
    if not token.strip():
        raise ValueError("agent callback token is missing")

    runner_temp_path = Path(runner_temp)
    runner_temp_path.mkdir(parents=True, exist_ok=True)
    image_dir, manifest = _prepare_output_paths(runner_temp_path)
    listing_url = f"{API_BASE_URL}/agent-runs/{run_id}/images"
    listing_request = _authenticated_get(listing_url, token)
    with urllib.request.urlopen(
        listing_request, timeout=REQUEST_TIMEOUT_SECONDS
    ) as response:
        records = _read_json_response(response)

    if not isinstance(records, list):
        raise ValueError("image listing must be a list")
    if len(records) > MAX_IMAGES:
        raise ValueError("task has more than three images")

    validated_records: list[tuple[int, str, int | None]] = []
    seen_ids: set[int] = set()
    for record in records:
        image_id, expected_mime, expected_size = _image_metadata(record)
        if image_id in seen_ids:
            raise ValueError("duplicate image ID")
        seen_ids.add(image_id)
        validated_records.append((image_id, expected_mime, expected_size))

    paths: list[Path] = []
    succeeded = False
    try:
        for image_id, expected_mime, expected_size in validated_records:
            url = f"{API_BASE_URL}/agent-runs/{run_id}/images/{image_id}"
            request = _authenticated_get(url, token)
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                actual_mime = _content_type(response.headers)
                if actual_mime != expected_mime:
                    raise ValueError("downloaded image MIME type does not match metadata")
                data = response.read(MAX_IMAGE_BYTES + 1)

            if not data:
                raise ValueError("downloaded image is empty")
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("downloaded image exceeds 20 MB")
            if expected_size is not None and len(data) != expected_size:
                raise ValueError("downloaded image size does not match metadata")

            extension = ALLOWED_MIME_EXTENSIONS[expected_mime]
            image_path = image_dir / f"image-{image_id}.{extension}"
            file_descriptor = os.open(
                image_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(file_descriptor, "wb") as image_file:
                image_file.write(data)
            paths.append(image_path)

        manifest.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")
        manifest.chmod(0o600)
        succeeded = True
        return paths
    finally:
        if not succeeded:
            shutil.rmtree(image_dir, ignore_errors=True)
            manifest.unlink(missing_ok=True)


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] != "download":
        raise SystemExit("usage: agent_images.py download")
    paths = download_images(
        task_json=os.environ["TASK_JSON"],
        token=os.environ["AGENT_CALLBACK_TOKEN"],
        runner_temp=os.environ["RUNNER_TEMP"],
    )
    print(f"Prepared {len(paths)} task image(s) for Codex.")


if __name__ == "__main__":
    main()
