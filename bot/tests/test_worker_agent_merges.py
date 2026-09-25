from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app import worker


async def test_merged_agent_run_notifies_bot_without_claiming_deploy(monkeypatch) -> None:
    notice = {
        "run_id": "run-1",
        "task_id": 17,
        "title": "<billing> migration",
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
    bot_options = {}

    def create_test_bot(**kwargs):
        bot_options.update(kwargs)
        return bot

    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(worker, "Bot", create_test_bot)
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
    assert "&lt;billing&gt; migration" in message
    assert "serverga chiqdi" not in message
    assert bot_options["default"].parse_mode == worker.ParseMode.HTML
    assert client.posts == ["http://api/api/v1/agent-runs/run-1/notified"]


def test_result_card_announces_only_verified_pr_as_ready() -> None:
    base = {
        "task_id": 18,
        "title": "Katalog yozuvini tuzatish",
        "repo_full_name": "muradjanov-dev/qurbot",
        "mode": "pr",
        "pr_url": "https://github.com/muradjanov-dev/qurbot/pull/4",
    }
    pending = worker.agent_result_card(base | {"status": "pr_opened", "ci_status": "pending"})
    assert "PR tayyor" not in pending
    ready = worker.agent_result_card(base | {"status": "pr_ready", "ci_status": "success"})
    assert "PR tayyor" in ready and "CI’dan o‘tdi" in ready
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
    assert "&lt;external source unavailable&gt;" in card
    assert "Opus &lt;5.5&gt;" in card


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


async def test_owner_release_card_has_controls_and_uses_private_owner_chat(
    monkeypatch,
) -> None:
    notice = {
        "run_id": "12345678-1234-5678-1234-567812345678",
        "task_id": 18,
        "title": "Katalog <yozuv>",
        "repo_full_name": "muradjanov-dev/qurbot",
        "status": "pr_ready",
        "mode": "pr",
        "chat_id": -1001,
        "owner_chat_id": 1001,
        "owner_controls_available": True,
        "head_sha": "c" * 40,
        "summary": "Checkout <timeout> fix",
        "impact": "Retries stay bounded",
        "ci_evidence": {
            "state": "success",
            "verified_sha": "c" * 40,
            "url": "https://github.com/muradjanov-dev/qurbot/actions/runs/8",
        },
        "review": {
            "state": "clean",
            "reviewed_head_sha": "c" * 40,
            "findings": [],
        },
        "actions": {
            "merge": {"available": True},
            "correction": {"available": True},
        },
        "pr_url": "https://github.com/muradjanov-dev/qurbot/pull/8",
        "telegram_message_id": 41,
        "owner_notice_chat_id": None,
        "owner_notice_message_id": None,
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
            self.acks.append((url, kwargs["json"]))
            return MagicMock()

    client = Client()
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=43))
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
    legacy_edit = bot.edit_message_text.await_args
    assert legacy_edit.kwargs["chat_id"] == -1001
    assert legacy_edit.kwargs["message_id"] == 41
    assert legacy_edit.kwargs["reply_markup"] is None
    owner_send = bot.send_message.await_args
    assert owner_send.args[0] == 1001
    assert "&lt;timeout&gt;" in owner_send.args[1]
    assert owner_send.kwargs["reply_markup"] is not None
    assert client.acks == [
        (
            "http://api/api/v1/agent-runs/12345678-1234-5678-1234-567812345678/notified",
            {"message_id": 41},
        ),
        (
            "http://api/api/v1/agent-runs/12345678-1234-5678-1234-567812345678/owner-notified",
            {"message_id": 43},
        ),
    ]


async def test_owner_deploy_notice_edits_same_card_and_keeps_details(
    monkeypatch,
) -> None:
    notice = {
        "run_id": "run-3",
        "task_id": 18,
        "title": "Katalog yozuvini tuzatish",
        "repo_full_name": "muradjanov-dev/qurbot",
        "status": "deployed",
        "mode": "pr",
        "chat_id": -1001,
        "owner_chat_id": 1001,
        "owner_controls_available": False,
        "head_sha": "d" * 40,
        "summary": "Updated checkout timeout",
        "impact": "Retry impact",
        "ci_evidence": {"state": "success", "verified_sha": "d" * 40},
        "review": {"state": "clean", "findings": []},
        "owner_notice_chat_id": 1001,
        "owner_notice_message_id": 43,
        "telegram_message_id": 42,
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
            self.acks.append((url, kwargs["json"]))
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

    assert bot.edit_message_text.await_count == 2
    legacy_edit, owner_edit = bot.edit_message_text.await_args_list
    assert legacy_edit.kwargs["chat_id"] == -1001
    assert legacy_edit.kwargs["message_id"] == 42
    assert legacy_edit.kwargs["reply_markup"] is None
    assert owner_edit.kwargs["chat_id"] == 1001
    assert owner_edit.kwargs["message_id"] == 43
    markup = owner_edit.kwargs["reply_markup"]
    assert [button.text for row in markup.inline_keyboard for button in row] == ["Batafsil"]
    bot.send_message.assert_not_awaited()
    assert client.acks == [
        ("http://api/api/v1/agent-runs/run-3/notified", {"message_id": 42}),
        ("http://api/api/v1/agent-runs/run-3/owner-notified", {"message_id": 43}),
    ]


async def test_owner_failure_notice_edits_existing_card_with_escaped_reason(
    monkeypatch,
) -> None:
    notice = {
        "run_id": "run-failed",
        "task_id": 19,
        "title": "Review check",
        "repo_full_name": "Asadtop4ik/task-manager",
        "status": "failed",
        "chat_id": -1001,
        "owner_chat_id": 1001,
        "owner_controls_available": False,
        "head_sha": "e" * 40,
        "summary": "Review could not complete",
        "impact": "No merge was made",
        "review": {"state": "failed", "findings": []},
        "ci_evidence": {"state": "success"},
        "error": "<review unavailable> & retry later",
        "owner_notice_chat_id": 1001,
        "owner_notice_message_id": 55,
        "telegram_message_id": 45,
    }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            response = MagicMock()
            response.json.return_value = [notice]
            return response

        async def post(self, url, **kwargs):
            return MagicMock()

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(worker, "Bot", lambda **kwargs: bot)
    monkeypatch.setattr(
        worker,
        "settings",
        SimpleNamespace(
            service_token="test", api_base_url="http://api", bot_token="123456:TEST"
        ),
    )

    await worker.notify_agent_runs({})

    assert bot.edit_message_text.await_count == 2
    legacy_edit, owner_edit = bot.edit_message_text.await_args_list
    assert legacy_edit.kwargs["chat_id"] == -1001
    assert legacy_edit.kwargs["message_id"] == 45
    assert owner_edit.kwargs["chat_id"] == 1001
    assert owner_edit.kwargs["message_id"] == 55
    assert "&lt;review unavailable&gt; &amp; retry later" in owner_edit.args[0]
    assert [
        button.text
        for row in owner_edit.kwargs["reply_markup"].inline_keyboard
        for button in row
    ] == ["Batafsil"]
    bot.send_message.assert_not_awaited()
