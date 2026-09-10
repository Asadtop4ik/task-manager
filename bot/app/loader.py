from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand

from app.config import settings
from app.handlers import router


def create_bot() -> Bot:
    # aiogram's own error for an empty token is a bare "Token is invalid!" with a
    # traceback, which looks like a bug rather than an unfilled env file.
    if not settings.bot_token:
        raise RuntimeError(
            "BOT_TOKEN is empty. Get one from BotFather and put it in .env "
            "(locally) or /srv/stack/env/task-manager.env (on the server)."
        )
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    # FSM state lives in Redis, not memory: a redeploy mid-`/new` must not lose
    # the half-filled task the manager was typing.
    storage = RedisStorage.from_url(settings.redis_url)
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(router)
    return dispatcher


COMMANDS = [
    BotCommand(command="new", description="Yangi vazifa (bosqichma-bosqich)"),
    BotCommand(command="my", description="Mening ochiq vazifalarim"),
    BotCommand(command="today", description="Bugungi va muddati o‘tganlari"),
    BotCommand(command="projects", description="Loyihalar"),
    BotCommand(command="cancel", description="Amalni bekor qilish"),
    BotCommand(command="help", description="Yordam"),
]


async def setup_commands(bot: Bot) -> None:
    """Populate the command menu.

    Done at startup rather than by hand in BotFather so the list cannot drift
    away from the handlers that actually exist.
    """
    await bot.set_my_commands(COMMANDS)
