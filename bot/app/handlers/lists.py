from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.api import ApiError, TaskApi
from app.cards import is_overdue, summary_line
from app.handlers.helpers import api_for, explain_api_error

router = Router(name="lists")


async def _render_grouped(api: TaskApi, tasks: list[dict], empty: str) -> str:
    if not tasks:
        return empty

    by_project: dict[str, list[dict]] = {}
    for task in tasks:
        by_project.setdefault(task["project"]["name"], []).append(task)

    blocks = []
    for project_name, rows in by_project.items():
        lines = [f"<b>{project_name}</b>"]
        lines += [summary_line(task) for task in rows]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


@router.message(Command("my"))
async def my_tasks(message: Message) -> None:
    api = api_for(message)
    try:
        me = await api.me()
        result = await api.tasks(assignee_id=me["id"], open_only=True, limit=50)
    except ApiError as error:
        await explain_api_error(message, error)
        return

    body = await _render_grouped(api, result["items"], "Sizda ochiq vazifa yo‘q. 🎉")
    await message.answer(body)


@router.message(Command("today"))
async def today(message: Message) -> None:
    """Overdue first, then everything else still open and assigned to you.

    The overdue block is separated rather than sorted in, because a late task
    that is merely at the top of a long list gets read as just another row.
    """
    api = api_for(message)
    try:
        me = await api.me()
        result = await api.tasks(assignee_id=me["id"], open_only=True, limit=50)
    except ApiError as error:
        await explain_api_error(message, error)
        return

    tasks = result["items"]
    late = [task for task in tasks if is_overdue(task)]
    dated = [task for task in tasks if task.get("due_at") and not is_overdue(task)]

    if not late and not dated:
        await message.answer("Muddatli vazifa yo‘q. 🎉")
        return

    blocks = []
    if late:
        blocks.append("<b>⚠️ Muddati o‘tgan</b>\n" + "\n".join(summary_line(t) for t in late))
    if dated:
        blocks.append("<b>🕒 Muddatli</b>\n" + "\n".join(summary_line(t) for t in dated))
    await message.answer("\n\n".join(blocks))
