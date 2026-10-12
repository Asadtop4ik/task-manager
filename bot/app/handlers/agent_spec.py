"""Owner-only re-run of a task whose agent run stopped on a SPEC_CONFLICT.

agent-svc reports `failed` with an `error` starting `SPEC_CONFLICT` when the task
brief and its acceptance criteria contradict each other. The conflict text is model
output, so it is only ever shown HTML-escaped. The owner may confirm the spec as
written; that starts the next attempt with a fixed `owner_note` (the backend accepts
`owner_note` from the owner only and keeps its own attempt cap).
"""

from __future__ import annotations

import re
from typing import Any

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.api import ApiError
from app.callbacks import SpecRerunAction
from app.handlers.helpers import api_for, editable
from app.logging import get_logger

router = Router(name="agent_spec")
log = get_logger(__name__)

SPEC_CONFLICT_PREFIX = "SPEC_CONFLICT"
# Same rule as the backend's `start_agent_run` guard: the marker, then a space, "(" or ":".
_SPEC_CONFLICT_RE = re.compile(r"SPEC_CONFLICT[ (:]")
# The backend allows two attempts per task revision (attempt_index 1 and 2).
MAX_ATTEMPTS = 2
LIMIT_REACHED_TEXT = "Qayta urinish limiti tugadi. Vazifani tahrirlang."
ALREADY_HANDLED_TEXT = "Allaqachon ishga tushirilgan yoki vazifa o'zgargan."
SPEC_CONFIRMED_NOTE = (
    "Owner spec'ni tasdiqladi: brief va acceptance mezonlarini so'zma-so'z bajar; "
    "spec'ni o'zgartirma."
)


def is_spec_conflict(run: dict[str, Any]) -> bool:
    return run.get("status") == "failed" and bool(
        _SPEC_CONFLICT_RE.match(str(run.get("error") or ""))
    )


def attempts_exhausted(run: dict[str, Any]) -> bool:
    attempt = run.get("attempt_index")
    return isinstance(attempt, int) and attempt >= MAX_ATTEMPTS


def spec_conflict_text(error: Any) -> str:
    """The conflict description without its marker; the caller escapes it."""
    text = str(error or "").removeprefix(SPEC_CONFLICT_PREFIX)
    return text.lstrip(" :-—").strip()


def spec_conflict_keyboard(task_id: int, *, confirming: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if confirming:
        builder.button(
            text="Ha, qayta ishga tushir",
            callback_data=SpecRerunAction(action="yes", task_id=task_id),
        )
        builder.button(
            text="Orqaga", callback_data=SpecRerunAction(action="back", task_id=task_id)
        )
    else:
        builder.button(
            text="📜 Spec bo'yicha qayta ishga tushir",
            callback_data=SpecRerunAction(action="ask", task_id=task_id),
        )
    builder.adjust(1)
    return builder.as_markup()


async def _set_markup(query: CallbackQuery, markup: InlineKeyboardMarkup | None) -> None:
    """Best effort: a failed cosmetic edit must never look like a failed action."""
    message = editable(query)
    if message is None:
        return
    try:
        await message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            log.warning("spec_rerun_markup_edit_failed", error=str(error)[:200])


@router.callback_query(SpecRerunAction.filter())
async def spec_rerun_action(query: CallbackQuery, callback_data: SpecRerunAction) -> None:
    message = editable(query)
    if message is None:
        await query.answer(
            "Bu karta eskirgan. Yangi kartani kuting yoki vazifani qayta oching.",
            show_alert=True,
        )
        return
    if message.chat.type != "private":
        await query.answer(
            "Bu amal faqat egasining shaxsiy chatida ishlaydi.", show_alert=True
        )
        return
    if callback_data.action not in {"ask", "yes", "back"}:
        await query.answer("Noma’lum amal.", show_alert=True)
        return
    api = api_for(query)
    task_id = callback_data.task_id
    try:
        actor = await api.me()
        if not actor.get("is_owner"):
            await query.answer("Bu amal faqat egasi uchun.", show_alert=True)
            return
        if callback_data.action == "back":
            await query.answer()
            await _set_markup(query, spec_conflict_keyboard(task_id))
            return
        if callback_data.action == "ask":
            await query.answer()
            await _set_markup(query, spec_conflict_keyboard(task_id, confirming=True))
            return
        # Only the newest run of the task may be re-run, and only while it is
        # still the SPEC_CONFLICT failure this card was drawn for.
        runs = await api.agent_runs(task_id)
        latest = runs[0] if runs else None
        if latest is None or not is_spec_conflict(latest):
            await query.answer(ALREADY_HANDLED_TEXT, show_alert=True)
            await _set_markup(query, None)
            return
        if attempts_exhausted(latest):
            await query.answer(LIMIT_REACHED_TEXT, show_alert=True)
            await _set_markup(query, None)
            return
        await api.start_agent_run(task_id, owner_note=SPEC_CONFIRMED_NOTE)
    except ApiError as error:
        if error.status == 409:
            await query.answer(ALREADY_HANDLED_TEXT, show_alert=True)
            await _set_markup(query, None)
            return
        if error.status == 403:
            text = "Bu amal faqat egasi uchun."
        else:
            text = "Qayta ishga tushirib bo‘lmadi. Birozdan so‘ng qayta urinib ko‘ring."
        await query.answer(text[:200], show_alert=True)
        return
    await query.answer("Agent qayta ishga tushirildi.")
    await _set_markup(query, None)
