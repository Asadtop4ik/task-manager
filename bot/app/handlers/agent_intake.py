"""Private-chat intake flow for Codex tasks with optional reference images."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import escape
from pathlib import PurePath
from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.api import ApiError
from app.callbacks import AgentIntakeAction
from app.config import settings
from app.handlers.helpers import (
    api_for,
    editable,
    explain_api_error,
    notify_assignee,
    send_card,
)
from app.parsing import parse
from app.states import AgentIntake

router = Router(name="agent_intake")

MAX_IMAGES = 3
MAX_IMAGE_BYTES = 20 * 1024 * 1024
PHOTO_DRAFT_SECONDS = 10 * 60
_MIME_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_ALLOWED_MIME_TYPES = frozenset(_MIME_BY_EXTENSION.values())


def _private(message: Message) -> bool:
    return message.chat.type == "private"


def _photos_active(data: dict[str, Any]) -> bool:
    created_at = data.get("photo_draft_created_at")
    return bool(
        created_at and datetime.now(UTC).timestamp() - float(created_at) <= PHOTO_DRAFT_SECONDS
    )


def _image_payload(message: Message) -> tuple[dict[str, Any] | None, str | None]:
    if message.photo:
        photo = message.photo[-1]
        mime_type = "image/jpeg"  # Telegram photo uploads are delivered as JPEG.
        size = photo.file_size
        file_id = photo.file_id
    elif message.document:
        document = message.document
        mime_type = (document.mime_type or "").lower()
        if mime_type not in _ALLOWED_MIME_TYPES:
            mime_type = _MIME_BY_EXTENSION.get(
                PurePath(document.file_name or "").suffix.lower(), ""
            )
        size = document.file_size
        file_id = document.file_id
    else:
        return None, "PNG, JPEG yoki WebP rasm yuboring."

    if mime_type not in _ALLOWED_MIME_TYPES:
        return None, "Faqat PNG, JPEG yoki WebP rasmlar qabul qilinadi."
    if size is not None and size > MAX_IMAGE_BYTES:
        return None, "Rasm 20 MB dan katta. Kichikroq rasm yuboring."
    return {"file_id": file_id, "mime": mime_type, "size": size}, None


def _action_keyboard(intake_id: int, *actions: tuple[str, str]):
    builder = InlineKeyboardBuilder()
    for label, action in actions:
        builder.button(
            text=label,
            callback_data=AgentIntakeAction(action=action, intake_id=intake_id),
        )
    builder.adjust(1)
    return builder.as_markup()


def notification_message(notice: dict[str, Any]) -> tuple[str, Any | None]:
    """Format one durable intake notice; returns text and its optional keyboard."""
    intake_id = int(notice["id"])
    status = notice["status"]
    title = escape(str(notice.get("title") or "Codex vazifasi")[:180])
    brief_data = notice.get("brief") or {}
    brief = brief_data if isinstance(brief_data, dict) else {}

    if status == "needs_answers":
        questions = notice.get("questions") or []
        lines = [f"🤔 <b>{title}</b>", ""]
        lines.append("Quyidagi savollarga javob bering:")
        lines.extend(
            f"{index}. {escape(str(question)[:500])}"
            for index, question in enumerate(questions[:3], 1)
        )
        return "\n".join(lines), _action_keyboard(intake_id, ("✖️ Bekor qilish", "cancel"))

    if status == "ready":
        mode = "!fast · PRsiz" if notice.get("mode") == "fast" else "PR ochiladi"
        lines = ["🧭 <b>Task xulosasi</b>", "", f"<b>{title}</b>"]
        if brief.get("goal"):
            lines.append(f"Maqsad: {escape(str(brief['goal'])[:800])}")
        acceptance = brief.get("acceptance") or []
        if acceptance:
            lines.append("Qabul mezonlari:")
            lines.extend(f"• {escape(str(item)[:250])}" for item in acceptance[:5])
        assumptions = brief.get("assumptions") or []
        if assumptions:
            lines.append("Taxminlar:")
            lines.extend(f"• {escape(str(item)[:150])}" for item in assumptions[:5])
        lines.extend(["", f"Rejim: {mode}"])
        text = "\n".join(lines)
        return text, _action_keyboard(
            intake_id,
            ("✅ Bajarish", "confirm"),
            ("✏️ Tuzatish", "edit"),
            ("✖️ Bekor qilish", "cancel"),
        )

    if status == "failed":
        detail = escape(str(notice.get("error") or "Codex intake xatosi")[:500])
        return (
            f"⚠️ <b>{title}</b>\n{detail}",
            _action_keyboard(
                intake_id,
                ("🔁 Qayta urinish", "retry"),
                ("↪️ PR bilan davom etish", "fallback"),
                ("✖️ Bekor qilish", "cancel"),
            ),
        )

    if status in {"queued", "analyzing"}:
        label = "navbatda" if status == "queued" else "ko‘rib chiqyapti"
        return f"🧭 {title}: Codex {label}.", None
    if status == "confirmed":
        return f"✅ {title}: task tasdiqlandi.", None
    if status == "cancelled":
        return f"✖️ {title}: intake bekor qilindi.", None
    return f"ℹ️ {title}: holat yangilandi.", None


async def _create_intake(
    message: Message,
    state: FSMContext,
    text: str,
    images: list[dict[str, Any]],
) -> bool:
    """Create only the intake record; task creation waits for its confirm button."""
    api = api_for(message)
    try:
        projects = await api.projects()
    except ApiError as error:
        await explain_api_error(message, error)
        return True

    project_keys = {str(project["key"]) for project in projects}
    parsed = parse(text, known_keys=project_keys)
    is_fast = parsed.fast_requested
    is_codex = (parsed.assignee_username or "").lower() == "codex"
    if not (is_fast or is_codex):
        await message.answer(
            "Codex taski uchun matnni <code>@codex</code> yoki <code>!fast</code> bilan boshlang."
        )
        return True
    if not parsed.is_usable:
        await message.answer("Codex nima qilishi kerakligini ham yozing.")
        return True

    project = next((row for row in projects if row["key"] == parsed.project_key), None)
    if project is None and len(projects) == 1:
        project = projects[0]
    if project is None:
        await message.answer(
            "Loyihani ko‘rsating, masalan: <code>task-manager: @codex ...</code>."
        )
        return True

    if is_fast:
        try:
            actor = await api.me()
        except ApiError as error:
            await explain_api_error(message, error)
            return True
        if not actor.get("is_owner"):
            await message.answer("!fast faqat jamoa egasi uchun.")
            return True

    try:
        await api.create_agent_intake(
            {
                "project_id": project["id"],
                "text": text,
                "mode": "fast" if is_fast else "pr",
                "chat_id": message.chat.id,
                "images": images,
            }
        )
    except ApiError as error:
        await explain_api_error(message, error)
        return True

    await state.clear()
    await message.answer(
        "🧭 Codex taskni ko‘rib chiqyapti. Xulosa tayyor bo‘lganda yuboraman."
    )
    return True


@router.message(F.photo)
@router.message(F.document)
async def receive_image(message: Message, state: FSMContext) -> None:
    if not _private(message):
        return
    if not settings.agent_intake_enabled:
        await message.answer("Rasmli Codex intake hozircha yoqilmagan.")
        return

    api = api_for(message)
    try:
        actor = await api.me()
        current = await api.current_agent_intake()
    except ApiError as error:
        await explain_api_error(message, error)
        return
    if not (actor.get("can_use_codex") or actor.get("is_owner")):
        await message.answer("Rasmli Codex taski uchun sizga Codex huquqi kerak.")
        return
    if current:
        await message.answer("Avval faol Codex xulosasini tasdiqlang yoki /cancel yozing.")
        return

    image, image_error = _image_payload(message)
    if image_error:
        await message.answer(image_error)
        return
    assert image is not None

    data = await state.get_data()
    now = datetime.now(UTC).timestamp()
    images = list(data.get("photo_draft_images", [])) if _photos_active(data) else []
    if len(images) >= MAX_IMAGES:
        await message.answer("Bitta Codex taskiga ko‘pi bilan 3 ta rasm qo‘shish mumkin.")
        return
    images.append(image)
    text_parts = list(data.get("photo_draft_text_parts", [])) if _photos_active(data) else []
    caption = (message.caption or "").strip()
    if caption:
        text_parts.append(caption)
    await state.set_state(AgentIntake.photo_pending)
    await state.update_data(
        photo_draft_created_at=(
            data.get("photo_draft_created_at") if _photos_active(data) else now
        ),
        photo_draft_images=images,
        photo_draft_text_parts=text_parts,
        photo_draft_media_group_id=message.media_group_id,
    )

    if message.media_group_id:
        await message.answer(
            "Rasmlar qabul qilindi. Endi <code>@codex</code> yoki <code>!fast</code> bilan task matnini yuboring."
        )
        return
    if caption:
        await _create_intake(message, state, caption, images)
        return
    await message.answer(
        "Rasm draftda saqlandi. 10 daqiqa ichida <code>@codex ...</code> yoki <code>!fast ...</code> bilan task matnini yuboring."
    )


async def route_intake_text(message: Message, state: FSMContext, text: str) -> bool:
    """Route private text to a pending photo draft or active backend intake."""
    if not settings.agent_intake_enabled or not _private(message):
        return False

    data = await state.get_data()
    draft_images = list(data.get("photo_draft_images", []))
    draft_active = bool(draft_images and _photos_active(data))
    if draft_images and not draft_active:
        await state.clear()
        await message.answer(
            "Rasm draftining 10 daqiqalik muddati o‘tdi; rasmni qayta yuboring."
        )
        data = {}
        draft_images = []

    api = api_for(message)
    try:
        current = await api.current_agent_intake()
    except ApiError as error:
        await explain_api_error(message, error)
        return True

    if current:
        intake_id = int(current["id"])
        status = current["status"]
        try:
            if status == "needs_answers":
                await api.answer_agent_intake(intake_id, text)
                await message.answer("Javob qabul qilindi. Codex xulosani yangilaydi.")
                return True
            if status == "ready":
                await message.answer(
                    "Xulosani o‘zgartirish uchun undagi ✏️ Tuzatish tugmasini bosing; "
                    "yoki ✅ Bajarish bilan tasdiqlang."
                )
                return True
        except ApiError as error:
            await explain_api_error(message, error)
            return True
        if status in {"queued", "analyzing"}:
            await message.answer(
                "Codex taskni ko‘rib chiqyapti. Natija tayyor bo‘lishini kuting."
            )
            return True
        if status == "failed":
            await message.answer(
                "Codex taski to‘xtagan. Xabardagi Qayta urinish yoki PR tugmasini bosing."
            )
            return True

    if draft_active:
        full_text = "\n".join([text, *data.get("photo_draft_text_parts", [])]).strip()
        return await _create_intake(message, state, full_text, draft_images)

    return (
        await _create_intake(message, state, text, []) if _has_agent_directive(text) else False
    )


def _has_agent_directive(text: str) -> bool:
    return bool(re.search(r"(?<!\w)@codex\b|(?:^|\s)!fast\b", text, re.IGNORECASE))


@router.callback_query(AgentIntakeAction.filter(F.action == "edit"))
async def edit_intake(
    query: CallbackQuery, callback_data: AgentIntakeAction, state: FSMContext
) -> None:
    await state.set_state(AgentIntake.revising)
    await state.update_data(agent_intake_id=callback_data.intake_id)
    await query.answer()
    prompt = editable(query)
    if prompt:
        await prompt.edit_text("Yangi xulosa matnini yozing:")


@router.message(AgentIntake.revising, F.text)
async def revise_from_button(message: Message, state: FSMContext) -> None:
    if not _private(message):
        return
    data = await state.get_data()
    intake_id = data.get("agent_intake_id")
    if not intake_id:
        await state.clear()
        await message.answer("Intake topilmadi. Taskni qaytadan yuboring.")
        return
    api = api_for(message)
    try:
        current = await api.current_agent_intake()
        if not current or int(current["id"]) != int(intake_id) or current["status"] != "ready":
            await state.clear()
            await message.answer("Intake endi tahrirlanmaydi. Yangi taskni qaytadan yuboring.")
            return
        await api.revise_agent_intake(int(intake_id), message.text or "")
    except ApiError as error:
        await explain_api_error(message, error)
        return
    await state.clear()
    await message.answer("Tuzatish qabul qilindi. Yangilangan xulosani yuboraman.")


@router.callback_query(AgentIntakeAction.filter(F.action == "cancel"))
async def cancel_intake(
    query: CallbackQuery, callback_data: AgentIntakeAction, state: FSMContext
) -> None:
    api = api_for(query)
    try:
        await api.cancel_agent_intake(callback_data.intake_id)
    except ApiError as error:
        await explain_api_error(query, error)
        return
    await state.clear()
    await query.answer("Bekor qilindi")
    prompt = editable(query)
    if prompt:
        await prompt.edit_text("Codex intake bekor qilindi.")


@router.callback_query(AgentIntakeAction.filter(F.action == "retry"))
async def retry_intake(query: CallbackQuery, callback_data: AgentIntakeAction) -> None:
    api = api_for(query)
    try:
        await api.retry_agent_intake(callback_data.intake_id)
    except ApiError as error:
        await explain_api_error(query, error)
        return
    await query.answer("Qayta ishga tushdi")
    prompt = editable(query)
    if prompt:
        await prompt.edit_text("🔁 Codex taskini qayta ko‘rib chiqyapti.")


async def _confirm(
    query: CallbackQuery,
    intake_id: int,
    state: FSMContext,
    bot: Bot,
    *,
    fallback_pr: bool,
) -> None:
    api = api_for(query)
    try:
        result = await api.confirm_agent_intake(intake_id, fallback_pr=fallback_pr)
    except ApiError as error:
        await explain_api_error(query, error)
        return

    await state.clear()
    if not result.get("created", False):
        await query.answer("Bu intake oldin tasdiqlangan")
        prompt = editable(query)
        if prompt:
            await prompt.edit_text("Task oldin yaratilgan.")
        return

    task = result["task"]
    mode = result.get("mode", "pr")
    await query.answer("Task yaratildi")
    prompt = editable(query)
    if prompt:
        await prompt.delete()
    assert query.from_user is not None
    chat_id = query.message.chat.id if query.message else query.from_user.id
    await send_card(bot, chat_id, task, api)
    await notify_assignee(bot, task, query.from_user.id, api)
    try:
        run = await api.start_agent_run(task["id"], mode=mode)
    except ApiError as error:
        await bot.send_message(
            chat_id,
            f"#{task['id']} yaratildi. Codex ishga tushmadi: {escape(error.detail)}",
        )
    else:
        await bot.send_message(
            chat_id, f"🤖 #{task['id']} Codexga yuborildi ({run['status']})."
        )


@router.callback_query(AgentIntakeAction.filter(F.action == "confirm"))
async def confirm_intake(
    query: CallbackQuery,
    callback_data: AgentIntakeAction,
    state: FSMContext,
    bot: Bot,
) -> None:
    await _confirm(query, callback_data.intake_id, state, bot, fallback_pr=False)


@router.callback_query(AgentIntakeAction.filter(F.action == "fallback"))
async def fallback_to_pr(
    query: CallbackQuery,
    callback_data: AgentIntakeAction,
    state: FSMContext,
    bot: Bot,
) -> None:
    await _confirm(query, callback_data.intake_id, state, bot, fallback_pr=True)
