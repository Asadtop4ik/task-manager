from unittest.mock import AsyncMock, MagicMock

from app.api import ApiError
from app.handlers import team


async def test_invite_start_notifies_owner_once(monkeypatch) -> None:
    message = MagicMock()
    message.chat.type = "private"
    message.text = "/start invite_validtoken"
    message.from_user.id = 8123
    message.from_user.first_name = "Ali"
    message.from_user.last_name = None
    message.from_user.username = "ali"
    message.answer = AsyncMock()
    state = MagicMock()
    state.clear = AsyncMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    api = MagicMock()
    api.join_request = AsyncMock(
        return_value={
            "id": 4,
            "telegram_id": 8123,
            "full_name": "Ali",
            "username": "ali",
            "status": "pending",
            "notify_owner": True,
            "owner_chat_id": 1001,
        }
    )
    monkeypatch.setattr(team, "api_for", lambda _: api)

    await team.start(message, state, bot)

    api.join_request.assert_awaited_once()
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 1001
    assert "Tasdiqlashni kuting" in message.answer.await_args.args[0]


async def test_plain_start_does_not_notify_owner_for_unknown_user(monkeypatch) -> None:
    message = MagicMock()
    message.chat.type = "private"
    message.text = "/start"
    message.from_user.id = 8123
    message.answer = AsyncMock()
    state = MagicMock()
    state.clear = AsyncMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    api = MagicMock()
    api.me = AsyncMock(side_effect=ApiError(401, "not authenticated"))
    monkeypatch.setattr(team, "api_for", lambda _: api)

    await team.start(message, state, bot)

    bot.send_message.assert_not_awaited()
    assert "taklif havolasini" in message.answer.await_args.args[0]


async def test_login_link_is_sent_only_in_private_chat(monkeypatch) -> None:
    message = MagicMock()
    message.chat.type = "private"
    message.answer = AsyncMock()
    api = MagicMock()
    api.magic_link = AsyncMock(return_value={"url": "https://tasks.test/login#token=abc"})
    monkeypatch.setattr(team, "api_for", lambda _: api)

    await team.login(message)
    api.magic_link.assert_awaited_once()
    assert "https://tasks.test/login#token=abc" in message.answer.await_args.args[0]

    message.chat.type = "group"
    await team.login(message)
    api.magic_link.assert_awaited_once()


async def test_approval_assigns_selected_projects_and_informs_applicant(monkeypatch) -> None:
    query = MagicMock()
    query.answer = AsyncMock()
    state = MagicMock()
    state.get_data = AsyncMock(return_value={"request_id": 4, "selected": [2, 3]})
    state.clear = AsyncMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    api = MagicMock()
    api.decide_join_request = AsyncMock(
        return_value={"id": 4, "full_name": "Ali", "telegram_id": 8123, "status": "approved"}
    )
    owner_message = MagicMock()
    owner_message.edit_text = AsyncMock()
    monkeypatch.setattr(team, "api_for", lambda _: api)
    monkeypatch.setattr(team, "editable", lambda _: owner_message)

    await team.approve(query, MagicMock(request_id=4), state, bot)

    api.decide_join_request.assert_awaited_once_with(4, "approve", [2, 3])
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 8123
