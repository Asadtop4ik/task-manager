"""Telegram-first, persistent read-only conversation about one project."""

from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.api import ApiError
from app.callbacks import DiscussionAction
from app.handlers.helpers import api_for, editable, explain_api_error
from app.states import ProjectDiscussionState

router = Router(name="project_discussion")


def discussion_keyboard(discussion_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📝 Vazifa qilish",
        callback_data=DiscussionAction(action="task", discussion_id=discussion_id),
    )
    builder.button(
        text="🔄 Yangi suhbat",
        callback_data=DiscussionAction(action="reset", discussion_id=discussion_id),
    )
    builder.adjust(1)
    return builder.as_markup()


@router.message(Command("suhbat"))
async def start_discussion(message: Message, state: FSMContext) -> None:
    if message.chat.type != "private":
        return
    api = api_for(message)
    try:
        projects = await api.projects()
        current_intake = await api.current_agent_intake()
    except ApiError as error:
        await explain_api_error(message, error)
        return
    if current_intake:
        await message.answer("Avval ochiq Codex task xulosasini tasdiqlang yoki bekor qiling.")
        return
    key = (message.text or "").split(maxsplit=1)
    requested = key[1].strip().lower() if len(key) > 1 else ""
    project = next((row for row in projects if row["key"].lower() == requested), None)
    if project is None:
        choices = ", ".join(escape(row["key"]) for row in projects)
        await message.answer(
            f"Loyihani tanlang: <code>/suhbat qurbot</code> kabi yozing. "
            f"Sizdagi loyihalar: {choices or 'yo‘q'}."
        )
        return
    try:
        discussion = await api.start_discussion(project["id"], message.chat.id)
    except ApiError as error:
        await explain_api_error(message, error)
        return
    await state.set_state(ProjectDiscussionState.active)
    await state.update_data(
        discussion_id=discussion["id"], discussion_project_key=project["key"]
    )
    resumed = bool(discussion["messages"])
    await message.answer(
        f"💬 <b>{escape(project['name'])}</b> haqida "
        f"{'oldingi suhbat davom etadi' if resumed else 'suhbat boshlandi'}. "
        "Savolingizni yoki rasmni yuboring. Vazifa qilmoqchi bo‘lsangiz, "
        "javob ostidagi tugmani bosing. /cancel suhbatdan chiqadi."
    )


async def route_discussion_text(message: Message, state: FSMContext, text: str) -> bool:
    if (
        message.chat.type != "private"
        or await state.get_state() != ProjectDiscussionState.active.state
    ):
        return False
    data = await state.get_data()
    discussion_id = data.get("discussion_id")
    if not isinstance(discussion_id, int):
        await state.clear()
        return False
    try:
        await api_for(message).discussion_message(discussion_id, text)
    except ApiError as error:
        await explain_api_error(message, error)
        return True
    await message.answer("🔎 Loyihani ko‘rib, javob tayyorlayapman.")
    return True


async def route_discussion_image(message: Message, state: FSMContext) -> bool:
    if (
        message.chat.type != "private"
        or await state.get_state() != ProjectDiscussionState.active.state
    ):
        return False
    from app.handlers.agent_intake import _image_payload

    image, error = _image_payload(message)
    if error:
        await message.answer(error)
        return True
    data = await state.get_data()
    discussion_id = data.get("discussion_id")
    if not isinstance(discussion_id, int) or image is None:
        await state.clear()
        return True
    try:
        await api_for(message).discussion_message(
            discussion_id, message.caption or "", [image]
        )
    except ApiError as api_error:
        await explain_api_error(message, api_error)
        return True
    await message.answer("🖼 Rasm qabul qilindi. Loyihani ko‘rib, javob tayyorlayapman.")
    return True


def _task_request(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    lines: list[str] = []
    images: list[dict[str, Any]] = []
    # The discussion may be months old. Earlier bot explanations, pasted run
    # cards and unrelated questions are not requirements for the new task.
    for item in messages[-6:]:
        role = "Men" if item.get("role") == "user" else "Codex"
        body = str(item.get("text") or "").strip()[:750]
        if body:
            lines.append(f"{role}: {body}")
        if item.get("role") == "user":
            images.extend(item.get("images") or [])
    transcript = "\n".join(lines)[-4_500:]
    request = (
        "@codex Quyidagi loyiha suhbatidagi oxirgi kelishilgan o‘zgarishni taskga "
        "aylantir. Agar bajariladigan o‘zgarish aniq bo‘lmasa, savol ber. "
        "Mijozga oid yangi qoida o‘ylab topma. Oldingi Codex javobi yoki "
        "yuborilgan xato kartasi ish productionga chiqqanini isbotlamaydi; "
        "buni repo holatidan tekshir. Oxirgi foydalanuvchi talabini ustun qo‘y.\n\n"
        "So‘nggi suhbat:\n" + transcript
    )
    return request[:12_000], images[-3:]


@router.callback_query(DiscussionAction.filter(F.action == "task"))
async def make_task(
    query: CallbackQuery, callback_data: DiscussionAction, state: FSMContext
) -> None:
    api = api_for(query)
    try:
        discussion = await api.discussion(callback_data.discussion_id)
        if discussion["status"] != "idle" or not discussion["messages"]:
            await query.answer("Avval suhbat javobini kuting.", show_alert=True)
            return
        text, images = _task_request(discussion["messages"])
        await api.create_agent_intake(
            {
                "project_id": discussion["project_id"],
                "chat_id": discussion["chat_id"],
                "mode": "pr",
                "text": text,
                "images": images,
            }
        )
    except ApiError as error:
        await query.answer(error.detail[:180], show_alert=True)
        return
    await state.clear()
    await query.answer("Task xulosasi tayyorlanadi")
    prompt = editable(query)
    if prompt:
        await prompt.answer(
            "🧭 Suhbatdan task xulosasini tayyorlayapman. Tasdiqingizgacha kod yozilmaydi."
        )


@router.callback_query(DiscussionAction.filter(F.action == "reset"))
async def reset_discussion(
    query: CallbackQuery, callback_data: DiscussionAction, state: FSMContext
) -> None:
    data = await state.get_data()
    if data.get("discussion_id") != callback_data.discussion_id:
        await query.answer(
            "Bu eski suhbat tugmasi. /suhbat bilan qayta oching.", show_alert=True
        )
        return
    try:
        await api_for(query).reset_discussion(callback_data.discussion_id)
    except ApiError as error:
        await query.answer(error.detail[:180], show_alert=True)
        return
    await query.answer("Yangi suhbat boshlandi")
    prompt = editable(query)
    if prompt:
        await prompt.answer("🔄 Oldingi suhbat konteksti tozalandi. Savolingizni yuboring.")
