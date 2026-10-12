"""Acceptance-check line on the release card and the owner's SPEC_CONFLICT re-run."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import worker
from app.api import ApiError
from app.callbacks import SpecRerunAction
from app.handlers import agent_release, agent_spec
from tests.test_agent_ops import _install_worker_fakes, _markup_labels

_CONFLICT = "SPEC_CONFLICT: brief says <b>10 items</b> but acceptance says 20"


def _query(*, actor_id=1001, chat_type="private"):
    message = SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, id=1001),
        message_id=77,
        edit_text=AsyncMock(),
        edit_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=actor_id), message=message, answer=AsyncMock()
    )


def _release_run(**overrides):
    run = {
        "run_id": "12345678-1234-5678-1234-567812345678",
        "status": "pr_ready",
        "summary": "Update checkout timeout",
        "impact": "Retries stay bounded",
        "head_sha": "a" * 40,
        "ci_evidence": {"state": "success"},
        "review": {"state": "clean", "findings": []},
        "actions": {"merge": {"available": True}, "correction": {"available": True}},
    }
    run.update(overrides)
    return run


def _gate(**overrides):
    gate = {"status": "passed", "total": 3, "passed": 3, "fixups": 0, "failed": []}
    gate.update(overrides)
    return gate


# --- release_card line -------------------------------------------------------


def test_card_shows_passing_acceptance_line() -> None:
    card = agent_release.release_card(_release_run(check_gate=_gate()))
    assert "Qabul tekshiruvi: 3/3 ✅" in card


def test_card_shows_failing_acceptance_line() -> None:
    card = agent_release.release_card(
        _release_run(check_gate=_gate(status="failed", passed=1, failed=["two", "three"]))
    )
    assert "Qabul tekshiruvi: 1/3 ❌" in card


@pytest.mark.parametrize("status", ["error", "unavailable"])
def test_card_says_checks_did_not_run_for_error_or_unavailable(status: str) -> None:
    card = agent_release.release_card(
        _release_run(check_gate=_gate(status=status, total=0, passed=0))
    )
    assert "Qabul tekshiruvi: o'tkazilmadi" in card


@pytest.mark.parametrize(
    "gate",
    [None, _gate(status="skipped", total=0, passed=0), _gate(total=0, passed=0), "junk"],
)
def test_card_omits_the_line_without_a_usable_gate(gate) -> None:
    card = agent_release.release_card(_release_run(check_gate=gate))
    assert "Qabul tekshiruvi" not in card


def test_card_without_check_gate_key_is_unchanged() -> None:
    assert "Qabul tekshiruvi" not in agent_release.release_card(_release_run())


# --- spec conflict card + keyboard ------------------------------------------


def _failed_notice(**overrides):
    notice = {
        "run_id": "run-sc-1",
        "task_id": 41,
        "title": "Count items",
        "repo_full_name": "Asadtop4ik/task-manager",
        "status": "failed",
        "error": _CONFLICT,
        "chat_id": -1001,
        "owner_chat_id": 1001,
    }
    notice.update(overrides)
    return notice


def test_callback_data_stays_under_telegram_limit() -> None:
    for action in ("ask", "yes", "back"):
        packed = SpecRerunAction(action=action, task_id=2_000_000_000).pack()
        assert len(packed.encode()) <= 64


def test_result_card_escapes_the_untrusted_conflict_text() -> None:
    card = worker.agent_result_card(_failed_notice())
    assert "Spec ziddiyati" in card
    assert "&lt;b&gt;10 items&lt;/b&gt;" in card
    assert "<b>10 items</b>" not in card
    assert "SPEC_CONFLICT" not in card


def test_other_failures_keep_the_plain_reason() -> None:
    card = worker.agent_result_card(_failed_notice(error="Codex timed out"))
    assert "Sabab: Codex timed out" in card
    assert "Spec ziddiyati" not in card


async def test_worker_attaches_the_rerun_button_only_to_the_owner_card(monkeypatch) -> None:
    _client, bot = _install_worker_fakes(monkeypatch, [_failed_notice()])
    await worker.notify_agent_runs({})

    legacy, owner = bot.send_message.await_args_list
    assert legacy.kwargs.get("reply_markup") is None
    assert "&lt;b&gt;10 items&lt;/b&gt;" in owner.args[1]
    labels = _markup_labels(owner.kwargs["reply_markup"])
    assert labels == ["📜 Spec bo'yicha qayta ishga tushir"]
    button = owner.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == SpecRerunAction(action="ask", task_id=41).pack()


async def test_worker_final_attempt_has_no_button_and_says_why(monkeypatch) -> None:
    _client, bot = _install_worker_fakes(monkeypatch, [_failed_notice(attempt_index=2)])
    await worker.notify_agent_runs({})
    owner = bot.send_message.await_args_list[1]
    assert owner.kwargs["reply_markup"] is None
    assert "Qayta urinish limiti tugadi. Vazifani tahrirlang." in owner.args[1]


async def test_worker_first_attempt_keeps_the_button(monkeypatch) -> None:
    _client, bot = _install_worker_fakes(monkeypatch, [_failed_notice(attempt_index=1)])
    await worker.notify_agent_runs({})
    owner = bot.send_message.await_args_list[1]
    assert owner.kwargs["reply_markup"] is not None
    assert "limiti" not in owner.args[1]


async def test_worker_no_button_for_ordinary_failures(monkeypatch) -> None:
    _client, bot = _install_worker_fakes(
        monkeypatch, [_failed_notice(error="Codex timed out")]
    )
    await worker.notify_agent_runs({})
    assert bot.send_message.await_args_list[1].kwargs["reply_markup"] is None


# --- handler -----------------------------------------------------------------


def _api(monkeypatch, *, is_owner=True, runs=None):
    api = MagicMock()
    api.me = AsyncMock(return_value={"is_owner": is_owner})
    api.agent_runs = AsyncMock(
        return_value=[{"status": "failed", "error": _CONFLICT}] if runs is None else runs
    )
    api.start_agent_run = AsyncMock(return_value={"run_id": "r2"})
    monkeypatch.setattr(agent_spec, "api_for", lambda _: api)
    return api


async def test_ask_shows_confirm_step_and_back_restores(monkeypatch) -> None:
    api = _api(monkeypatch)
    query = _query()
    await agent_spec.spec_rerun_action(query, SpecRerunAction(action="ask", task_id=41))
    markup = query.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert _markup_labels(markup) == ["Ha, qayta ishga tushir", "Orqaga"]
    api.start_agent_run.assert_not_awaited()

    query = _query()
    await agent_spec.spec_rerun_action(query, SpecRerunAction(action="back", task_id=41))
    markup = query.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert _markup_labels(markup) == ["📜 Spec bo'yicha qayta ishga tushir"]
    api.start_agent_run.assert_not_awaited()


async def test_confirm_starts_the_run_with_the_fixed_owner_note(monkeypatch) -> None:
    api = _api(monkeypatch)
    query = _query()
    await agent_spec.spec_rerun_action(query, SpecRerunAction(action="yes", task_id=41))

    api.agent_runs.assert_awaited_once_with(41)
    api.start_agent_run.assert_awaited_once_with(
        41,
        owner_note="Owner spec'ni tasdiqladi: brief va acceptance mezonlarini so'zma-so'z "
        "bajar; spec'ni o'zgartirma.",
    )
    assert query.message.edit_reply_markup.await_args.kwargs["reply_markup"] is None
    assert query.answer.await_args.kwargs.get("show_alert") is None


async def test_confirm_on_a_stale_card_does_not_start_a_run(monkeypatch) -> None:
    api = _api(monkeypatch, runs=[{"status": "dispatched", "error": None}])
    query = _query()
    await agent_spec.spec_rerun_action(query, SpecRerunAction(action="yes", task_id=41))
    api.start_agent_run.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_non_owner_cannot_rerun(monkeypatch) -> None:
    api = _api(monkeypatch, is_owner=False)
    query = _query(actor_id=2002)
    await agent_spec.spec_rerun_action(query, SpecRerunAction(action="yes", task_id=41))
    api.agent_runs.assert_not_awaited()
    api.start_agent_run.assert_not_awaited()
    query.answer.assert_awaited_once_with("Bu amal faqat egasi uchun.", show_alert=True)


async def test_group_chat_is_refused_before_any_api_call(monkeypatch) -> None:
    api = _api(monkeypatch)
    query = _query(chat_type="supergroup")
    await agent_spec.spec_rerun_action(query, SpecRerunAction(action="yes", task_id=41))
    api.me.assert_not_awaited()
    api.start_agent_run.assert_not_awaited()
    assert query.answer.await_args.kwargs["show_alert"] is True


async def test_backend_409_maps_to_already_handled_and_removes_buttons(monkeypatch) -> None:
    api = _api(monkeypatch)
    api.start_agent_run.side_effect = ApiError(409, "spec_conflict_not_pending")
    query = _query()
    await agent_spec.spec_rerun_action(query, SpecRerunAction(action="yes", task_id=41))
    assert (
        query.answer.await_args.args[0]
        == "Allaqachon ishga tushirilgan yoki vazifa o'zgargan."
    )
    assert query.answer.await_args.kwargs["show_alert"] is True
    assert query.message.edit_reply_markup.await_args.kwargs["reply_markup"] is None


async def test_final_attempt_is_refused_early_in_uzbek(monkeypatch) -> None:
    api = _api(
        monkeypatch,
        runs=[{"status": "failed", "error": _CONFLICT, "attempt_index": 2}],
    )
    query = _query()
    await agent_spec.spec_rerun_action(query, SpecRerunAction(action="yes", task_id=41))
    api.start_agent_run.assert_not_awaited()
    assert (
        query.answer.await_args.args[0] == "Qayta urinish limiti tugadi. Vazifani tahrirlang."
    )
    assert query.message.edit_reply_markup.await_args.kwargs["reply_markup"] is None


async def test_markup_edit_failure_after_the_run_started_is_swallowed(monkeypatch) -> None:
    from aiogram.exceptions import TelegramBadRequest

    api = _api(monkeypatch)
    query = _query()
    query.message.edit_reply_markup.side_effect = TelegramBadRequest(
        method=MagicMock(), message="message to edit not found"
    )
    await agent_spec.spec_rerun_action(query, SpecRerunAction(action="yes", task_id=41))
    api.start_agent_run.assert_awaited_once()
    assert query.answer.await_args.args[0] == "Agent qayta ishga tushirildi."


async def test_inaccessible_message_gets_its_own_alert(monkeypatch) -> None:
    api = _api(monkeypatch)
    query = _query()
    query.message = None
    await agent_spec.spec_rerun_action(query, SpecRerunAction(action="yes", task_id=41))
    api.start_agent_run.assert_not_awaited()
    assert "eskirgan" in query.answer.await_args.args[0]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("SPEC_CONFLICT: x", True),
        ("SPEC_CONFLICT (x)", True),
        ("SPEC_CONFLICT x", True),
        ("SPEC_CONFLICTING", False),
        ("SPEC_CONFLICT", False),
        ("see SPEC_CONFLICT: x", False),
    ],
)
def test_spec_conflict_prefix_is_tight(error: str, expected: bool) -> None:
    assert agent_spec.is_spec_conflict({"status": "failed", "error": error}) is expected


def test_api_client_sends_owner_note_only_when_given() -> None:
    from app.api import TaskApi

    api = TaskApi(1001)
    calls = []

    async def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs["json"]))
        return {}

    api._request = fake_request  # type: ignore[method-assign]
    import asyncio

    asyncio.run(api.start_agent_run(5))
    asyncio.run(api.start_agent_run(5, owner_note="x"))
    assert calls == [
        ("POST", "/agent-runs/tasks/5", {"mode": "pr"}),
        ("POST", "/agent-runs/tasks/5", {"mode": "pr", "owner_note": "x"}),
    ]
