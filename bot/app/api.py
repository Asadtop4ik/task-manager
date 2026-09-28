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

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        code: str | None = None,
        current_head_sha: str | None = None,
    ) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.code = code
        self.current_head_sha = current_head_sha

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
                payload = response.json()
                error = payload.get("error")
                if isinstance(error, dict):
                    detail = str(error.get("message") or error.get("code") or response.text)
                    code = str(error.get("code")) if error.get("code") else None
                    current_head_sha = error.get("current_head_sha")
                else:
                    detail = str(payload.get("detail", response.text))
                    code = None
                    current_head_sha = None
            except ValueError:
                detail = response.text
                code = None
                current_head_sha = None
            log.info("api_error", method=method, path=path, status=response.status_code)
            raise ApiError(
                response.status_code,
                detail,
                code=code,
                current_head_sha=current_head_sha,
            )

        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # --- identity -------------------------------------------------------

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "/auth/me")

    async def users(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/users")

    async def create_invite(self) -> dict[str, Any]:
        return await self._request("POST", "/team/invites")

    async def join_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/team/join-requests", json=payload)

    async def pending_join_requests(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/team/join-requests/pending")

    async def decide_join_request(
        self, request_id: int, decision: str, project_ids: list[int] | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/team/join-requests/{request_id}/{decision}",
            json={"project_ids": project_ids} if project_ids is not None else None,
        )

    async def magic_link(self) -> dict[str, Any]:
        return await self._request("POST", "/auth/magic/request")

    # --- projects -------------------------------------------------------

    async def projects(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/projects")

    # --- tasks ----------------------------------------------------------

    async def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/tasks", json=payload)

    async def task(self, task_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/tasks/{task_id}")

    async def start_agent_run(self, task_id: int, mode: str = "pr") -> dict[str, Any]:
        return await self._request("POST", f"/agent-runs/tasks/{task_id}", json={"mode": mode})

    async def agent_runs(self, task_id: int) -> list[dict[str, Any]]:
        return await self._request("GET", f"/agent-runs/tasks/{task_id}")

    async def cancel_agent_run(self, run_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/agent-runs/{run_id}/cancel")

    async def agent_run(self, run_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/agent-runs/{run_id}")

    async def merge_agent_run(
        self, run_id: str, *, expected_head_sha: str, action_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/agent-runs/{run_id}/merge",
            json={"expected_head_sha": expected_head_sha, "action_id": action_id},
        )

    async def request_agent_correction(
        self,
        run_id: str,
        *,
        instruction: str,
        expected_head_sha: str,
        action_id: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/agent-runs/{run_id}/corrections",
            json={
                "instruction": instruction,
                "expected_head_sha": expected_head_sha,
                "action_id": action_id,
            },
        )

    async def agent_run_for_ops(self, ops_id: int) -> dict[str, Any]:
        """The run detail owning one ops request — callback data has no room for run_id.

        ASSUMPTION: relies on `GET /agent-ops/{ops_id}` (OwnerUser), returning the
        same `AgentRunDetailOut` shape as `agent_run`. Not one of the 3 endpoints
        the spec enumerates for the ops router; see the WP-B report for why the
        64-byte callback cap makes some such lookup unavoidable.
        """
        return await self._request("GET", f"/agent-ops/{ops_id}")

    async def decide_ops_request(
        self,
        ops_id: int,
        *,
        decision: str,
        request_hash: str,
        action_id: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/agent-ops/{ops_id}/decision",
            json={
                "decision": decision,
                "request_hash": request_hash,
                "action_id": action_id,
            },
        )

    # --- agent intake ---------------------------------------------------

    async def create_agent_intake(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/agent-intakes", json=payload)

    async def current_agent_intake(self) -> dict[str, Any] | None:
        return await self._request("GET", "/agent-intakes/current")

    async def answer_agent_intake(self, intake_id: int, text: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/agent-intakes/{intake_id}/answer", json={"text": text}
        )

    async def revise_agent_intake(self, intake_id: int, text: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/agent-intakes/{intake_id}/revise", json={"text": text}
        )

    async def confirm_agent_intake(
        self, intake_id: int, *, fallback_pr: bool = False
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/agent-intakes/{intake_id}/confirm",
            json={"fallback_pr": fallback_pr},
        )

    async def cancel_agent_intake(self, intake_id: int) -> dict[str, Any] | None:
        return await self._request("POST", f"/agent-intakes/{intake_id}/cancel")

    async def retry_agent_intake(self, intake_id: int) -> dict[str, Any]:
        return await self._request("POST", f"/agent-intakes/{intake_id}/retry")

    # --- project discussion -------------------------------------------

    async def start_discussion(self, project_id: int, chat_id: int) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/project-discussions",
            json={"project_id": project_id, "chat_id": chat_id},
        )

    async def discussion(self, discussion_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/project-discussions/{discussion_id}")

    async def discussion_message(
        self, discussion_id: int, text: str, images: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/project-discussions/{discussion_id}/messages",
            json={"text": text, "images": images or []},
        )

    async def reset_discussion(self, discussion_id: int) -> dict[str, Any]:
        return await self._request("POST", f"/project-discussions/{discussion_id}/reset")

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
