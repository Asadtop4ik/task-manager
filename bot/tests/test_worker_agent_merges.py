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
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
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


def test_result_card_distinguishes_unverified_pr_from_deployed_sha() -> None:
    base = {
        "task_id": 18,
        "title": "Katalog yozuvini tuzatish",
        "repo_full_name": "muradjanov-dev/qurbot",
        "mode": "pr",
        "pr_url": "https://github.com/muradjanov-dev/qurbot/pull/4",
    }
    ready = worker.agent_result_card(base | {"status": "pr_ready"})
    assert "CI natijasini PR sahifasida" in ready
    assert "Production’da" not in ready

    deployed = worker.agent_result_card(
        base
        | {
            "status": "deployed",
            "deployed_sha": "a" * 40,
            "github_run_url": "https://github.com/muradjanov-dev/qurbot/actions/runs/1",
        }
    )
    assert "Production’da" in deployed
    assert "a" * 12 in deployed


def test_failure_card_keeps_plain_text_diagnostic_reason() -> None:
    card = worker.agent_result_card(
        {
            "task_id": 27,
            "title": "Opus <5.5>",
            "repo_full_name": "muradjanov-dev/qurbot",
            "status": "failed",
            "error": "agent produced no file changes\nCodex izohi: <external source unavailable>",
        }
    )
    assert "agent produced no file changes" in card
    assert "Codex izohi" in card
    assert "<external source unavailable>" in card
    assert "Opus <5.5>" in card


async def test_deploy_updates_existing_result_card(monkeypatch) -> None:
    notice = {
        "run_id": "run-2",
        "task_id": 18,
        "title": "Katalog yozuvini tuzatish",
        "repo_full_name": "muradjanov-dev/qurbot",
        "status": "deployed",
        "mode": "pr",
        "chat_id": 1001,
        "telegram_message_id": 42,
        "deployed_sha": "b" * 40,
        "github_run_url": "https://github.com/muradjanov-dev/qurbot/actions/runs/7",
    }

    class Client:
        def __init__(self):
            self.acks = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            response = MagicMock()
            response.json.return_value = [notice]
            return response

        async def post(self, url, **kwargs):
            self.acks.append(kwargs["json"])
            return MagicMock()

    client = Client()
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
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

    bot.edit_message_text.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    assert client.acks == [{"message_id": 42}]
