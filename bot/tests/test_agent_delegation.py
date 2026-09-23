from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers import quick


@pytest.mark.parametrize("role,expected_assignee", [("manager", None), ("executor", 7)])
async def test_one_line_codex_task_keeps_a_human_owner(
    monkeypatch, role: str, expected_assignee: int | None
) -> None:
    state = MagicMock()
    state.get_data = AsyncMock(
        return_value={
            "project_id": 4,
            "draft": {
                "title": "Fix menu",
                "assignee_username": "codex",
                "priority": None,
                "due_at": None,
            },
        }
    )
    state.clear = AsyncMock()
    query = MagicMock()
    query.message.chat.id = 123
    query.from_user.id = 456
    query.answer = AsyncMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    api = MagicMock()
    api.me = AsyncMock(return_value={"id": 7, "role": role})
    api.create_task = AsyncMock(return_value={"id": 42, "status": "todo"})
    api.start_agent_run = AsyncMock(return_value={"status": "dispatched"})
    prompt = MagicMock()
    prompt.delete = AsyncMock()
    monkeypatch.setattr(quick, "api_for", lambda _: api)
    monkeypatch.setattr(quick, "editable", lambda _: prompt)
    monkeypatch.setattr(quick, "send_card", AsyncMock())
    monkeypatch.setattr(quick, "notify_assignee", AsyncMock())

    await quick.create(query, state, bot)

    payload = api.create_task.await_args.args[0]
    assert payload.get("assignee_id") == expected_assignee
    api.start_agent_run.assert_awaited_once_with(42)
    bot.send_message.assert_awaited_once()


async def test_stopagent_cancels_latest_active_run(monkeypatch) -> None:
    message = MagicMock()
    message.text = "/stopagent 42"
    message.answer = AsyncMock()
    api = MagicMock()
    api.agent_runs = AsyncMock(
        return_value=[
            {"run_id": "active", "status": "running"},
            {"run_id": "older", "status": "failed"},
        ]
    )
    api.cancel_agent_run = AsyncMock(return_value={"status": "cancelled"})
    monkeypatch.setattr(quick, "api_for", lambda _: api)

    await quick.stop_agent(message)

    api.cancel_agent_run.assert_awaited_once_with("active")
    message.answer.assert_awaited_once()
