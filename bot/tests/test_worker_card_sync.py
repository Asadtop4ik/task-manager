from unittest.mock import AsyncMock, MagicMock

from app import worker


async def test_deleted_card_is_disabled_and_acknowledged(monkeypatch) -> None:
    notice = {
        "event_id": 9,
        "kind": "deleted",
        "chat_id": 1001,
        "message_id": 7,
        "task": {"id": 42},
    }

    class FakeClient:
        def __init__(self):
            self.posts: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            response = MagicMock()
            response.json.return_value = [notice]
            return response

        async def post(self, url: str, **kwargs):
            self.posts.append(url)
            return MagicMock()

    client = FakeClient()
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(worker, "create_bot", lambda: bot)

    await worker.sync_deleted_task_cards({})

    bot.edit_message_text.assert_awaited_once_with(
        chat_id=1001, message_id=7, text="🗑 Vazifa #42 o‘chirildi.", reply_markup=None
    )
    assert client.posts[0].endswith("/card-sync/9/notified")
