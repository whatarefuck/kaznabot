from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaznabot import repo


class DbSessionMiddleware(BaseMiddleware):
    """Открывает сессию БД на каждый апдейт; доступна в фильтрах и хендлерах как ``session``."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self.sessionmaker = sessionmaker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.sessionmaker() as session:
            data["session"] = session
            return await handler(event, data)


class TrackUsersMiddleware(BaseMiddleware):
    """Запоминает всех, кто пишет в чат: Bot API не умеет искать людей по @username,
    поэтому знакомство «@nick — Саша» работает для тех, кого бот уже видел (или станет заглушкой).
    Кладёт в хендлер ``db_user`` — строку users автора сообщения."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.from_user is not None and not event.from_user.is_bot:
            session = data["session"]
            user = await repo.upsert_user(session, event.from_user)
            await repo.ensure_member(session, event.chat.id, user)
            # Коммитим сразу: дальше хендлер может надолго уйти в GigaChat, а SQLite держит блокировку до коммита
            await session.commit()
            data["db_user"] = user
        return await handler(event, data)
