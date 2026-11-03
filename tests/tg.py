"""Фейковый Telegram для сквозных тестов: настоящие роутеры, middleware, граф и БД."""

import itertools
from collections.abc import AsyncGenerator, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendChatAction,
    SendMessage,
    SendPhoto,
    SendPoll,
    StopPoll,
)
from aiogram.methods.base import TelegramMethod, TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    InputPollOption,
    Message,
    MessageEntity,
    Poll,
    PollOption,
    Update,
    User,
)
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from kaznabot.__main__ import build_dispatcher
from kaznabot.ai.assistant import Assistant
from kaznabot.config import Settings
from kaznabot.db import User as DbUser
from kaznabot.db import make_sessionmaker
from tests.fakes import FakeAI

BOT = User(id=999, is_bot=True, first_name="Kazna", username="kazna_bot")
CHAT = Chat(id=-100, type="supergroup", title="Квартира")
VASYA = User(id=1, is_bot=False, first_name="Vasiliy", username="vasya")
PETYA = User(id=2, is_bot=False, first_name="Pyotr", username="petya")
SASHA = User(id=3, is_bot=False, first_name="Alexander", username="sasha")
LESHA = User(id=4, is_bot=False, first_name="Alexey", username="lesha")


class FakeTelegram(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[TelegramMethod[Any]] = []
        self.sent_messages: list[Message] = []  # то, что «вернул» Telegram на SendMessage
        self.polls: list[Poll] = []
        self.ids = itertools.count(1000)

    async def close(self) -> None:
        pass

    def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        raise NotImplementedError

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 — сигнатура задана BaseSession
    ) -> TelegramType:
        self.sent.append(method)
        return cast(TelegramType, self._answer(bot, method))

    def _answer(self, bot: Bot, method: TelegramMethod[Any]) -> Any:
        if isinstance(method, GetMe):
            return BOT
        if isinstance(method, SendChatAction | AnswerCallbackQuery | DeleteMessage):
            return True
        if isinstance(method, SendMessage):
            chat = Chat(id=int(method.chat_id), type="supergroup")
            m = Message(message_id=next(self.ids), date=datetime.now(), chat=chat, from_user=BOT, text=method.text)
            self.sent_messages.append(m)
            return m.as_(bot)
        if isinstance(method, SendPoll):
            options = [
                PollOption(
                    text=str(o.text if isinstance(o, InputPollOption) else o), voter_count=0, persistent_id=str(i)
                )
                for i, o in enumerate(method.options)
            ]
            poll = Poll(
                id=f"poll{next(self.ids)}",
                question=method.question,
                options=options,
                total_voter_count=0,
                is_closed=False,
                is_anonymous=False,
                type="regular",
                allows_multiple_answers=bool(method.allows_multiple_answers),
                allows_revoting=True,
                members_only=False,
            )
            self.polls.append(poll)
            chat = Chat(id=int(method.chat_id), type="supergroup")
            return Message(message_id=next(self.ids), date=datetime.now(), chat=chat, from_user=BOT, poll=poll).as_(bot)
        if isinstance(method, StopPoll):
            return self.polls[-1].model_copy(update={"is_closed": True})
        if isinstance(method, SendPhoto):
            return Message(message_id=next(self.ids), date=datetime.now(), chat=CHAT, from_user=BOT).as_(bot)
        if isinstance(method, EditMessageText | EditMessageReplyMarkup):
            edited = Message(message_id=method.message_id or 0, date=datetime.now(), chat=CHAT, from_user=BOT, text="x")
            return edited.as_(bot)
        raise AssertionError(f"unexpected {type(method).__name__}")

    def last[M: TelegramMethod[Any]](self, cls: type[M]) -> M:
        return next(m for m in reversed(self.sent) if isinstance(m, cls))


@dataclass
class Harness:
    tg: FakeTelegram
    bot: Bot
    ai: FakeAI
    dp: Dispatcher
    engine: AsyncEngine

    async def feed(self, update: Update) -> None:
        await self.dp.feed_update(self.bot, update)

    async def uid(self, username: str) -> int:
        async with make_sessionmaker(self.engine)() as s:
            return (await s.scalars(select(DbUser.id).where(DbUser.username == username))).one()

    def reply(self) -> str:
        return self.tg.last(SendMessage).text

    def edited(self) -> str:
        return self.tg.last(EditMessageText).text or ""


@pytest.fixture
def harness(engine: AsyncEngine) -> Iterator[Harness]:
    tg, ai = FakeTelegram(), FakeAI()
    settings = Settings(bot_token="999:x", gigachat_credentials="x")
    dp = build_dispatcher(settings, Assistant(ai, InMemorySaver()), make_sessionmaker(engine))
    yield Harness(tg, Bot("999:x", session=tg), ai, dp, engine)
    # Роутеры — модульные синглтоны, а aiogram разрешает подключить их к диспетчеру только один раз
    for r in dp.sub_routers:
        r._parent_router = None


msg_ids = itertools.count(1)
upd_ids = itertools.count(1)


def msg(
    user: User,
    text: str,
    reply_to: Message | None = None,
    mentions: Sequence[str] = (),
    chat: Chat = CHAT,
) -> Update:
    entities = [MessageEntity(type="mention", offset=text.index(m), length=len(m)) for m in mentions]
    return Update(
        update_id=next(upd_ids),
        message=Message(
            message_id=next(msg_ids),
            date=datetime.now(),
            chat=chat,
            from_user=user,
            text=text,
            reply_to_message=reply_to,
            entities=entities or None,
        ),
    )


def bot_msg(message_id: int, chat: Chat = CHAT) -> Message:
    return Message(message_id=message_id, date=datetime.now(), chat=chat, from_user=BOT, text="draft")


def click(user: User, data: str, message_id: int, chat: Chat = CHAT) -> Update:
    return Update(
        update_id=next(upd_ids),
        callback_query=CallbackQuery(
            id=str(next(upd_ids)),
            from_user=user,
            chat_instance="c",
            data=data,
            message=bot_msg(message_id, chat),
        ),
    )


def first_button(markup: InlineKeyboardMarkup | None) -> str:
    assert markup is not None
    data = markup.inline_keyboard[0][0].callback_data
    assert data is not None
    return data
