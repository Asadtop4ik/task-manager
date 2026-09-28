import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

from app import worker
from app.callbacks import AgentOpsAction
from app.handlers import agent_ops


def _ops_row(**overrides):
    row = {
        "id": 501,
        "position": 1,
        "kind": "env_set",
        "key": "ADMIN_TG_IDS",
        "op": "list_add",
        "value": "5339875840",
        "reason": "owner asked for one more admin",
        "restart_services": ["qurbot-web", "qurbot-worker"],
        "request_hash": "a" * 64,
        "status": "proposed",
        "policy_reason": None,
        "result": None,
    }
    row.update(overrides)
    return row


def _ops_run(*, head_sha="", ops_requests=None, run_id="12345678-1234-5678-1234-567812345678"):
    return {
        "run_id": run_id,
        "task_id": 9,
        "title": "Env tweak",
        "status": "ops_pending" if not head_sha else "pr_ready",
        "repo_full_name": "muradjanov-dev/qurbot",
        "head_sha": head_sha,
        "actions": {"merge": {"available": True}, "correction": {"available": True}},
        "summary": "Checkout timeout fix",
        "impact": "Retries stay bounded",
        "ops_requests": ops_requests if ops_requests is not None else [_ops_row()],
    }


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


def _markup_labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


# --- callback data ---------------------------------------------------------


def test_ops_callback_data_stays_under_telegram_limit() -> None:
    for action in ("ask", "yes", "no", "back"):
        packed = AgentOpsAction(action=action, ops_id=2_000_000_000, h="0123456789").pack()
        assert len(packed.encode()) <= 64


# --- ops_lines: escaping, truncation, invalid rows --------------------------


def test_ops_lines_escapes_and_truncates_value_and_reason() -> None:
    row = _ops_row(value="<script>" + "x" * 200, reason="<b>" + "y" * 400)
    lines = agent_ops.ops_lines({"ops_requests": [row]})
    text = "\n".join(lines)

    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    value_line = next(line for line in lines if "<code>" in line)
    inner = re.search(r"<code>(.*?)</code>", value_line).group(1)
    # _html_text's budget bounds the escaped content at 80; the ellipsis is one
    # more character on top of that, so the rendered value tops out at 81.
    assert len(inner) <= 81
    assert inner.endswith("…")

    reason_line = next(line for line in lines if line.strip().startswith("Sabab:"))
    assert "Codex, tasdiqlanmagan:" in reason_line
    assert "&lt;b&gt;" in reason_line
    reason_text = reason_line.split("Codex, tasdiqlanmagan:", 1)[1].strip()
    assert len(reason_text) <= 301


def test_ops_lines_header_counts_all_rows_and_uses_position() -> None:
    rows = [_ops_row(id=1, position=1), _ops_row(id=2, position=2, op="replace", value="x")]
    lines = agent_ops.ops_lines({"ops_requests": rows})
    assert "Ops so‘rovlari (2):" in lines[1]
    assert lines[2].startswith("1. ADMIN_TG_IDS")


def test_unknown_op_falls_back_to_an_escaped_label() -> None:
    row = _ops_row(op="<b>x</b>")
    text = "\n".join(agent_ops.ops_lines({"ops_requests": [row]}))
    assert "<b>x</b>" not in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text


def test_invalid_row_with_valid_key_shows_key_but_never_value() -> None:
    row = _ops_row(status="invalid", value="5339875840", policy_reason="allowlistda yo‘q")
    text = "\n".join(agent_ops.ops_lines({"ops_requests": [row]}))
    assert "ADMIN_TG_IDS" in text
    assert "5339875840" not in text
    assert "allowlistda yo‘q" in text


def test_invalid_row_with_unsafe_key_shows_placeholder_never_the_key_or_value() -> None:
    row = _ops_row(status="invalid", key="not a key; rm -rf", value="super-secret")
    text = "\n".join(agent_ops.ops_lines({"ops_requests": [row]}))
    assert "super-secret" not in text
    assert "not a key" not in text
    assert "(noto‘g‘ri kalit)" in text


def test_result_codes_render_their_documented_labels() -> None:
    precondition = agent_ops._status_line(
        _ops_row(status="failed", result={"code": "precondition"})
    )
    rolled_back = agent_ops._status_line(
        _ops_row(status="failed", result={"code": "failed_rolled_back"})
    )
    rollback_failed = agent_ops._status_line(
        _ops_row(status="failed", result={"code": "failed_rollback_failed"})
    )
    applied = agent_ops._status_line(
        _ops_row(status="applied", result={"image_tag": "a2911c0ff00d"})
    )
    assert "precondition" in precondition
    assert "orqaga qaytarildi" in rolled_back
    assert "qo‘lda tekshiring" in rollback_failed
    assert "a2911c0…" in applied


def test_missing_ops_fields_never_crash() -> None:
    assert agent_ops.ops_lines({}) == []
    assert agent_ops.ops_lines({"ops_requests": None}) == []
    assert agent_ops.ops_lines({"ops_requests": "not-a-list"}) == []
    assert agent_ops.ops_keyboard(None) is None
    assert agent_ops.ops_keyboard([{"status": "applied"}]) is None
    assert agent_ops.ops_count_line({}) is None
    card = worker.agent_result_card(
        {"task_id": 1, "title": "x", "repo_full_name": "a/b", "status": "pr_opened"}
    )
    assert card


# --- legacy vs owner card: values only ever on the owner side --------------


def test_agent_result_card_never_shows_keys_or_values_or_the_count_itself() -> None:
    # agent_result_card() is reused as the owner-card base for ops-only runs
    # (see agent_ops._owner_card), so it must stay free of the count line —
    # only the legacy/task-origin send path in notify_agent_runs appends it.
    notice = {
        "task_id": 1,
        "title": "Env tweak",
        "repo_full_name": "muradjanov-dev/qurbot",
        "status": "ops_pending",
        "ops_pending_count": 1,
        "ops_requests": [_ops_row()],
    }
    card = worker.agent_result_card(notice)
    assert "Ops:" not in card
    assert "ADMIN_TG_IDS" not in card
    assert "5339875840" not in card


def test_ops_count_line_reads_the_field_and_falls_back_to_counting_proposed_rows() -> None:
    rows = [
        _ops_row(id=1, status="proposed"),
        _ops_row(id=2, status="applied"),
        _ops_row(id=3, status="proposed"),
    ]
    # Field present: trust it even if it disagrees with the rows.
    assert (
        agent_ops.ops_count_line({"ops_pending_count": 5, "ops_requests": rows})
        == "Ops: 5 ta so‘rov egasi tasdig‘ida"
    )
    assert agent_ops.ops_count_line({"ops_pending_count": 0, "ops_requests": rows}) is None
    # Field absent or None: fall back to counting `proposed` rows ourselves.
    assert (
        agent_ops.ops_count_line({"ops_requests": rows}) == "Ops: 2 ta so‘rov egasi tasdig‘ida"
    )
    assert (
        agent_ops.ops_count_line({"ops_pending_count": None, "ops_requests": rows})
        == "Ops: 2 ta so‘rov egasi tasdig‘ida"
    )
    assert agent_ops.ops_count_line({}) is None
    assert agent_ops.ops_count_line({"ops_requests": [_ops_row(status="applied")]}) is None


def test_ops_applied_and_ops_pending_legacy_status_lines() -> None:
    pending = worker.agent_result_card(
        {"task_id": 1, "title": "x", "repo_full_name": "a/b", "status": "ops_pending"}
    )
    applied = worker.agent_result_card(
        {"task_id": 1, "title": "x", "repo_full_name": "a/b", "status": "ops_applied"}
    )
    assert "tasdig‘ini kutmoqda" in pending
    assert "qo‘llandi" in applied
    assert "Agent ishi to‘xtadi" not in pending
    assert "Agent ishi to‘xtadi" not in applied


# --- ops-only owner card (no head_sha) --------------------------------------


def test_ops_only_owner_card_has_ops_buttons_and_no_release_actions() -> None:
    run = _ops_run(head_sha="")
    text, markup = agent_ops._owner_card(run)
    assert "ADMIN_TG_IDS" in text
    labels = _markup_labels(markup)
    assert any("tasdiqlash" in label for label in labels)
    assert "Merge va deploy" not in labels
    assert "Batafsil" not in labels


def test_release_owner_card_combines_release_and_ops_rows() -> None:
    run = _ops_run(head_sha="c" * 40)
    text, markup = agent_ops._owner_card(run)
    assert "ADMIN_TG_IDS" in text
    labels = _markup_labels(markup)
    assert "Merge va deploy" in labels
    assert any("tasdiqlash" in label for label in labels)


# --- two-step approve, single-step reject -----------------------------------


async def test_ask_flips_the_keyboard_without_deciding(monkeypatch) -> None:
    row = _ops_row()
    run = _ops_run(ops_requests=[row])
    api = MagicMock()
    api.me = AsyncMock(return_value={"is_owner": True})
    api.agent_run_for_ops = AsyncMock(return_value=run)
    api.decide_ops_request = AsyncMock()
    monkeypatch.setattr(agent_ops, "api_for", lambda _: api)
    query = _query()

    await agent_ops.ops_action(
        query, AgentOpsAction(action="ask", ops_id=row["id"], h=row["request_hash"][:10])
    )

    api.decide_ops_request.assert_not_awaited()
    query.message.edit_text.assert_awaited_once()
    markup = query.message.edit_text.await_args.kwargs["reply_markup"]
    labels = _markup_labels(markup)
    assert any("Ha, #1" in label for label in labels)
    assert "Orqaga" in labels


async def test_back_restores_the_normal_keyboard(monkeypatch) -> None:
    row = _ops_row()
    run = _ops_run(ops_requests=[row])
    api = MagicMock()
    api.me = AsyncMock(return_value={"is_owner": True})
    api.agent_run_for_ops = AsyncMock(return_value=run)
    api.decide_ops_request = AsyncMock()
    monkeypatch.setattr(agent_ops, "api_for", lambda _: api)
    query = _query()

    await agent_ops.ops_action(
        query, AgentOpsAction(action="back", ops_id=row["id"], h=row["request_hash"][:10])
    )

    api.decide_ops_request.assert_not_awaited()
    markup = query.message.edit_text.await_args.kwargs["reply_markup"]
    labels = _markup_labels(markup)
    assert "✅ #1 tasdiqlash" in labels
    assert "❌ #1 rad etish" in labels


async def test_yes_approves_with_full_hash_and_idempotent_action_id(monkeypatch) -> None:
    row = _ops_row()
    run = _ops_run(ops_requests=[row])
    approved = run | {"ops_requests": [row | {"status": "approved"}]}
    api = MagicMock()
    api.me = AsyncMock(return_value={"is_owner": True})
    api.agent_run_for_ops = AsyncMock(return_value=run)
    api.decide_ops_request = AsyncMock(return_value={"ops_request": row, "run": approved})
    monkeypatch.setattr(agent_ops, "api_for", lambda _: api)
    callback = AgentOpsAction(action="yes", ops_id=row["id"], h=row["request_hash"][:10])

    await agent_ops.ops_action(_query(), callback)
    await agent_ops.ops_action(_query(), callback)

    assert api.decide_ops_request.await_count == 2
    calls = api.decide_ops_request.await_args_list
    assert calls[0].kwargs["decision"] == "approve"
    assert calls[0].kwargs["request_hash"] == row["request_hash"]
    ids = [call.kwargs["action_id"] for call in calls]
    assert ids[0] == ids[1]
    UUID(ids[0])


async def test_no_rejects_in_a_single_step(monkeypatch) -> None:
    row = _ops_row()
    run = _ops_run(ops_requests=[row])
    api = MagicMock()
    api.me = AsyncMock(return_value={"is_owner": True})
    api.agent_run_for_ops = AsyncMock(return_value=run)
    api.decide_ops_request = AsyncMock(return_value={"run": run})
    monkeypatch.setattr(agent_ops, "api_for", lambda _: api)
    query = _query()

    await agent_ops.ops_action(
        query, AgentOpsAction(action="no", ops_id=row["id"], h=row["request_hash"][:10])
    )

    api.decide_ops_request.assert_awaited_once()
    assert api.decide_ops_request.await_args.kwargs["decision"] == "reject"


# --- guards: owner-only, private-only, freshness ----------------------------


async def test_non_owner_callback_cannot_fetch_or_decide(monkeypatch) -> None:
    api = MagicMock()
    api.me = AsyncMock(return_value={"is_owner": False})
    api.agent_run_for_ops = AsyncMock()
    api.decide_ops_request = AsyncMock()
    monkeypatch.setattr(agent_ops, "api_for", lambda _: api)
    query = _query(actor_id=2002)

    await agent_ops.ops_action(query, AgentOpsAction(action="ask", ops_id=501, h="a" * 10))

    api.agent_run_for_ops.assert_not_awaited()
    api.decide_ops_request.assert_not_awaited()
    query.answer.assert_awaited_once_with("Bu amal faqat egasi uchun.", show_alert=True)


async def test_group_chat_refused_before_any_api_call(monkeypatch) -> None:
    api = MagicMock()
    api.me = AsyncMock()
    monkeypatch.setattr(agent_ops, "api_for", lambda _: api)
    query = _query(chat_type="group")

    await agent_ops.ops_action(query, AgentOpsAction(action="ask", ops_id=501, h="a" * 10))

    api.me.assert_not_awaited()
    assert "shaxsiy chatida" in query.answer.await_args.args[0]


async def test_stale_hash_prefix_refreshes_instead_of_deciding(monkeypatch) -> None:
    row = _ops_row()
    run = _ops_run(ops_requests=[row])
    api = MagicMock()
    api.me = AsyncMock(return_value={"is_owner": True})
    api.agent_run_for_ops = AsyncMock(return_value=run)
    api.decide_ops_request = AsyncMock()
    monkeypatch.setattr(agent_ops, "api_for", lambda _: api)
    query = _query()

    await agent_ops.ops_action(
        query, AgentOpsAction(action="yes", ops_id=row["id"], h="f" * 10)
    )

    api.decide_ops_request.assert_not_awaited()
    query.answer.assert_awaited_once_with(agent_ops.STALE_CARD_ALERT, show_alert=True)
    query.message.edit_text.assert_awaited_once()


async def test_conflict_alert_maps_to_short_uzbek_text_and_refreshes(monkeypatch) -> None:
    from app.api import ApiError

    row = _ops_row()
    run = _ops_run(ops_requests=[row])
    api = MagicMock()
    api.me = AsyncMock(return_value={"is_owner": True})
    api.agent_run_for_ops = AsyncMock(return_value=run)
    api.decide_ops_request = AsyncMock(
        side_effect=ApiError(409, "already decided", code="conflict")
    )
    monkeypatch.setattr(agent_ops, "api_for", lambda _: api)
    query = _query()

    await agent_ops.ops_action(
        query, AgentOpsAction(action="yes", ops_id=row["id"], h=row["request_hash"][:10])
    )

    query.answer.assert_awaited_once_with(
        "Boshqa qaror allaqachon qabul qilingan.", show_alert=True
    )
    assert api.agent_run_for_ops.await_count == 2  # initial load + post-409 refresh


# --- worker.notify_agent_runs integration -----------------------------------


class _Client:
    def __init__(self, notices):
        self._notices = notices
        self.posts: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, *args, **kwargs):
        response = MagicMock()
        response.json.return_value = self._notices
        return response

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs.get("json")))
        return MagicMock()


def _install_worker_fakes(monkeypatch, notices):
    client = _Client(notices)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=99))
    bot.edit_message_text = AsyncMock()
    bot.session.close = AsyncMock()
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(worker, "Bot", lambda **kwargs: bot)
    monkeypatch.setattr(
        worker,
        "settings",
        SimpleNamespace(service_token="test", api_base_url="http://api", bot_token="1:T"),
    )
    return client, bot


async def test_worker_appends_ops_block_to_owner_release_card(monkeypatch) -> None:
    row = _ops_row()
    notice = {
        "run_id": "12345678-1234-5678-1234-567812345678",
        "task_id": 1,
        "title": "Checkout fix",
        "repo_full_name": "muradjanov-dev/qurbot",
        "status": "pr_ready",
        "chat_id": -1001,
        "owner_chat_id": 1001,
        "owner_controls_available": True,
        "ops_controls_available": True,
        "head_sha": "c" * 40,
        "summary": "Checkout timeout fix",
        "impact": "Retries stay bounded",
        "actions": {"merge": {"available": True}, "correction": {"available": True}},
        "ops_requests": [row],
        "ops_pending_count": 1,
    }
    _client, bot = _install_worker_fakes(monkeypatch, [notice])

    await worker.notify_agent_runs({})

    calls = bot.send_message.await_args_list
    legacy_text = calls[0].args[1]
    owner_text = calls[1].args[1]
    owner_markup = calls[1].kwargs["reply_markup"]

    assert "Ops: 1 ta so‘rov egasi tasdig‘ida" in legacy_text
    assert "ADMIN_TG_IDS" not in legacy_text

    assert "ADMIN_TG_IDS" in owner_text
    assert "5339875840" in owner_text
    labels = _markup_labels(owner_markup)
    assert "Merge va deploy" in labels
    assert any("tasdiqlash" in label for label in labels)


async def test_worker_ops_only_run_gets_owner_card_with_ops_buttons(monkeypatch) -> None:
    row = _ops_row()
    notice = {
        "run_id": "run-ops-1",
        "task_id": 2,
        "title": "Add admin",
        "repo_full_name": "muradjanov-dev/qurbot",
        "status": "ops_pending",
        "chat_id": -1002,
        "owner_chat_id": 1001,
        "ops_controls_available": True,
        "ops_requests": [row],
        "ops_pending_count": 1,
    }
    _client, bot = _install_worker_fakes(monkeypatch, [notice])

    await worker.notify_agent_runs({})

    calls = bot.send_message.await_args_list
    legacy_text = calls[0].args[1]
    owner_text = calls[1].args[1]
    owner_markup = calls[1].kwargs["reply_markup"]

    # The legacy/task-origin card: count only, never a key or value.
    assert "Ops: 1 ta so‘rov egasi tasdig‘ida" in legacy_text
    assert "ADMIN_TG_IDS" not in legacy_text
    assert "5339875840" not in legacy_text

    # The owner card (no head_sha, so agent_result_card is its base): full
    # detail block, but no redundant count line — that line is legacy-only.
    assert "ADMIN_TG_IDS" in owner_text
    assert "5339875840" in owner_text
    assert "Ops: 1 ta so‘rov egasi tasdig‘ida" not in owner_text
    labels = _markup_labels(owner_markup)
    assert any("tasdiqlash" in label for label in labels)
    assert "Merge va deploy" not in labels


async def test_worker_without_ops_controls_available_shows_text_but_no_buttons(
    monkeypatch,
) -> None:
    row = _ops_row()
    notice = {
        "run_id": "run-ops-2",
        "task_id": 3,
        "title": "Add admin",
        "repo_full_name": "muradjanov-dev/qurbot",
        "status": "ops_pending",
        "chat_id": -1003,
        "owner_chat_id": 1001,
        "ops_controls_available": False,
        "ops_requests": [row],
        "ops_pending_count": 1,
    }
    _client, bot = _install_worker_fakes(monkeypatch, [notice])

    await worker.notify_agent_runs({})

    owner_call = bot.send_message.await_args_list[1]
    assert "ADMIN_TG_IDS" in owner_call.args[1]
    assert owner_call.kwargs["reply_markup"] is None


async def test_worker_old_notice_without_ops_fields_does_not_crash(monkeypatch) -> None:
    notice = {
        "run_id": "run-plain",
        "task_id": 4,
        "title": "Plain run",
        "repo_full_name": "muradjanov-dev/qurbot",
        "status": "merged",
        "chat_id": -1004,
        "pr_url": "https://github.com/muradjanov-dev/qurbot/pull/9",
    }
    _client, bot = _install_worker_fakes(monkeypatch, [notice])

    await worker.notify_agent_runs({})

    bot.send_message.assert_awaited_once()
