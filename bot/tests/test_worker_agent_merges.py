from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app import worker


async def test_merged_agent_run_notifies_bot_without_claiming_deploy(monkeypatch) -> None:
    notice = {
        "run_id": "run-1",
        "task_id": 17,
        "status": "merged",
        "chat_id": 1001,
        "pr_url": "https://github.com/muradjanov-dev/qurbot/pull/7",
        "github_run_url": None,
        "error": None,
        "mode": "pr",
    }

    class Client:
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
            self.posts.append(url)
            return MagicMock()

    client = Client()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(worker, "Bot", lambda **kwargs: bot)
    monkeypatch.setattr(
        worker,
        "settings",
        SimpleNamespace(
            service_token="test", api_base_url="http://api", bot_token="123456:TEST"
        ),
    )

    await worker.notify_agent_runs({})

    message = bot.send_message.await_args.args[1]
    assert "PR birlashtirildi" in message
    assert "serverga chiqdi" not in message
    assert client.posts == ["http://api/api/v1/agent-runs/run-1/notified"]
