from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

from app.callbacks import AgentReleaseAction
from app.handlers import agent_release


def _query(*, actor_id=1001, chat_type="private"):
    message = SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, id=1001),
        message_id=77,
        edit_text=AsyncMock(),
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=actor_id),
        message=message,
        answer=AsyncMock(),
    )


def _state():
    return SimpleNamespace(
        set_state=AsyncMock(),
        update_data=AsyncMock(),
    )


def _run(*, head_sha="a" * 40, merge=True, correction=True):
    return {
        "run_id": "12345678-1234-5678-1234-567812345678",
        "status": "ready",
        "summary": "Update checkout timeout",
        "impact": "Checkout retries remain bounded",
        "head_sha": head_sha,
        "ci": {"state": "success", "checks": [{"name": "CI", "state": "success"}]},
        "review": {"state": "clean", "findings": []},
        "actions": {
            "merge": {"available": merge},
            "correction": {"available": correction},
        },
    }


def test_release_callback_data_stays_under_telegram_limit() -> None:
    keyboard = agent_release.release_keyboard("12345678-1234-5678-1234-567812345678", "a" * 40)
    assert keyboard is not None
    assert all(
        len(button.callback_data.encode()) <= 64
        for row in keyboard.inline_keyboard
        for button in row
    )


def test_release_card_escapes_untrusted_fields_and_stays_within_telegram_limit() -> None:
    card = agent_release.release_card(
        _run()
        | {
            "summary": "<script> & " + ("long " * 1500),
            "review": {
                "state": "clean",
                "findings": [
                    {"severity": "P1", "title": "<unexpected> & issue", "file": "bot/<x>.py"}
                ],
            },
        }
    )
    assert "&lt;script&gt; &amp;" in card
    assert "&lt;unexpected&gt; &amp; issue" in card
    assert len(card) <= agent_release.CARD_LIMIT


def test_review_block_shows_concise_finding_and_only_available_correction() -> None:
    run = _run(merge=False, correction=True)
    run["status"] = "pr_ready"
    run["review"] = {
        "state": "changes_requested",
        "findings": [
            {
                "severity": "P1",
                "title": "Checkout retries ignore the configured cap",
                "file": "src/checkout.py",
                "line": 82,
            }
        ],
    }

    card = agent_release.release_card(run)
    keyboard = agent_release.release_keyboard(
        run["run_id"], run["head_sha"], actions=run["actions"]
    )
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "PR tayyor" not in card
    assert "Tuzatish kerak" in card
    assert "P1" in card and "retries ignore the configured cap" in card
    assert labels == ["Tuzatish so‘rash", "Batafsil"]


def test_details_remain_available_when_release_actions_are_gated() -> None:
    keyboard = agent_release.release_keyboard(
        "12345678-1234-5678-1234-567812345678",
        "f" * 40,
        actions={
            "merge": {"available": False},
            "correction": {"available": False},
        },
    )

    assert keyboard is not None
    assert [button.text for row in keyboard.inline_keyboard for button in row] == ["Batafsil"]


def test_release_card_displays_backend_verified_head_sha_field() -> None:
    run = _run()
    run["ci_evidence"] = {
        "state": "success",
        "verified_head_sha": "a" * 40,
        "url": "https://github.com/Asadtop4ik/task-manager/actions/runs/123",
    }

    card = agent_release.release_card(run)

    assert f"CI SHA: <code>{'a' * 12}</code>" in card
    assert "CI natijasini ochish" in card


def test_qa_deploy_failure_card_shows_safe_reason_and_workflow_link() -> None:
    run = _run() | {
        "repo_full_name": agent_release.QA_REPOSITORY,
        "status": "merged",
        "merged_sha": "b" * 40,
        "error": "QA deployment failed [readiness_failed]: /ready returned 503 <body>",
        "github_run_url": "https://github.com/Asadtop4ik/agent-qa/actions/runs/42",
    }

    card = agent_release.release_card(run)

    assert "QA deploy xato" in card
    assert "PR tayyor" not in card
    assert "QA merge SHA: <code>" + "b" * 40 + "</code>" in card
    assert "QA /ready tekshiruvi o‘tmadi: /ready returned 503 &lt;body&gt;" in card
    assert "QA Actions natijasini ochish" in card


def test_qa_deployed_card_shows_exact_image_and_readiness_evidence_without_old_error() -> None:
    run = _run() | {
        "repo_full_name": agent_release.QA_REPOSITORY,
        "status": "deployed",
        "merged_sha": "b" * 40,
        "deployed_sha": "c" * 40,
        "qa_ready_sha": "c" * 40,
        "qa_ready_url": "http://127.0.0.1:18082/ready",
        "github_run_url": "https://github.com/Asadtop4ik/agent-qa/actions/runs/43",
        "error": "stale failure must not survive a successful deploy",
    }

    card = agent_release.release_card(run)

    assert "QA deploy tayyor" in card
    assert "QA merge SHA: <code>" + "b" * 40 + "</code>" in card
    assert "QA image SHA: <code>" + "c" * 40 + "</code>" in card
    assert "QA /ready SHA: <code>" + "c" * 40 + "</code>" in card
    assert "http://127.0.0.1:18082/ready" in card
    assert "QA Actions natijasini ochish" in card
    assert "Sabab:" not in card
    assert "stale failure" not in card


def test_non_qa_run_does_not_use_qa_deploy_failure_label() -> None:
    card = agent_release.release_card(
        _run()
        | {
            "repo_full_name": "muradjanov-dev/qurbot",
            "status": "merged",
            "error": "QA deployment failed [deploy_failed]: test only",
        }
    )

    assert "QA deploy xato" not in card


async def test_stale_callback_refetches_and_never_merges(monkeypatch) -> None:
    api = MagicMock()
    api.me = AsyncMock(return_value={"is_owner": True})
    api.agent_run = AsyncMock(return_value=_run(head_sha="b" * 40))
    api.merge_agent_run = AsyncMock()
    monkeypatch.setattr(agent_release, "api_for", lambda _: api)
    query = _query()

    await agent_release.release_action(
        query,
        AgentReleaseAction(
            action="merge",
            run_id="12345678-1234-5678-1234-567812345678",
            sha12="a" * 12,
        ),
        _state(),
    )

    api.agent_run.assert_awaited_once()
    api.merge_agent_run.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True
    query.message.edit_text.assert_awaited_once()


async def test_non_owner_callback_cannot_fetch_or_mutate_run(monkeypatch) -> None:
    api = MagicMock()
    api.me = AsyncMock(return_value={"is_owner": False})
    api.agent_run = AsyncMock()
    api.merge_agent_run = AsyncMock()
    monkeypatch.setattr(agent_release, "api_for", lambda _: api)
    query = _query(actor_id=2002)

    await agent_release.release_action(
        query,
        AgentReleaseAction(
            action="merge",
            run_id="12345678-1234-5678-1234-567812345678",
            sha12="a" * 12,
        ),
        _state(),
    )

    api.agent_run.assert_not_awaited()
    api.merge_agent_run.assert_not_awaited()
    query.answer.assert_awaited_once_with("Bu amal faqat egasi uchun.", show_alert=True)


async def test_repeated_merge_clicks_reuse_same_action_id(monkeypatch) -> None:
    run = _run(correction=False)
    api = MagicMock()
    api.me = AsyncMock(return_value={"is_owner": True})
    api.agent_run = AsyncMock(return_value=run)
    api.merge_agent_run = AsyncMock(return_value={"message": "Qabul qilindi"})
    monkeypatch.setattr(agent_release, "api_for", lambda _: api)
    callback = AgentReleaseAction(
        action="merge",
        run_id=run["run_id"],
        sha12=run["head_sha"][:12],
    )

    await agent_release.release_action(_query(), callback, _state())
    await agent_release.release_action(_query(), callback, _state())

    assert api.merge_agent_run.await_count == 2
    ids = [call.kwargs["action_id"] for call in api.merge_agent_run.await_args_list]
    assert ids[0] == ids[1]
    UUID(ids[0])


async def test_correction_submit_uses_full_sha_and_edits_card(monkeypatch) -> None:
    run = _run(merge=False, correction=True)
    api = MagicMock()
    api.me = AsyncMock(return_value={"is_owner": True})
    api.agent_run = AsyncMock(return_value=run)
    api.request_agent_correction = AsyncMock(
        return_value={"message": "Tuzatish navbatga qo‘shildi"}
    )
    monkeypatch.setattr(agent_release, "api_for", lambda _: api)
    state = MagicMock()
    state.get_data = AsyncMock(
        return_value={
            "correction_run_id": run["run_id"],
            "correction_head_sha": run["head_sha"],
            "correction_chat_id": 1001,
            "correction_message_id": 77,
        }
    )
    state.clear = AsyncMock()
    message = SimpleNamespace(
        text="Add an integration test",
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=1001),
        answer=AsyncMock(),
    )
    bot = MagicMock()
    bot.edit_message_text = AsyncMock()

    await agent_release.submit_correction(message, state, bot)

    api.request_agent_correction.assert_awaited_once()
    payload = api.request_agent_correction.await_args.kwargs
    assert payload["expected_head_sha"] == "a" * 40
    UUID(payload["action_id"])
    bot.edit_message_text.assert_awaited_once()
    state.clear.assert_awaited_once()


async def test_correction_api_failure_keeps_fsm_for_retry(monkeypatch) -> None:
    from app.api import ApiError

    run = _run(merge=False, correction=True)
    api = MagicMock()
    api.me = AsyncMock(return_value={"is_owner": True})
    api.agent_run = AsyncMock(return_value=run)
    api.request_agent_correction = AsyncMock(side_effect=ApiError(503, "agent unavailable"))
    monkeypatch.setattr(agent_release, "api_for", lambda _: api)
    state = MagicMock()
    state.get_data = AsyncMock(
        return_value={
            "correction_run_id": run["run_id"],
            "correction_head_sha": run["head_sha"],
            "correction_chat_id": 1001,
            "correction_message_id": 77,
        }
    )
    state.clear = AsyncMock()
    message = SimpleNamespace(
        text="Add a test",
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=1001),
        answer=AsyncMock(),
    )

    await agent_release.submit_correction(message, state, MagicMock())

    state.clear.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert "agent unavailable" in message.answer.await_args.args[0]
