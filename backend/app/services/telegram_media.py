"""Fetch a Telegram image without exposing the bot token to coding agents."""

import httpx
from fastapi import HTTPException

from app.core.config import settings

MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})


def _matches_image(data: bytes, mime: str) -> bool:
    if mime == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/webp":
        return data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


async def telegram_image(file_id: str, mime: str, expected_size: int | None) -> bytes:
    if mime not in IMAGE_MIMES or (
        expected_size is not None and expected_size > MAX_IMAGE_BYTES
    ):
        raise HTTPException(status_code=413, detail="unsupported or oversized image")
    if not settings.bot_token:
        raise HTTPException(status_code=503, detail="Telegram image access is unavailable")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            metadata = await client.get(
                f"https://api.telegram.org/bot{settings.bot_token}/getFile",
                params={"file_id": file_id},
            )
            metadata.raise_for_status()
            payload = metadata.json()
            if not payload.get("ok"):
                raise ValueError("Telegram did not return the image")
            file = payload.get("result") or {}
            path = str(file.get("file_path") or "")
            if not path or path.startswith("/") or ".." in path.split("/"):
                raise ValueError("Telegram returned an invalid image path")
            if int(file.get("file_size") or 0) > MAX_IMAGE_BYTES:
                raise HTTPException(
                    status_code=413, detail="image exceeds Telegram download limit"
                )
            chunks = bytearray()
            async with client.stream(
                "GET", f"https://api.telegram.org/file/bot{settings.bot_token}/{path}"
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_IMAGE_BYTES:
                        raise HTTPException(
                            status_code=413, detail="image exceeds Telegram download limit"
                        )
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        # The underlying exception may contain a URL with the bot token.
        raise HTTPException(status_code=502, detail="Telegram image download failed") from None
    data = bytes(chunks)
    if not _matches_image(data, mime):
        raise HTTPException(status_code=422, detail="Telegram file is not a supported image")
    return data
