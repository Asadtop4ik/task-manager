"""Invite-only onboarding and browser magic links in the owner's bot chat."""

from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.api import ApiError
from app.callbacks import JoinAction
from app.handlers.helpers import api_for, editable, explain_api_error
from app.logging import get_logger
from app.states import JoinApproval
from app.texts import HELP

log = get_logger(__name__)
router = Router(name="team")


def _request_keyboard(request_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Tasdiqlash",
        callback_data=JoinAction(action="approve", request_id=request_id),
    )
    builder.button(
        text="❌ Rad etish", callback_data=JoinAction(action="reject", request_id=request_id)
    )
    builder.adjust(2)
    return builder.as_markup()


def _projects_keyboard(request_id: int, projects: list[dict], selected: set[int]):
    builder = InlineKeyboardBuilder()
    for project in projects:
        project_id = int(project["id"])
        marker = "✅" if project_id in selected else "▫️"
        builder.button(
            text=f"{marker} {project['name']}",
            callback_data=JoinAction(
                action="project", request_id=request_id, project_id=project_id
            ),
        )
    builder.button(
        text="✅ Yakunlash", callback_data=JoinAction(action="confirm", request_id=request_id)
    )
    builder.button(
        text="↩️ Keyin", callback_data=JoinAction(action="cancel", request_id=request_id)
    )
    builder.adjust(1)
    return builder.as_markup()


async def _notify_owner(bot: Bot, row: dict) -> None:
    owner_chat_id = row.get("owner_chat_id")
    if owner_chat_id is None:
        return
    username = f" (@{escape(row['username'])})" if row.get("username") else ""
    await bot.send_message(
        owner_chat_id,
        f"👤 Yangi a’zo kirishni so‘radi: <b>{escape(row['full_name'])}</b>{username}\n"
        f"Telegram ID: <code>{row['telegram_id']}</code>",
        reply_markup=_request_keyboard(row["id"]),
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    if message.chat.type != "private" or message.from_user is None:
        await message.answer("Botdan shaxsiy chatda foydalaning.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1] == "login":
        await login(message)
        return
    invite_token = parts[1].removeprefix("invite_") if len(parts) > 1 else ""
    if len(parts) > 1 and parts[1].startswith("invite_"):
        user = message.from_user
        try:
            row = await api_for(message).join_request(
                {
                    "invite_token": invite_token,
                    "telegram_id": user.id,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "username": user.username,
                }
            )
        except ApiError as error:
            if error.status == 410:
                await message.answer("Taklif havolasi ishlatilgan yoki muddati tugagan.")
            else:
                await explain_api_error(message, error)
            return
        if row["status"] == "active":
            await message.answer(f"Salom, {escape(row['full_name'])}!\n\n{HELP}")
        elif row["status"] == "pending":
            if row.get("notify_owner"):
                try:
                    await _notify_owner(bot, row)
                except (TelegramAPIError, OSError) as exc:
                    log.error("owner_join_notice_failed", request_id=row["id"], error=str(exc))
            await message.answer("So‘rovingiz egasiga yuborildi. Tasdiqlashni kuting.")
        else:
            await message.answer("So‘rov rad etilgan. Yangi taklif kerak.")
        return
    try:
        me = await api_for(message).me()
    except ApiError as error:
        if error.is_unknown_user:
            await message.answer("Kirish uchun jamoa egasidan taklif havolasini oling.")
        elif error.is_pending_approval:
            await message.answer("Hisobingiz tasdiqlanishini kuting.")
        else:
            await explain_api_error(message, error)
        return
    await message.answer(f"Salom, {escape(me['full_name'])}!\n\n{HELP}")


@router.message(Command("invite"))
async def invite(message: Message) -> None:
    if message.chat.type != "private":
        await message.answer("Taklifni shaxsiy chatda yarating.")
        return
    try:
        result = await api_for(message).create_invite()
    except ApiError as error:
        await explain_api_error(message, error)
        return
    await message.answer(
        "Bu taklif bir odam uchun va 7 kun amal qiladi. Sherigingizga yuboring:\n"
        f"{escape(result['url'])}",
        disable_web_page_preview=True,
    )


@router.message(Command("login"))
async def login(message: Message) -> None:
    if message.chat.type != "private":
        await message.answer("Login havolasini shaxsiy chatda oling.")
        return
    try:
        result = await api_for(message).magic_link()
    except ApiError as error:
        await explain_api_error(message, error)
        return
    await message.answer(
        "Brauzerda oching. Havola 5 daqiqa ichida bir marta ishlaydi:\n"
        f"{escape(result['url'])}",
        disable_web_page_preview=True,
    )


@router.message(Command("pending"))
async def pending(message: Message, bot: Bot) -> None:
    if message.chat.type != "private":
        await message.answer("So‘rovlarni shaxsiy chatda ko‘ring.")
        return
    try:
        rows = await api_for(message).pending_join_requests()
    except ApiError as error:
        await explain_api_error(message, error)
        return
    if not rows:
        await message.answer("Kutilayotgan a’zo yo‘q.")
        return
    for row in rows:
        await bot.send_message(
            message.chat.id,
            f"👤 <b>{escape(row['full_name'])}</b> — tasdiqlash kutilmoqda",
            reply_markup=_request_keyboard(row["id"]),
        )


@router.callback_query(JoinAction.filter(F.action == "approve"))
async def choose_projects(
    query: CallbackQuery, callback_data: JoinAction, state: FSMContext
) -> None:
    try:
        projects = await api_for(query).projects()
        pending_rows = await api_for(query).pending_join_requests()
    except ApiError as error:
        await explain_api_error(query, error)
        return
    if not any(row["id"] == callback_data.request_id for row in pending_rows):
        await query.answer("So‘rov allaqachon ko‘rib chiqilgan.", show_alert=True)
        return
    await state.set_state(JoinApproval.selecting_projects)
    await state.update_data(request_id=callback_data.request_id, selected=[])
    await query.answer()
    message = editable(query)
    if message:
        await message.edit_text(
            "A’zoga ko‘rinadigan loyihalarni tanlang:",
            reply_markup=_projects_keyboard(callback_data.request_id, projects, set()),
        )


@router.callback_query(JoinAction.filter(F.action == "project"))
async def toggle_project(
    query: CallbackQuery, callback_data: JoinAction, state: FSMContext
) -> None:
    data = await state.get_data()
    if data.get("request_id") != callback_data.request_id:
        await query.answer("Tanlov eskirgan. /pending bilan qayta oching.", show_alert=True)
        return
    selected = set(data.get("selected", []))
    if callback_data.project_id in selected:
        selected.remove(callback_data.project_id)
    else:
        selected.add(callback_data.project_id)
    await state.update_data(selected=sorted(selected))
    try:
        projects = await api_for(query).projects()
    except ApiError as error:
        await explain_api_error(query, error)
        return
    await query.answer()
    message = editable(query)
    if message:
        await message.edit_reply_markup(
            reply_markup=_projects_keyboard(callback_data.request_id, projects, selected)
        )


@router.callback_query(JoinAction.filter(F.action == "confirm"))
async def approve(
    query: CallbackQuery, callback_data: JoinAction, state: FSMContext, bot: Bot
) -> None:
    data = await state.get_data()
    if data.get("request_id") != callback_data.request_id:
        await query.answer("Tanlov eskirgan. /pending bilan qayta oching.", show_alert=True)
        return
    selected = data.get("selected", [])
    if not selected:
        await query.answer("Kamida bitta loyiha tanlang.", show_alert=True)
        return
    try:
        row = await api_for(query).decide_join_request(
            callback_data.request_id, "approve", selected
        )
    except ApiError as error:
        await explain_api_error(query, error)
        return
    if row["status"] != "approved":
        await query.answer("So‘rov allaqachon rad etilgan.", show_alert=True)
        await state.clear()
        return
    await state.clear()
    await query.answer("Tasdiqlandi")
    message = editable(query)
    if message:
        await message.edit_text(f"✅ {escape(row['full_name'])} tasdiqlandi.")
    try:
        await bot.send_message(
            row["telegram_id"],
            "✅ Hisobingiz tasdiqlandi. Brauzerga kirish uchun /login yozing.",
        )
    except (TelegramAPIError, OSError):
        log.warning("join_approval_notice_failed", request_id=row["id"])


@router.callback_query(JoinAction.filter(F.action == "reject"))
async def reject(query: CallbackQuery, callback_data: JoinAction, bot: Bot) -> None:
    try:
        row = await api_for(query).decide_join_request(callback_data.request_id, "reject")
    except ApiError as error:
        await explain_api_error(query, error)
        return
    if row["status"] != "rejected":
        await query.answer("So‘rov allaqachon tasdiqlangan.", show_alert=True)
        return
    await query.answer("Rad etildi")
    message = editable(query)
    if message:
        await message.edit_text(f"❌ {escape(row['full_name'])} rad etildi.")
    try:
        await bot.send_message(row["telegram_id"], "So‘rovingiz rad etildi.")
    except (TelegramAPIError, OSError):
        log.warning("join_rejection_notice_failed", request_id=row["id"])


@router.callback_query(JoinAction.filter(F.action == "cancel"))
async def cancel_selection(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer("Keyin /pending orqali davom eting")
