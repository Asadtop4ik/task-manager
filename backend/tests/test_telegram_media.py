import httpx
import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.services import telegram_media


async def test_downloads_a_valid_telegram_image_without_storing_a_link(monkeypatch) -> None:
    monkeypatch.setattr(settings, "bot_token", "123456:TEST-BOT-TOKEN")
    image = b"\xff\xd8\xff\xd9"
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/test.jpg", "file_size": 4}},
            )
        return httpx.Response(200, content=image)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        telegram_media.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    assert await telegram_media.telegram_image("file-1", "image/jpeg", 4) == image
    assert len(seen) == 2


async def test_rejects_oversized_or_fake_image_before_agent_input(monkeypatch) -> None:
    with pytest.raises(HTTPException) as oversized:
        await telegram_media.telegram_image("file-1", "image/jpeg", 20 * 1024 * 1024 + 1)
    assert oversized.value.status_code == 413

    monkeypatch.setattr(settings, "bot_token", "123456:TEST-BOT-TOKEN")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "photos/test.jpg", "file_size": 4}},
            )
        return httpx.Response(200, content=b"nope")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        telegram_media.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(HTTPException) as fake:
        await telegram_media.telegram_image("file-1", "image/jpeg", 4)
    assert fake.value.status_code == 422
