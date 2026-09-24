from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app import worker


async def test_intake_notice_ack_includes_message_id_and_revision(monkeypatch) -> None:
    notice = {
        "id": 8,
        "revision": 5,
        "status": "ready",
        "chat_id": 1001,
        "title": "Fix board",
        "brief": "Move the filter to the top.",
        "questions": [],
        "mode": "pr",
    }

    class FakeClient:
        def __init__(self):
            self.posts = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            response = MagicMock()
            response.json.return_value = [notice]
            return response

        async def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return MagicMock()

    client = FakeClient()
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=77))
    bot.session.close = AsyncMock()
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(worker, "create_bot", lambda: bot)
    monkeypatch.setattr(
        worker,
        "settings",
        SimpleNamespace(
            agent_intake_enabled=True,
            service_token="test-token",
            api_base_url="http://api",
        ),
    )

    await worker.notify_agent_intakes({})

    bot.send_message.assert_awaited_once()
    url, request = client.posts[0]
    assert url == "http://api/api/v1/agent-intakes/8/notified"
    assert request["json"] == {"message_id": 77, "revision": 5}
