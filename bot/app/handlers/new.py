"""The guided /new flow.

Quick capture is faster when you know the syntax; this exists for when you do
not, or when you are picking an assignee you cannot spell. One message, edited in
place at every step, so the chat does not fill up with five half-finished forms.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.api import ApiError
from app.callbacks import NewTaskChoice
from app.config import settings
from app.handlers.helpers import (
    api_for,
    editable,
    explain_api_error,
    notify_assignee,
    send_card,
)
from app.parsing import DEFAULT_DUE_TIME
from app.states import NewTask
from app.texts import PRIORITY_LABEL

router = Router(name="new")

_PRIORITIES = ["urgent", "high", "normal", "low"]


@router.message(Command("new"))
async def start_new(message: Message, state: FSMContext) -> None:
    api = api_for(message)
    try:
        projects = await api.projects()
    except ApiError as error:
        await explain_api_error(message, error)
        return

    if not projects:
        await message.answer("Sizga hali loyiha biriktirilmagan.")
        return

    await state.clear()
    await state.set_state(NewTask.project)

    builder = InlineKeyboardBuilder()
    for project in projects:
        builder.button(
            text=project["name"],
            callback_data=NewTaskChoice(field="project", value=str(project["id"])),
        )
    builder.adjust(2)
    await message.answer("Qaysi loyiha?", reply_markup=builder.as_markup())


@router.callback_query(NewTask.project, NewTaskChoice.filter(F.field == "project"))
async def chose_project(
    query: CallbackQuery, callback_data: NewTaskChoice, state: FSMContext
) -> None:
    await state.update_data(project_id=int(callback_data.value))
    await state.set_state(NewTask.title)
    await query.answer()
    message = editable(query)
    if message:
        await message.edit_text("Vazifa matnini yozing:")


@router.message(NewTask.title, F.text)
async def got_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer("Matn bo‘sh. Qaytadan yozing:")
        return

    await state.update_data(title=title)
    await state.set_state(NewTask.assignee)

    api = api_for(message)
    try:
        users = await api.users()
    except ApiError as error:
        await explain_api_error(message, error)
        return

    builder = InlineKeyboardBuilder()
    for user in users:
        builder.button(
            text=user["full_name"],
            callback_data=NewTaskChoice(field="assignee", value=str(user["id"])),
        )
    builder.button(
        text="— biriktirmasdan —", callback_data=NewTaskChoice(field="assignee", value="0")
    )
    builder.adjust(1)
    await message.answer("Kimga biriktiramiz?", reply_markup=builder.as_markup())


@router.callback_query(NewTask.assignee, NewTaskChoice.filter(F.field == "assignee"))
async def chose_assignee(
    query: CallbackQuery, callback_data: NewTaskChoice, state: FSMContext
) -> None:
    assignee_id = int(callback_data.value)
    await state.update_data(assignee_id=assignee_id or None)
    await state.set_state(NewTask.priority)
    await query.answer()

    builder = InlineKeyboardBuilder()
    for priority in _PRIORITIES:
        builder.button(
            text=PRIORITY_LABEL[priority],
            callback_data=NewTaskChoice(field="priority", value=priority),
        )
    builder.adjust(2)
    message = editable(query)
    if message:
        await message.edit_text("Muhimligi?", reply_markup=builder.as_markup())


@router.callback_query(NewTask.priority, NewTaskChoice.filter(F.field == "priority"))
async def chose_priority(
    query: CallbackQuery, callback_data: NewTaskChoice, state: FSMContext
) -> None:
    await state.update_data(priority=callback_data.value)
    await state.set_state(NewTask.due)
    await query.answer()

    builder = InlineKeyboardBuilder()
    for label, value in (
        ("Bugun", "today"),
        ("Ertaga", "tomorrow"),
        ("3 kun", "+3"),
        ("Bir hafta", "+7"),
        ("Muddatsiz", "none"),
    ):
        builder.button(text=label, callback_data=NewTaskChoice(field="due", value=value))
    builder.adjust(2)
    message = editable(query)
    if message:
        await message.edit_text("Muddati?", reply_markup=builder.as_markup())


def _due_from_choice(value: str) -> str | None:
    """Resolve a button to a UTC timestamp, in the team's timezone."""
    if value == "none":
        return None
    zone = ZoneInfo(settings.timezone)
    today = datetime.now(zone).date()
    days = {"today": 0, "tomorrow": 1}.get(value)
    if days is None:
        days = int(value.lstrip("+"))
    moment = datetime.combine(today + timedelta(days=days), DEFAULT_DUE_TIME, tzinfo=zone)
    return moment.astimezone(UTC).isoformat()


@router.callback_query(NewTask.due, NewTaskChoice.filter(F.field == "due"))
async def chose_due(
    query: CallbackQuery, callback_data: NewTaskChoice, state: FSMContext, bot: Bot
) -> None:
    data = await state.get_data()
    payload = {
        "project_id": data["project_id"],
        "title": data["title"],
        "priority": data.get("priority", "normal"),
        "assignee_id": data.get("assignee_id"),
        "due_at": _due_from_choice(callback_data.value),
        "source": "bot",
        "source_chat_id": query.message.chat.id if query.message else None,
    }

    api = api_for(query)
    try:
        task = await api.create_task({k: v for k, v in payload.items() if v is not None})
    except ApiError as error:
        await explain_api_error(query, error)
        return

    await state.clear()
    await query.answer("Yaratildi")
    prompt = editable(query)
    if prompt:
        await prompt.delete()
    assert query.from_user is not None
    chat_id = query.message.chat.id if query.message else query.from_user.id
    await send_card(bot, chat_id, task, api)
    await notify_assignee(bot, task, query.from_user.id, api)


@router.message(NewTask.project)
@router.message(NewTask.assignee)
@router.message(NewTask.priority)
@router.message(NewTask.due)
async def nudge(message: Message) -> None:
    """Typing where a button is expected.

    Without this the message falls through to quick capture and silently starts a
    second, competing draft.
    """
    await message.answer("Yuqoridagi tugmalardan birini tanlang yoki /cancel yozing.")
