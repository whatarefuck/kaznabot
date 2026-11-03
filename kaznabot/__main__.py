import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaznabot import repo
from kaznabot.ai.assistant import Assistant
from kaznabot.ai.llm import GigaChatAI
from kaznabot.config import Settings
from kaznabot.db import create_tables, make_engine, make_sessionmaker
from kaznabot.handlers import chat, chats, common, reports
from kaznabot.middlewares import DbSessionMiddleware, TrackUsersMiddleware

COMMANDS = [
    BotCommand(command="add", description="Добавить трату"),
    BotCommand(command="spent", description="Траты с последнего обнуления"),
    BotCommand(command="debts", description="Кто кому сколько должен"),
    BotCommand(command="chart", description="График трат и долгов"),
    BotCommand(command="exclude", description="Убрать человека из чеков за период"),
    BotCommand(command="people", description="Кто участвует и как кого зовут"),
    BotCommand(command="delete", description="Удалить чек"),
    BotCommand(command="reset", description="Обнулить долги"),
    BotCommand(command="join", description="Участвовать в расчёте"),
    BotCommand(command="leave", description="Выйти из расчёта"),
    BotCommand(command="help", description="Справка"),
]


def make_bot(settings: Settings) -> Bot:
    session = None
    if settings.telegram_api_url:
        # is_local: сервер с --local отдаёт файлы не по HTTP, а путём на диске, и бот читает их напрямую
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.telegram_api_url, is_local=True))
    return Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def build_dispatcher(
    settings: Settings, assistant: Assistant, sessionmaker: async_sessionmaker[AsyncSession]
) -> Dispatcher:
    # settings и assistant попадут в хендлеры по имени аргумента
    dp = Dispatcher(settings=settings, assistant=assistant)
    dp.update.middleware(DbSessionMiddleware(sessionmaker))
    dp.message.outer_middleware(TrackUsersMiddleware())
    # Порядок важен: служебные события и команды раньше «ловушки» для обычного текста в chat
    dp.include_routers(chats.router, common.router, reports.router, chat.router)
    return dp


async def run() -> None:
    settings = Settings()
    engine = make_engine(settings.database_url)
    await create_tables(engine)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as session:
        await repo.release_busy_drafts(session)
        await session.commit()
    bot = make_bot(settings)

    Path(settings.checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(settings.checkpoint_path) as checkpointer:
        assistant = Assistant(GigaChatAI(settings), checkpointer, settings.tz)
        dp = build_dispatcher(settings, assistant, sessionmaker)
        await bot.set_my_commands(COMMANDS)
        try:
            await dp.start_polling(bot)
        finally:
            await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
