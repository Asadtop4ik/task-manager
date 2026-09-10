"""Buttons on a task card.

Every callback is answered — an unanswered one leaves Telegram's spinner turning
on the user's phone, which reads as a hung bot. qurbot's release pass had to fix
exactly this class of bug; do not reintroduce it here.
"""

from __future__ import annotations

from datetime import UTC

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.api import ApiError
from app.callbacks import TaskAction
from app.cards import format_due
from app.config import settings
from app.handlers.helpers import api_for, editable, explain_api_error, refresh_card
from app.parsing import parse
from app.states import CardAction

router = Router(name="cards")

# Which button means which status. The API's transition table is still the
# authority; this only decides what the tap asks for.
_TRANSITIONS = {
    "start": "in_progress",
    "done": "done",
    "block": "blocked",
    "reopen": "todo",
}


@router.callback_query(TaskAction.filter(F.action.in_(_TRANSITIONS)))
async def move(query: CallbackQuery, callback_data: TaskAction) -> None:
    api = api_for(query)
    try:
        task = await api.transition(callback_data.task_id, _TRANSITIONS[callback_data.action])
    except ApiError as error:
        await explain_api_error(query, error)
        return

    await query.answer("Yangilandi")
    await refresh_card(query, task)


@router.callback_query(TaskAction.filter(F.action == "comment"))
async def ask_comment(
    query: CallbackQuery, callback_data: TaskAction, state: FSMContext
) -> None:
    await state.set_state(CardAction.comment)
    await state.update_data(task_id=callback_data.task_id)
    await query.answer()
    message = editable(query)
    if message:
        await message.reply(f"#{callback_data.task_id} uchun izohni yozing:")


@router.message(CardAction.comment, F.text)
async def save_comment(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_id = data.get("task_id")
    if task_id is None:
        await state.clear()
        return

    api = api_for(message)
    try:
        await api.comment(int(task_id), message.text or "")
    except ApiError as error:
        await explain_api_error(message, error)
        await state.clear()
        return

    await state.clear()
    await message.answer("Izoh saqlandi. ✅")


@router.callback_query(TaskAction.filter(F.action == "snooze"))
async def ask_snooze(
    query: CallbackQuery, callback_data: TaskAction, state: FSMContext
) -> None:
    await state.set_state(CardAction.snooze)
    await state.update_data(task_id=callback_data.task_id)
    await query.answer()
    message = editable(query)
    if message:
        await message.reply(
            "Yangi muddatni yozing: <code>ertaga</code>, <code>+3</code>, "
            "<code>25.12 09:00</code> yoki <code>18:00</code>"
        )


@router.message(CardAction.snooze, F.text)
async def save_snooze(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_id = data.get("task_id")
    if task_id is None:
        await state.clear()
        return

    # The same parser as quick capture, so "ertaga 18:00" means the same thing
    # everywhere in the bot rather than having a second, subtly different reader.
    parsed = parse(message.text or "", known_keys=set(), tz=settings.timezone)
    if parsed.due_at is None:
        await message.answer(
            "Muddatni tushunmadim. Masalan: <code>ertaga</code> yoki <code>25.12 09:00</code>"
        )
        return

    api = api_for(message)
    try:
        await api.set_due(int(task_id), parsed.due_at.astimezone(UTC).isoformat())
    except ApiError as error:
        await explain_api_error(message, error)
        await state.clear()
        return

    await state.clear()
    await message.answer(f"Yangi muddat: {format_due(parsed.due_at.isoformat())} ✅")


@router.callback_query(TaskAction.filter())
async def unknown_action(query: CallbackQuery) -> None:
    """Catch-all so an old card's button never leaves the spinner turning."""
    await query.answer("Bu tugma endi ishlamaydi. /my orqali yangilang.", show_alert=True)
