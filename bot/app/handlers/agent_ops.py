"""Owner-only Telegram controls for approving Codex's env-change ("ops") requests.

Codex never touches prod secrets directly: it proposes at most 3 env edits per
run, agent-svc validates them against a root-owned allowlist, and only the
owner's explicit tap here lets one through. Approval is two-step (it restarts a
service), rejection is one tap. Every request carries a `request_hash` prefix so
a click on a stale card (the request already changed since it was drawn)
refreshes instead of acting on the wrong thing.

`AgentOpsAction` deliberately has no `run_id` field: `ops_id` (up to 10 digits),
`h` (a 10-hex prefix) and the longest action name already leave no room for a
36-char run UUID inside Telegram's 64-byte callback-data cap (confirmed: aiogram
itself refuses to pack one). So every handler resolves the owning run via
`TaskApi.agent_run_for_ops(ops_id)` — see the ASSUMPTION note below.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid5

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.api import ApiError
from app.callbacks import AgentOpsAction
from app.handlers.agent_release import _html_text
from app.handlers.helpers import api_for, editable

router = Router(name="agent_ops")

_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z", re.ASCII)
_INVALID_KEY_PLACEHOLDER = "(noto‘g‘ri kalit)"

_OP_LABELS = {
    "replace": "o‘zgartirish",
    "list_add": "qo‘shish",
    "list_remove": "olib tashlash",
}

_STATUS_LABELS = {
    "proposed": "⏳ tasdiq kutilmoqda",
    "approved": "✅ tasdiqlandi — deploydan keyin qo‘llanadi",
    "applying": "⚙️ qo‘llanmoqda",
    "rejected": "❌ rad etildi",
    "cancelled": "✖️ bekor qilindi",
}

_RESULT_CODE_LABELS = {
    "precondition": "⚠️ xato: precondition",
    "failed_rolled_back": "↩️ xato, orqaga qaytarildi",
    "failed_rollback_failed": "🚨 orqaga qaytarish ham o‘tmadi — qo‘lda tekshiring",
    "refused": "⛔ siyosat rad etdi",
    "bad_request": "⚠️ xato: bad_request",
    "busy": "⏳ band, qayta urinilmoqda",
}

_DECISION_ALERTS = {
    "conflict": "Boshqa qaror allaqachon qabul qilingan.",
    "not_pending": "Bu so‘rov endi kutilmayapti.",
    "stale_request": "Karta yangilandi.",
}

STALE_CARD_ALERT = "Karta yangilandi"

# The closed set of machine codes agent-svc can send as the run-level
# `ops_note` (agentsvc/agent_svc/ops_requests.py `build_ops_note`,
# agentsvc/agent_svc/implement.py) or as a denied row's `policy_reason`
# (agentsvc's own `no_allowlist`/`ops_disabled`; see agentsvc/agent_svc/
# ops_requests.py and implement.py) — mapped to a short Uzbek line so the
# owner never sees raw agent-svc/policy text. A code outside this set is
# hidden entirely rather than shown raw.
_OPS_REASON_LABELS = {
    "ambiguous": "Codex bir nechta ops so‘rovi qatorini yubordi — birortasi ham qabul qilinmadi.",
    "invalid_trailer": "Codexning ops so‘rovi formatida xato bor edi.",
    "ops_unavailable": "Ops tekshiruvi ishlamadi — hech narsa taklif qilinmadi.",
    "no_allowlist": "Bu loyiha uchun ruxsat ro‘yxati topilmadi.",
    "ops_disabled": "Ops funksiyasi hozircha o‘chirilgan.",
}
_OPS_DROPPED_RE = re.compile(r"dropped_(\d+)\Z")

# A safety margin under Telegram's 4096-character message cap (see
# bot/app/cards.py's own `MAX_CARD_CHARS`) — the release/status portion is
# truncated to keep the whole owner message under this; the ops block itself
# is never truncated (approve/reject evidence must stay intact).
_OWNER_TEXT_LIMIT = 4000


def _ops_reason_label(code: Any) -> str | None:
    text = str(code or "")
    if text in _OPS_REASON_LABELS:
        return _OPS_REASON_LABELS[text]
    match = _OPS_DROPPED_RE.fullmatch(text)
    if match:
        return (
            f"Codex {match.group(1)} ta ops so‘rovini noto‘g‘ri formatda yubordi — "
            "ular e’tiborga olinmadi."
        )
    return None


def ops_note_line(notice: dict[str, Any]) -> str | None:
    """The run-level `ops_note` as one short Uzbek line — never the raw code,
    and omitted entirely for an unknown/future one (see `_ops_reason_label`)."""
    label = _ops_reason_label(notice.get("ops_note"))
    return f"Ops eslatmasi: {label}" if label else None


def compose_owner_text(base: str, block: list[str]) -> str:
    """`base` (the release/status card) plus the ops block, truncating `base`
    — never `block` — so the total stays at or under `_OWNER_TEXT_LIMIT`."""
    if not block:
        return base
    tail = "\n".join(block)
    budget = max(_OWNER_TEXT_LIMIT - len(tail) - 1, 0)
    if len(base) > budget:
        base = base[: max(budget - 1, 0)].rstrip() + "…"
    return "\n".join([base, tail])


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _safe_key(row: dict[str, Any]) -> str:
    """The raw key, only if it looks like a real env key — never the value."""
    key = str(row.get("key") or "")
    if _KEY_RE.fullmatch(key):
        return _html_text(key, 64)
    return _INVALID_KEY_PLACEHOLDER


def _hash_prefix(value: Any) -> str | None:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else None


def _status_line(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "")
    raw_result = row.get("result")
    result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    if status == "applied":
        image = str(result.get("image_tag") or "")
        if image:
            short = image[:7] + ("…" if len(image) > 7 else "")
            return f"✅ qo‘llandi (image {_html_text(short, 16)})"
        return "✅ qo‘llandi"
    if status == "failed":
        code = str(result.get("code") or "")
        return _RESULT_CODE_LABELS.get(code, f"⚠️ xato: {_html_text(code or 'noma’lum', 40)}")
    if status == "invalid":
        label = "⛔ siyosat rad etdi"
        reason_label = _ops_reason_label(row.get("policy_reason"))
        if reason_label:
            label += f" — {reason_label}"
        return label
    return _STATUS_LABELS.get(status, _html_text(status or "noma’lum", 40))


def _row_lines(row: dict[str, Any]) -> list[str]:
    position = row.get("position")
    position_label = position if isinstance(position, int) else "?"
    status = str(row.get("status") or "")

    if status == "invalid":
        # A denied/invalid row failed policy validation — the value it carried
        # may be anything Codex sent, so it is never rendered, and even the
        # key is only shown when it still looks like a real env key.
        lines = [f"{position_label}. {_safe_key(row)}", f"   Holat: {_status_line(row)}"]
        reason = row.get("reason")
        if reason:
            lines.append(f"   Sabab: Codex, tasdiqlanmagan: {_html_text(reason, 300)}")
        return lines

    key = _safe_key(row)
    op_raw = str(row.get("op") or "")
    op_label = _OP_LABELS.get(op_raw, _html_text(op_raw or "amal", 24))
    # The full value, escaped — never truncated: `AgentOpsProposal.value` is
    # already capped at 256 chars server-side (`OPS_VALUE_RE`), so this is
    # just a defensive ceiling, not a display truncation.
    value = _html_text(row.get("value"), 256)
    services = row.get("restart_services")
    service_names = (
        ", ".join(_html_text(name, 40) for name in services if isinstance(name, str))
        if isinstance(services, list)
        else ""
    )
    arrow = f" → {service_names} qayta ishga tushadi" if service_names else ""
    lines = [
        f"{position_label}. {key} · {op_label}: <code>{value}</code>{arrow}",
        f"   Holat: {_status_line(row)}",
    ]
    reason = row.get("reason")
    if reason:
        lines.append(f"   Sabab: Codex, tasdiqlanmagan: {_html_text(reason, 300)}")
    return lines


def ops_lines(notice: dict[str, Any]) -> list[str]:
    """The full owner-facing ops block: the run-level `ops_note` (if any),
    then a header plus one entry per request.

    Empty when there is nothing to show (older API responses, or a run that
    never proposed any and has no note), so callers can append the result
    unconditionally.
    """
    rows = notice.get("ops_requests")
    note = ops_note_line(notice)
    if not isinstance(rows, list) or not rows:
        return ["", note] if note else []
    lines = [""]
    if note:
        lines.append(note)
    lines.append(f"<b>Ops so‘rovlari ({len(rows)}):</b>")
    for row in rows:
        if isinstance(row, dict):
            lines.extend(_row_lines(row))
    return lines


def ops_count_line(notice: dict[str, Any]) -> str | None:
    """The one line the legacy task-origin card is allowed to show — no keys, no values."""
    count = notice.get("ops_pending_count")
    if not isinstance(count, int):
        # Older/partial API responses may omit the field; count `proposed`
        # rows ourselves rather than silently showing nothing.
        rows = notice.get("ops_requests")
        count = (
            sum(1 for row in rows if isinstance(row, dict) and row.get("status") == "proposed")
            if isinstance(rows, list)
            else 0
        )
    if count <= 0:
        return None
    return f"Ops: {count} ta so‘rov egasi tasdig‘ida"


def ops_keyboard(
    ops_requests: list[Any] | None,
    *,
    asking_ops_id: int | None = None,
) -> InlineKeyboardMarkup | None:
    """Buttons for every `proposed` row; `asking_ops_id` flips one row to its confirm step."""
    rows = [
        row
        for row in (ops_requests or [])
        if isinstance(row, dict) and row.get("status") == "proposed"
    ]
    if not rows:
        return None
    builder = InlineKeyboardBuilder()
    added = False
    for row in rows:
        ops_id = row.get("id")
        if not isinstance(ops_id, int):
            continue
        h = _hash_prefix(row.get("request_hash"))
        if h is None:
            continue
        position = row.get("position") if isinstance(row.get("position"), int) else "?"
        if ops_id == asking_ops_id:
            builder.button(
                text=f"Ha, #{position} ni qo‘llash",
                callback_data=AgentOpsAction(action="yes", ops_id=ops_id, h=h),
            )
            builder.button(
                text="Orqaga",
                callback_data=AgentOpsAction(action="back", ops_id=ops_id, h=h),
            )
        else:
            builder.button(
                text=f"✅ #{position} tasdiqlash",
                callback_data=AgentOpsAction(action="ask", ops_id=ops_id, h=h),
            )
            builder.button(
                text=f"❌ #{position} rad etish",
                callback_data=AgentOpsAction(action="no", ops_id=ops_id, h=h),
            )
        added = True
    if not added:
        return None
    builder.adjust(2)
    return builder.as_markup()


def combine_keyboards(*markups: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """Stack two keyboards' rows into one (release controls above, ops controls below)."""
    rows: list[list[Any]] = []
    for markup in markups:
        if markup is not None:
            rows.extend(markup.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _find_row(run: dict[str, Any], ops_id: int) -> dict[str, Any] | None:
    rows = run.get("ops_requests")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("id") == ops_id:
            return row
    return None


def _decision_alert(error: ApiError) -> str:
    # The backend sends a plain `detail` string ("conflict", "not_pending", …),
    # not the `{"error": {"code": ...}}` shape `TaskApi` parses a `code` from —
    # so `error.code` is None here and the real signal is in `error.detail`.
    key = error.code or error.detail
    if key in _DECISION_ALERTS:
        return _DECISION_ALERTS[key]
    if error.status == 409:
        return "Bu amal endi mumkin emas (run yopilgan)."
    return _short(error.detail, 180)


def _owner_card(
    run: dict[str, Any], *, asking_ops_id: int | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    """The owner's own view after a click: release text (or plain status) plus the ops block.

    Kept independent from `worker.notify_agent_runs`'s own composition (which
    additionally gates on the notification-only `ops_controls_available` flag
    for the *first* send) the same way `agent_release._refresh` is already
    independent of it for release actions — once a card exists with buttons, a
    real click on it is its own proof the feature is live.
    """
    head_sha = str(run.get("head_sha") or "")
    ops_requests = run.get("ops_requests") if isinstance(run.get("ops_requests"), list) else []
    block = ops_lines(run)
    ops_markup = ops_keyboard(ops_requests, asking_ops_id=asking_ops_id)

    if len(head_sha) == 40:
        # Deferred import: agent_release is the lower-level card renderer and
        # never imports this module, so this stays a one-way dependency.
        from app.handlers.agent_release import release_card, release_keyboard

        base = release_card(run)
        release_markup = release_keyboard(
            str(run.get("run_id") or ""), head_sha, actions=run.get("actions") or {}
        )
        markup = combine_keyboards(release_markup, ops_markup)
    else:
        # An ops-only run (no PR, no head_sha): reuse the exact status wording
        # the owner already saw when the card first arrived. Deferred import
        # breaks the cycle — worker.py imports from this module at load time.
        from app.worker import agent_result_card

        base = agent_result_card(run)
        markup = ops_markup

    text = compose_owner_text(base, block)
    return text, markup


async def _refresh(
    query: CallbackQuery, run: dict[str, Any], *, asking_ops_id: int | None = None
) -> None:
    message = editable(query)
    if message is None:
        return
    text, markup = _owner_card(run, asking_ops_id=asking_ops_id)
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


async def _load_owner_run(query: CallbackQuery, ops_id: int) -> dict[str, Any] | None:
    message = editable(query)
    if message is None or message.chat.type != "private":
        await query.answer(
            "Bu amal faqat egasining shaxsiy chatida ishlaydi.", show_alert=True
        )
        return None
    api = api_for(query)
    try:
        actor = await api.me()
        if not actor.get("is_owner"):
            await query.answer("Bu amal faqat egasi uchun.", show_alert=True)
            return None
        return await api.agent_run_for_ops(ops_id)
    except ApiError as error:
        if error.status == 404:
            text = "So‘rov topilmadi yoki endi mavjud emas."
        elif error.status == 403:
            text = "Bu amal faqat egasi uchun."
        else:
            text = "Holatni olib bo‘lmadi. Birozdan so‘ng qayta urinib ko‘ring."
        await query.answer(text, show_alert=True)
        return None


@router.callback_query(AgentOpsAction.filter())
async def ops_action(query: CallbackQuery, callback_data: AgentOpsAction) -> None:
    run = await _load_owner_run(query, callback_data.ops_id)
    if run is None:
        return

    row = _find_row(run, callback_data.ops_id)
    if row is None:
        await query.answer("So‘rov topilmadi.", show_alert=True)
        await _refresh(query, run)
        return

    current_hash = _hash_prefix(row.get("request_hash"))
    if current_hash is None or current_hash != callback_data.h:
        await query.answer(STALE_CARD_ALERT, show_alert=True)
        await _refresh(query, run)
        return

    if callback_data.action == "ask":
        await query.answer()
        await _refresh(query, run, asking_ops_id=callback_data.ops_id)
        return

    if callback_data.action == "back":
        await query.answer()
        await _refresh(query, run)
        return

    if callback_data.action not in {"yes", "no"}:
        await query.answer("Noma’lum amal.", show_alert=True)
        return

    if row.get("status") != "proposed":
        await query.answer("Bu so‘rov endi kutilmayapti.", show_alert=True)
        await _refresh(query, run)
        return

    decision = "approve" if callback_data.action == "yes" else "reject"
    request_hash = str(row.get("request_hash") or "")
    run_id = str(run.get("run_id") or "")
    action_id = str(
        uuid5(UUID(run_id), f"ops:{callback_data.ops_id}:{decision}:{request_hash}")
    )
    api = api_for(query)
    try:
        result = await api.decide_ops_request(
            callback_data.ops_id,
            decision=decision,
            request_hash=request_hash,
            action_id=action_id,
        )
    except ApiError as error:
        await query.answer(_decision_alert(error), show_alert=True)
        if error.status == 409:
            refreshed = await _load_owner_run(query, callback_data.ops_id)
            if refreshed is not None:
                await _refresh(query, refreshed)
        return

    await query.answer("Tasdiqlandi." if decision == "approve" else "Rad etildi.")
    updated_run = result.get("run") if isinstance(result, dict) else None
    await _refresh(query, updated_run if isinstance(updated_run, dict) else run)
