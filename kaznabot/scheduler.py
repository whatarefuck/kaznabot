"""Планировщик: раз в полминуты смотрит, не пора ли что-то прислать в чаты.

Всё, что уже сделано, записано в БД, поэтому после перезапуска бот не повторяет сообщения,
а пропущенное, пока он лежал, досылает — если ещё не поздно.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaznabot import repo
from kaznabot.config import Settings
from kaznabot.handlers.household import post_dinner_poll, refresh_dinner, send_duty_reminder

log = logging.getLogger(__name__)

SATURDAY = 5
DUTY_GRACE = time(12, 0)  # напоминание о воскресной уборке не шлём после полудня воскресенья


def last_weekly(now: datetime, weekday: int, at: time) -> datetime:
    """Последний момент «weekday в at» не позже now (в часовом поясе now)."""
    day = now.date() - timedelta(days=(now.weekday() - weekday) % 7)
    moment = datetime.combine(day, at, tzinfo=now.tzinfo)
    return moment if moment <= now else moment - timedelta(days=7)


class Scheduler:
    def __init__(
        self,
        bot: Bot,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        clock: Callable[[], datetime] | None = None,
    ):
        self.bot = bot
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(settings.tz))

    async def run(self, every: float = 30) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(every)

    async def tick(self) -> None:
        now = self.clock()
        async with self.sessionmaker() as session:
            await self.duty_reminders(session, now)
            await self.dinner_polls(session, now)
            await self.dinner_deadlines(session, now)

    async def duty_reminders(self, session: AsyncSession, now: datetime) -> None:
        sunday = last_weekly(now, SATURDAY, self.settings.duty_reminder_time).date() + timedelta(days=1)
        if now >= datetime.combine(sunday, DUTY_GRACE, tzinfo=now.tzinfo):
            return
        key = sunday.isoformat()
        for chat_id in await repo.chats_with(session, "duty_reminders"):
            if await repo.job_done(session, chat_id, "duty", key):
                continue
            await self._each(session, chat_id, "duty", self._remind(session, chat_id, sunday, key))

    async def _remind(self, session: AsyncSession, chat_id: int, sunday: date, key: str) -> None:
        if await send_duty_reminder(self.bot, session, chat_id, sunday):
            repo.mark_job(session, chat_id, "duty", key)
            await session.commit()

    async def dinner_polls(self, session: AsyncSession, now: datetime) -> None:
        week_start = last_weekly(now, SATURDAY, self.settings.dinner_poll_time).date() + timedelta(days=2)
        # Опрос имеет смысл до начала недели; если бот лежал всё воскресенье — уже не шлём
        if now.date() >= week_start:
            return
        key = week_start.isoformat()
        for chat_id in await repo.chats_with(session, "dinner_polls"):
            if await repo.job_done(session, chat_id, "dinner_poll", key):
                continue
            if (
                await repo.dinner_poll_for_week(session, chat_id, week_start) is not None
            ):  # уже устроили через /dinner poll
                continue
            # Отмечаем заранее: если Telegram примет опрос, а ответ не дойдёт, второй опрос в чат не уйдёт
            repo.mark_job(session, chat_id, "dinner_poll", key)
            await session.commit()
            poll = post_dinner_poll(self.bot, session, chat_id, week_start, self.settings)
            await self._each(session, chat_id, "dinner poll", poll)

    async def dinner_deadlines(self, session: AsyncSession, now: datetime) -> None:
        for poll in await repo.due_dinner_polls(session, now.astimezone(UTC).replace(tzinfo=None)):
            close = refresh_dinner(self.bot, session, poll, self.settings, closing=True)
            await self._each(session, poll.chat_id, "dinner deadline", close)

    async def _each(self, session: AsyncSession, chat_id: int, what: str, job: Awaitable[object]) -> None:
        """Ошибка в одном чате не мешает остальным; повторим на следующем тике."""
        try:
            await job
        except Exception:
            log.exception("%s failed in chat %s", what, chat_id)
            await session.rollback()
