from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.handlers import agent_intake
from app.states import AgentIntake


def _message(*, chat_type="private", chat_id=101, text=None):
    message = MagicMock()
    message.chat.type = chat_type
    message.chat.id = chat_id
    message.text = text
    message.from_user.id = 202
    message.answer = AsyncMock()
    return message


def _state(data=None, state_name=None):
    state = MagicMock()
    state.get_data = AsyncMock(return_value=data or {})
    state.get_state = AsyncMock(return_value=state_name)
    state.clear = AsyncMock()
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()
    return state


def _api(monkeypatch, *, current=None, projects=None):
    api = MagicMock()
    api.current_agent_intake = AsyncMock(return_value=current)
    api.projects = AsyncMock(
        return_value=projects or [{"id": 7, "key": "task-manager", "name": "Task Manager"}]
    )
    api.create_agent_intake = AsyncMock(return_value={"id": 9, "status": "queued"})
    api.create_task = AsyncMock()
    api.answer_agent_intake = AsyncMock()
    api.revise_agent_intake = AsyncMock()
    api.me = AsyncMock(return_value={"is_owner": True})
    monkeypatch.setattr(agent_intake, "api_for", lambda _: api)
    monkeypatch.setattr(agent_intake, "settings", SimpleNamespace(agent_intake_enabled=True))
    return api


async def test_private_codex_text_creates_intake_without_creating_task(monkeypatch) -> None:
    api = _api(monkeypatch)
    message = _message(text="task-manager: @codex fix the board")
    state = _state()

    handled = await agent_intake.route_intake_text(
        message, state, "task-manager: @codex fix the board"
    )

    assert handled is True
    api.create_agent_intake.assert_awaited_once_with(
        {
            "project_id": 7,
            "text": "task-manager: @codex fix the board",
            "mode": "pr",
            "chat_id": 101,
            "images": [],
        }
    )
    api.create_task.assert_not_awaited()
    state.clear.assert_awaited_once()


async def test_private_fast_intake_requires_owner(monkeypatch) -> None:
    api = _api(monkeypatch)
    api.me = AsyncMock(return_value={"is_owner": False})
    message = _message(text="task-manager: fix the board !fast")
    state = _state()

    handled = await agent_intake.route_intake_text(
        message, state, "task-manager: fix the board !fast"
    )

    assert handled is True
    api.create_agent_intake.assert_not_awaited()
    message.answer.assert_awaited_once()


async def test_group_codex_text_stays_in_existing_quick_flow(monkeypatch) -> None:
    _api(monkeypatch)
    message = _message(chat_type="group", text="@codex fix the board")

    handled = await agent_intake.route_intake_text(message, _state(), message.text)

    assert handled is False


async def test_photo_then_text_submits_image_and_text_together(monkeypatch) -> None:
    api = _api(monkeypatch)
    created_at = datetime.now(UTC).timestamp()
    images = [{"file_id": "tg-photo-id", "mime": "image/jpeg", "size": 1200}]
    message = _message(text="task-manager: @codex fix this screen")
    state = _state(
        {
            "photo_draft_created_at": created_at,
            "photo_draft_images": images,
            "photo_draft_text_parts": ["Please use this screenshot"],
        },
        AgentIntake.photo_pending.state,
    )

    await agent_intake.route_intake_text(
        message, state, "task-manager: @codex fix this screen"
    )

    payload = api.create_agent_intake.await_args.args[0]
    assert (
        payload["text"] == "Please use this screenshot\ntask-manager: @codex fix this screen"
    )
    assert payload["images"] == images
    assert payload["mode"] == "pr"


async def test_photo_draft_expires_after_ten_minutes(monkeypatch) -> None:
    _api(monkeypatch)
    message = _message(text="ordinary note")
    state = _state(
        {
            "photo_draft_created_at": (
                datetime.now(UTC) - timedelta(seconds=agent_intake.PHOTO_DRAFT_SECONDS + 1)
            ).timestamp(),
            "photo_draft_images": [{"file_id": "old"}],
        }
    )

    handled = await agent_intake.route_intake_text(message, state, "ordinary note")

    assert handled is False
    state.clear.assert_awaited_once()
    assert "10 daqiqalik" in message.answer.await_args.args[0]


async def test_private_photo_starts_draft_and_group_photo_is_ignored(monkeypatch) -> None:
    monkeypatch.setattr(agent_intake, "settings", SimpleNamespace(agent_intake_enabled=True))
    image_message = _message()
    image_message.photo = [SimpleNamespace(file_id="photo", file_size=1500)]
    image_message.document = None
    image_message.caption = None
    image_message.media_group_id = None
    state = _state()

    await agent_intake.receive_image(image_message, state)

    state.set_state.assert_awaited_once_with(AgentIntake.photo_pending)
    update = state.update_data.await_args.kwargs
    assert update["photo_draft_images"] == [
        {"file_id": "photo", "mime": "image/jpeg", "size": 1500}
    ]
    assert update["photo_draft_text_parts"] == []
    assert "10 daqiqa" in image_message.answer.await_args.args[0]

    group_message = _message(chat_type="group")
    group_message.photo = [SimpleNamespace(file_id="group", file_size=1500)]
    group_state = _state()
    await agent_intake.receive_image(group_message, group_state)
    group_state.get_data.assert_not_awaited()
    group_message.answer.assert_not_awaited()


async def test_photo_caption_creates_an_intake_with_the_image(monkeypatch) -> None:
    api = _api(monkeypatch)
    image_message = _message()
    image_message.photo = [SimpleNamespace(file_id="photo", file_size=1500)]
    image_message.document = None
    image_message.caption = "task-manager: @codex fix this screen"
    image_message.media_group_id = None
    state = _state()

    await agent_intake.receive_image(image_message, state)

    api.create_agent_intake.assert_awaited_once_with(
        {
            "project_id": 7,
            "text": image_message.caption,
            "mode": "pr",
            "chat_id": 101,
            "images": [{"file_id": "photo", "mime": "image/jpeg", "size": 1500}],
        }
    )
    state.clear.assert_awaited_once()


async def test_fourth_image_is_rejected_without_dropping_existing_draft(monkeypatch) -> None:
    monkeypatch.setattr(agent_intake, "settings", SimpleNamespace(agent_intake_enabled=True))
    now = datetime.now(UTC).timestamp()
    state = _state(
        {
            "photo_draft_created_at": now,
            "photo_draft_images": [
                {"file_id": str(index), "mime": "image/jpeg", "size": 100}
                for index in range(3)
            ],
        }
    )
    message = _message()
    message.photo = [SimpleNamespace(file_id="fourth", file_size=100)]
    message.document = None
    message.caption = None
    message.media_group_id = "album"

    await agent_intake.receive_image(message, state)

    assert "ko‘pi bilan 3 ta" in message.answer.await_args.args[0]
    state.update_data.assert_not_awaited()


def test_directive_match_does_not_trigger_on_mention_prefixes() -> None:
    assert agent_intake._has_agent_directive("@codex fix it")
    assert agent_intake._has_agent_directive("task !FAST")
    assert not agent_intake._has_agent_directive("@codexchange fix it")
    assert not agent_intake._has_agent_directive("read !fastly logs")


async def test_needs_answers_routes_user_text_as_answer(monkeypatch) -> None:
    api = _api(monkeypatch, current={"id": 33, "status": "needs_answers"})
    message = _message(text="Only private users can see it")

    handled = await agent_intake.route_intake_text(
        message, _state(), "Only private users can see it"
    )

    assert handled is True
    api.answer_agent_intake.assert_awaited_once_with(33, "Only private users can see it")
    api.create_agent_intake.assert_not_awaited()
    api.create_task.assert_not_awaited()


async def test_ready_intake_routes_unprompted_text_as_revision(monkeypatch) -> None:
    api = _api(monkeypatch, current={"id": 33, "status": "ready"})
    message = _message(text="Also handle the mobile layout")

    handled = await agent_intake.route_intake_text(
        message, _state(), "Also handle the mobile layout"
    )

    assert handled is True
    api.revise_agent_intake.assert_awaited_once_with(33, "Also handle the mobile layout")
    api.create_agent_intake.assert_not_awaited()


def test_image_validation_enforces_types_size_and_count_boundary() -> None:
    photo_message = MagicMock()
    photo_message.photo = [
        SimpleNamespace(file_id="photo", file_size=agent_intake.MAX_IMAGE_BYTES)
    ]
    photo_message.document = None

    image, error = agent_intake._image_payload(photo_message)

    assert error is None
    assert image == {
        "file_id": "photo",
        "mime": "image/jpeg",
        "size": agent_intake.MAX_IMAGE_BYTES,
    }

    too_large = MagicMock()
    too_large.photo = [
        SimpleNamespace(file_id="large", file_size=agent_intake.MAX_IMAGE_BYTES + 1)
    ]
    too_large.document = None
    _, error = agent_intake._image_payload(too_large)
    assert error and "20 MB" in error

    unsupported = MagicMock()
    unsupported.photo = []
    unsupported.document = SimpleNamespace(
        file_id="pdf", file_size=200, mime_type="application/pdf", file_name="image.pdf"
    )
    _, error = agent_intake._image_payload(unsupported)
    assert error and "PNG, JPEG yoki WebP" in error


async def test_duplicate_confirm_does_not_send_card_or_start_another_run(monkeypatch) -> None:
    api = MagicMock()
    api.confirm_agent_intake = AsyncMock(
        return_value={"task": {"id": 42}, "mode": "pr", "created": False}
    )
    api.start_agent_run = AsyncMock()
    monkeypatch.setattr(agent_intake, "api_for", lambda _: api)
    query = MagicMock()
    query.from_user.id = 202
    query.message.chat.id = 101
    query.answer = AsyncMock()
    prompt = MagicMock()
    prompt.edit_text = AsyncMock()
    monkeypatch.setattr(agent_intake, "editable", lambda _: prompt)
    monkeypatch.setattr(agent_intake, "send_card", AsyncMock())
    state = _state()
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await agent_intake._confirm(query, 9, state, bot, fallback_pr=False)

    agent_intake.send_card.assert_not_awaited()
    api.start_agent_run.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    prompt.edit_text.assert_awaited_once_with("Task oldin yaratilgan.")


async def test_confirm_creates_card_and_starts_only_the_returned_mode(monkeypatch) -> None:
    task = {"id": 42, "status": "todo"}
    api = MagicMock()
    api.confirm_agent_intake = AsyncMock(
        return_value={"task": task, "mode": "fast", "created": True}
    )
    api.start_agent_run = AsyncMock(return_value={"status": "dispatched"})
    monkeypatch.setattr(agent_intake, "api_for", lambda _: api)
    query = MagicMock()
    query.from_user.id = 202
    query.message.chat.id = 101
    query.answer = AsyncMock()
    prompt = MagicMock()
    prompt.delete = AsyncMock()
    monkeypatch.setattr(agent_intake, "editable", lambda _: prompt)
    send_card = AsyncMock()
    notify_assignee = AsyncMock()
    monkeypatch.setattr(agent_intake, "send_card", send_card)
    monkeypatch.setattr(agent_intake, "notify_assignee", notify_assignee)
    state = _state()
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await agent_intake._confirm(query, 9, state, bot, fallback_pr=False)

    api.confirm_agent_intake.assert_awaited_once_with(9, fallback_pr=False)
    send_card.assert_awaited_once_with(bot, 101, task, api)
    notify_assignee.assert_awaited_once_with(bot, task, 202, api)
    api.start_agent_run.assert_awaited_once_with(42, mode="fast")
    bot.send_message.assert_awaited_once()
