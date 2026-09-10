from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

router = Router(name="root")


@router.message(CommandStart())
async def start(message: Message) -> None:
    """Placeholder until milestone 3 lands the real command set.

    It exists now so a deployed skeleton is verifiable from a phone: if this
    replies, the webhook, the secret header and the container are all correct.
    """
    await message.answer(
        "Task Manager is up. Commands land in milestone 3 — "
        "for now this only confirms the webhook works."
    )
