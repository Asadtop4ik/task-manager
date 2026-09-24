"""Quick capture: one line of text becomes a task, after confirmation.

Nothing is created straight from the parse. The bot shows what it understood and
waits — a silently mis-read deadline is worse than no parsing at all, and this is
the step that makes an aggressive parser safe.
"""

from __future__ import annotations

from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.api import ApiError, TaskApi
from app.callbacks import QuickConfirm
from app.cards import format_due
from app.handlers.agent_intake import route_intake_text
from app.handlers.helpers import (
    api_for,
    editable,
    explain_api_error,
    notify_assignee,
    send_card,
)
from app.parsing import ParsedTask, parse
from app.states import QuickCapture
from app.texts import PRIORITY_LABEL

router = Router(name="quick")


def _confirmation(
    parsed: ParsedTask, project_name: str | None, assignee_name: str | None
) -> str:
    lines = ["<b>Shu vazifa yaratilsinmi?</b>", "", f"📝 {escape(parsed.title)}"]
    lines.append(
        f"📁 {escape(project_name)}" if project_name else "📁 <i>loyiha tanlanmagan</i>"
    )
    if parsed.priority:
        lines.append(f"⚡️ {PRIORITY_LABEL.get(parsed.priority, parsed.priority)}")
    if parsed.fast_requested:
        lines.append("🤖 Codex · ⚡ PRsiz prod (!fast)")
    elif (parsed.assignee_username or "").lower() == "codex":
        lines.append("🤖 Codex ishga tushadi")
    elif assignee_name:
        lines.append(f"👤 {escape(assignee_name)}")
    elif parsed.assignee_username:
        lines.append(f"👤 <i>@{escape(parsed.assignee_username)} topilmadi</i>")
    if parsed.due_at:
        lines.append(f"🕒 {format_due(parsed.due_at.isoformat())}")
    return "\n".join(lines)


def _confirm_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Yaratish", callback_data=QuickConfirm(action="create"))
    builder.button(text="✏️ Matnni o‘zgartirish", callback_data=QuickConfirm(action="edit"))
    builder.button(text="✖️ Bekor", callback_data=QuickConfirm(action="cancel"))
    builder.adjust(1)
    return builder.as_markup()


async def _resolve(api: TaskApi, parsed: ParsedTask) -> tuple[dict | None, dict | None]:
    projects = await api.projects()
    project = next((p for p in projects if p["key"] == parsed.project_key), None)
    if project is None and len(projects) == 1:
        # With one project there is nothing to disambiguate, so do not make the
        # user name it every single time.
        project = projects[0]

    assignee = None
    if parsed.assignee_username and parsed.assignee_username.lower() != "codex":
        users = await api.users()
        wanted = parsed.assignee_username.lower()
        assignee = next(
            (u for u in users if (u.get("username") or "").lower() == wanted), None
        )
    return project, assignee


async def _offer(message: Message, state: FSMContext, text: str) -> None:
    api = api_for(message)
    try:
        projects = await api.projects()
    except ApiError as error:
        await explain_api_error(message, error)
        return

    known = {p["key"] for p in projects}
    parsed = parse(text, known_keys=known)
    if parsed.fast_requested:
        try:
            actor = await api.me()
        except ApiError as error:
            await explain_api_error(message, error)
            return
        if not actor.get("is_owner"):
            await message.answer("!fast faqat jamoa egasi uchun.")
            return

    if not parsed.is_usable:
        await state.set_state(QuickCapture.editing_title)
        await state.update_data(draft=_dump(parsed))
        await message.answer("Vazifa matni yo‘q. Nima qilish kerakligini yozing:")
        return

    project, assignee = await _resolve(api, parsed)
    await state.set_state(QuickCapture.confirming)
    await state.update_data(
        draft=_dump(parsed),
        project_id=project["id"] if project else None,
        assignee_id=assignee["id"] if assignee else None,
    )
    await message.answer(
        _confirmation(
            parsed,
            project["name"] if project else None,
            assignee["full_name"] if assignee else None,
        ),
        reply_markup=_confirm_keyboard(),
    )


def _dump(parsed: ParsedTask) -> dict[str, Any]:
    return {
        "title": parsed.title,
        "priority": parsed.priority,
        "due_at": parsed.due_at.isoformat() if parsed.due_at else None,
        "project_key": parsed.project_key,
        "assignee_username": parsed.assignee_username,
        "fast_requested": parsed.fast_requested,
    }


@router.message(Command("task"), F.reply_to_message)
async def task_from_reply(message: Message, state: FSMContext) -> None:
    """Turn the message being replied to into a task."""
    source = message.reply_to_message
    text = (source.text or source.caption or "").strip() if source else ""
    if not text:
        await message.answer("Bu xabarda matn yo‘q.")
        return
    await _offer(message, state, text)


@router.message(QuickCapture.editing_title, F.text)
async def retype_title(message: Message, state: FSMContext) -> None:
    if await route_intake_text(message, state, message.text or ""):
        return
    data = await state.get_data()
    draft = data.get("draft", {})
    # Keep the project, priority and deadline already understood; only the words
    # are being replaced.
    prefix = f"{draft['project_key']}: " if draft.get("project_key") else ""
    await _offer(message, state, prefix + (message.text or ""))


@router.callback_query(QuickConfirm.filter(F.action == "cancel"))
async def cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer("Bekor qilindi")
    message = editable(query)
    if message:
        await message.edit_text("Bekor qilindi.")


@router.callback_query(QuickConfirm.filter(F.action == "edit"))
async def edit(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(QuickCapture.editing_title)
    await query.answer()
    message = editable(query)
    if message:
        await message.edit_text("Yangi matnni yozing:")


@router.callback_query(QuickConfirm.filter(F.action == "create"))
async def create(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    if not draft:
        await query.answer("Muddati o‘tgan — qaytadan yozing", show_alert=True)
        await state.clear()
        return

    if not data.get("project_id"):
        await query.answer("Avval loyihani ko‘rsating: masalan «keto: ...»", show_alert=True)
        return

    api = api_for(query)
    agent_requested = (draft.get("assignee_username") or "").lower() == "codex" or bool(
        draft.get("fast_requested")
    )
    assignee_id = data.get("assignee_id")
    if agent_requested:
        # An executor may delegate their own work to Codex while remaining the
        # human owner. Managers can leave the task unassigned.
        try:
            actor = await api.me()
        except ApiError as error:
            await explain_api_error(query, error)
            return
        if actor["role"] == "executor":
            assignee_id = actor["id"]

    payload = {
        "project_id": data["project_id"],
        "title": draft["title"],
        "priority": draft.get("priority") or "normal",
        "due_at": draft.get("due_at"),
        "assignee_id": assignee_id,
        "source": "bot",
        "source_chat_id": query.message.chat.id if query.message else None,
    }
    try:
        task = await api.create_task({k: v for k, v in payload.items() if v is not None})
    except ApiError as error:
        await explain_api_error(query, error)
        return

    await state.clear()
    await query.answer("Yaratildi")
    prompt = editable(query)
    if prompt:
        # The confirmation prompt is replaced by the card, not stacked above it.
        await prompt.delete()
    assert query.from_user is not None
    chat_id = query.message.chat.id if query.message else query.from_user.id
    await send_card(bot, chat_id, task, api)
    await notify_assignee(bot, task, query.from_user.id, api)
    if agent_requested:
        try:
            if draft.get("fast_requested"):
                run = await api.start_agent_run(task["id"], mode="fast")
            else:
                run = await api.start_agent_run(task["id"])
        except ApiError as error:
            await bot.send_message(
                chat_id,
                f"#{task['id']} yaratildi. Codex ishga tushmadi: {escape(error.detail)}",
            )
        else:
            await bot.send_message(
                chat_id, f"🤖 #{task['id']} Codexga yuborildi ({run['status']})."
            )


@router.message(Command("agent"))
async def delegate_existing(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Masalan: /agent 42")
        return
    task_id = int(parts[1])
    api = api_for(message)
    try:
        run = await api.start_agent_run(task_id)
    except ApiError as error:
        await explain_api_error(message, error)
        return
    await message.answer(f"🤖 #{task_id} Codexga yuborildi ({run['status']}).")


@router.message(Command("stopagent"))
async def stop_agent(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Masalan: /stopagent 42")
        return
    task_id = int(parts[1])
    api = api_for(message)
    try:
        runs = await api.agent_runs(task_id)
        active = next(
            (
                r
                for r in runs
                if r["status"]
                in {
                    "pending",
                    "dispatching",
                    "dispatched",
                    "running",
                    "validating",
                    "pr_ready",
                }
            ),
            None,
        )
        if active is None:
            await message.answer(f"#{task_id} uchun faol Codex ishi yo‘q.")
            return
        await api.cancel_agent_run(active["run_id"])
    except ApiError as error:
        await explain_api_error(message, error)
        return
    await message.answer(f"⏹ #{task_id} Codex ishi to‘xtatildi.")


# Registered last in this router so every command and FSM state above wins first.
@router.message(F.text & ~F.text.startswith("/"))
async def quick_capture(message: Message, state: FSMContext) -> None:
    if await route_intake_text(message, state, message.text or ""):
        return
    await _offer(message, state, message.text or "")
