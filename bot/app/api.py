"""Typed-enough client for the task API.

The bot never touches Postgres. It calls the API as a first-party client with
the service token, naming the Telegram user it is acting for, so permissions and
attribution are decided in exactly one place rather than duplicated here.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)


class ApiError(Exception):
    """A request the API refused. `status` is what it said."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail

    @property
    def is_pending_approval(self) -> bool:
        return self.status == 403 and "approved" in self.detail

    @property
    def is_unknown_user(self) -> bool:
        # The service token was fine but that telegram_id has never logged in.
        return self.status == 401


class TaskApi:
    def __init__(self, telegram_id: int) -> None:
        self._telegram_id = telegram_id

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {
            "X-Service-Token": settings.service_token,
            "X-Acting-User": str(self._telegram_id),
        }
        url = f"{settings.api_base_url.rstrip('/')}/api/v1{path}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.request(method, url, headers=headers, **kwargs)

        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail", response.text))
            except ValueError:
                detail = response.text
            log.info("api_error", method=method, path=path, status=response.status_code)
            raise ApiError(response.status_code, detail)

        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # --- identity -------------------------------------------------------

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "/auth/me")

    async def users(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/users")

    # --- projects -------------------------------------------------------

    async def projects(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/projects")

    # --- tasks ----------------------------------------------------------

    async def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/tasks", json=payload)

    async def task(self, task_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/tasks/{task_id}")

    async def start_agent_run(self, task_id: int) -> dict[str, Any]:
        return await self._request("POST", f"/agent-runs/tasks/{task_id}")

    async def agent_runs(self, task_id: int) -> list[dict[str, Any]]:
        return await self._request("GET", f"/agent-runs/tasks/{task_id}")

    async def tasks(self, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        return await self._request("GET", "/tasks", params=clean)

    async def transition(self, task_id: int, status: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/tasks/{task_id}/transition", json={"status": status}
        )

    async def comment(self, task_id: int, body: str) -> dict[str, Any]:
        return await self._request("POST", f"/tasks/{task_id}/comments", json={"body": body})

    async def set_due(self, task_id: int, due_at: str | None) -> dict[str, Any]:
        return await self._request("PATCH", f"/tasks/{task_id}", json={"due_at": due_at})

    async def set_card(self, task_id: int, chat_id: int, message_id: int) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/tasks/{task_id}/card",
            json={"chat_id": chat_id, "message_id": message_id},
        )
