"""Shared plumbing for the handlers."""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from app.api import ApiError, TaskApi
from app.cards import build_card
from app.logging import get_logger
from app.texts import NOT_REGISTERED, PENDING_APPROVAL

log = get_logger(__name__)


def editable(query: CallbackQuery) -> Message | None:
    """The message this button sits on, if it can still be edited.

    Telegram hands back an InaccessibleMessage for a callback on a message older
    than about 48 hours, and every edit on one raises. Cards for long-running
    tasks reach that age routinely, so this is a real path, not a type nicety.
    """
    message = query.message
    if message is None or isinstance(message, InaccessibleMessage):
        return None
    return message


def api_for(event: Message | CallbackQuery) -> TaskApi:
    assert event.from_user is not None
    return TaskApi(event.from_user.id)


async def explain_api_error(event: Message | CallbackQuery, error: ApiError) -> None:
    """Turn an API refusal into something a person can act on.

    "Not registered" and "waiting for approval" are the two an ordinary user will
    actually hit, and they need different advice; everything else is a bug and
    says so rather than pretending.
    """
    if error.is_unknown_user:
        text = NOT_REGISTERED
    elif error.is_pending_approval:
        text = PENDING_APPROVAL
    elif error.status == 404:
        text = "Topilmadi — vazifa o‘chirilgan yoki sizga ko‘rinmaydi."
    elif error.status == 409:
        text = f"Bu amal mumkin emas: {error.detail}"
    elif error.status == 403:
        text = "Bunga ruxsatingiz yo‘q."
    else:
        log.error("unexpected_api_error", status=error.status, detail=error.detail)
        text = "Xatolik yuz berdi. Birozdan so‘ng qayta urinib ko‘ring."

    if isinstance(event, CallbackQuery):
        # Answering with show_alert puts it in front of the user instead of a
        # toast they will miss.
        await event.answer(text[:200], show_alert=True)
    else:
        await event.answer(text)


async def send_card(bot: Bot, chat_id: int, task: dict[str, Any], api: TaskApi) -> None:
    """Post a task card and remember where it landed.

    Recording the message id is what lets a later status change edit this card
    rather than posting another one.
    """
    text, markup = build_card(task)
    message = await bot.send_message(chat_id, text, reply_markup=markup)
    try:
        await api.set_card(task["id"], chat_id, message.message_id)
    except ApiError as error:
        # Not worth failing the user's action over: the card is already visible,
        # it just will not be edited in place later.
        log.warning("set_card_failed", task_id=task["id"], status=error.status)


async def notify_assignee(bot: Bot, task: dict[str, Any], actor_id: int, api: TaskApi) -> None:
    """Tell the assignee, unless they are the person who just did this."""
    assignee = task.get("assignee")
    if not assignee or assignee["telegram_id"] == actor_id:
        return
    try:
        await send_card(bot, assignee["telegram_id"], task, api)
    except Exception as error:  # a blocked bot must not fail the create
        # Telegram refuses to message a user who has never started the bot; the
        # task still exists and shows up on the web.
        log.info("assignee_notify_failed", task_id=task["id"], error=str(error))


async def refresh_card(event: CallbackQuery, task: dict[str, Any]) -> None:
    """Redraw the card this button belongs to, in place."""
    text, markup = build_card(task)
    message = editable(event)
    if message is None:
        return
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception as error:
        # "message is not modified" is the common one and is harmless.
        log.debug("card_edit_skipped", error=str(error))
