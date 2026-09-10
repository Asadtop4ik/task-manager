from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.api import ApiError
from app.handlers.helpers import api_for, explain_api_error
from app.texts import HELP

router = Router(name="common")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    try:
        me = await api_for(message).me()
    except ApiError as error:
        await explain_api_error(message, error)
        return

    await message.answer(
        f"Salom, {me['full_name']}!\n\n{HELP}",
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Bekor qilinadigan amal yo‘q.")
        return
    await state.clear()
    await message.answer("Bekor qilindi.")


@router.message(Command("projects"))
async def projects(message: Message) -> None:
    api = api_for(message)
    try:
        rows = await api.projects()
    except ApiError as error:
        await explain_api_error(message, error)
        return

    if not rows:
        await message.answer("Sizga hali loyiha biriktirilmagan.")
        return

    lines = ["<b>Loyihalar</b>", ""]
    for project in rows:
        counts = await api.tasks(project_id=project["id"], open_only=True, limit=1)
        lines.append(
            f"• <b>{project['name']}</b> (<code>{project['key']}</code>) — {counts['total']} ochiq"
        )
    lines += ["", "Yangi vazifa: <code>keto: matn</code> ko‘rinishida yozing."]
    await message.answer("\n".join(lines))
