"""Сквозной тест: настоящие роутеры, middleware, граф и БД; фейковые Telegram API и GigaChat."""

import itertools
from collections.abc import AsyncGenerator, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

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
)
from aiogram.methods.base import TelegramMethod, TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    Update,
    User,
)
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from kaznabot.__main__ import build_dispatcher
from kaznabot.ai.assistant import Assistant
from kaznabot.ai.schemas import ExcludeRequest, Intent, PersonName, Receipt, ReceiptItem
from kaznabot.config import Settings
from kaznabot.db import User as DbUser
from kaznabot.db import make_sessionmaker
from kaznabot.handlers import chat, chats, common, reports
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
    for r in (chats.router, common.router, reports.router, chat.router):
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


async def test_intro_split_refine_confirm_debts(harness: Harness) -> None:
    h, tg, ai = harness, harness.tg, harness.ai
    for u in (VASYA, PETYA):
        await h.feed(msg(u, "/join"))
    await h.feed(msg(SASHA, "всем привет"))  # бот запомнил Сашу, но в LLM ничего не ушло
    assert ai.calls == []

    # Знакомство без обращения к боту; Лёша ещё не писал в чат
    people = [PersonName(ref="@sasha", name="саша"), PersonName(ref="@lesha", name="Лёша")]
    ai.answers.append(Intent(intent="introduce", people=people))
    await h.feed(msg(VASYA, "@sasha - саша, а @lesha - Лёша", mentions=["@sasha", "@lesha"]))
    assert "Запомнил: Саша — @sasha, Лёша — @lesha" in h.reply()
    assert "@sasha (" not in ai.prompt()  # упоминания переданы модели как есть
    vasya, sasha, lesha = [await h.uid(u) for u in ("vasya", "sasha", "lesha")]

    # Трата с разделом: молоко только мне и Саше, творог и хлеб на всех
    milk = ReceiptItem(name="Молоко", amount=100, for_whom=[vasya, sasha])
    bread = ReceiptItem(name="Хлеб", amount=80)
    ai.answers.append(Receipt(is_purchase=True, items=[milk, ReceiptItem(name="Творог", amount=200), bread]))
    await h.feed(msg(VASYA, "/add молоко 100 мне и Саше, творог 200, хлеб 80"))
    draft_msg = tg.last(EditMessageReplyMarkup)
    assert draft_msg.message_id is not None
    ok = first_button(draft_msg.reply_markup)
    draft_text = h.edited()
    assert "Молоко — 100 ₽ → <i>Vasiliy, Саша</i>" in draft_text  # Васю не представляли — имя из Telegram
    assert "Делится на всех: Vasiliy, Pyotr, Саша, Лёша" in draft_text

    # Уточнять может только автор
    await h.feed(msg(PETYA, "убери творог", reply_to=bot_msg(draft_msg.message_id)))
    assert "только тот, кто его создал" in h.reply()

    ai.answers.append(Receipt(is_purchase=True, items=[milk, bread]))
    await h.feed(msg(VASYA, "убери творог", reply_to=bot_msg(draft_msg.message_id)))
    assert "убери творог" in ai.prompt()
    refined = tg.sent_messages[-1]
    refined_text = refined.text or ""
    assert "обновлён" in refined_text
    assert "Творог" not in refined_text
    assert "Доли: Vasiliy 70 ₽ · Саша 70 ₽ · Pyotr 20 ₽ · Лёша 20 ₽" in refined_text

    # Подтверждает только автор; кнопки теперь на новом сообщении
    await h.feed(click(PETYA, ok, refined.message_id))
    assert tg.last(AnswerCallbackQuery).show_alert
    await h.feed(click(VASYA, ok, refined.message_id))
    assert "Внесено #1" in h.edited()
    await h.feed(click(VASYA, ok, refined.message_id))
    assert "закрыт" in (tg.last(AnswerCallbackQuery).text or "")

    await h.feed(msg(PETYA, "/debts"))
    debts = h.reply()
    assert "Саша → Vasiliy: <b>70 ₽</b>" in debts, debts
    assert "Pyotr → Vasiliy: <b>20 ₽</b>" in debts
    assert "Лёша → Vasiliy: <b>20 ₽</b>" in debts

    await h.feed(msg(PETYA, "/chart"))
    photo = tg.last(SendPhoto)
    assert "Саша → Vasiliy: <b>70 ₽</b>" in (photo.caption or "")

    # Лёша впервые пишет в чат — заглушка превращается в его аккаунт, имя и долг сохраняются
    await h.feed(msg(LESHA, "привет"))
    await h.feed(msg(LESHA, "/people"))
    assert "Лёша — @lesha" in h.reply()
    assert "ещё не писал" not in h.reply()
    assert await h.uid("lesha") == lesha

    await h.feed(msg(PETYA, "/delete 1"))
    assert "Удалён чек #1" in h.reply()
    await h.feed(msg(PETYA, "/debts"))
    assert "Все в расчёте" in h.reply()


async def test_question_to_bot_routes_to_report(harness: Harness) -> None:
    harness.ai.answers.append(Intent(intent="debts"))
    await harness.feed(msg(VASYA, "@kazna_bot кто кому сколько должен?", mentions=["@kazna_bot"]))
    assert "Расчёт с начала учёта" in harness.edited()
    assert "@kazna_bot" not in harness.ai.prompt()  # упоминание бота вырезано из текста


async def test_exclude_command_apply_and_undo(harness: Harness) -> None:
    h, tg, ai = harness, harness.tg, harness.ai
    for u in (VASYA, PETYA):
        await h.feed(msg(u, "/join"))
    ai.answers.append(Receipt(is_purchase=True, items=[ReceiptItem(name="Хлеб", amount=100)]))
    await h.feed(msg(VASYA, "/add хлеб 100"))
    draft = tg.last(EditMessageReplyMarkup)
    assert draft.message_id is not None
    await h.feed(click(VASYA, first_button(draft.reply_markup), draft.message_id))

    today = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
    ai.answers.append(ExcludeRequest(person_id=await h.uid("petya"), date_from=today, date_to=None))
    await h.feed(msg(VASYA, "/exclude Петю за сегодня"))
    assert "Петю за сегодня" in ai.prompt()
    preview = tg.last(EditMessageReplyMarkup)
    assert preview.message_id is not None
    assert preview.reply_markup is not None
    assert preview.reply_markup.inline_keyboard[0][0].text == "✅ Применить"
    assert "Затронет чеков: 1" in h.edited()
    assert "<b>50 ₽</b>" in h.edited()

    await h.feed(click(VASYA, first_button(preview.reply_markup), preview.message_id))
    done = tg.last(EditMessageText)
    assert "Убрал из чеков" in (done.text or "")
    undo = first_button(done.reply_markup)
    await h.feed(msg(PETYA, "/debts"))
    assert "Все в расчёте" in h.reply()  # хлеб теперь только на Васю

    await h.feed(click(PETYA, undo, preview.message_id))
    assert "Отменено" in h.edited()
    await h.feed(msg(PETYA, "/debts"))
    assert "Pyotr → Vasiliy: <b>50 ₽</b>" in h.reply()


# --- несколько чатов ---

FLAT_B = Chat(id=-200, type="supergroup", title="Вторая квартира")


async def add_expense(h: Harness, user: User, amount: float, chat: Chat = CHAT) -> None:
    h.ai.answers.append(Receipt(is_purchase=True, items=[ReceiptItem(name="Хлеб", amount=amount)]))
    await h.feed(msg(user, "/add хлеб", chat=chat))
    draft = h.tg.last(EditMessageReplyMarkup)
    assert draft.message_id is not None
    await h.feed(click(user, first_button(draft.reply_markup), draft.message_id, chat))


async def test_chats_keep_separate_books(harness: Harness) -> None:
    h = harness
    for u in (VASYA, PETYA):
        await h.feed(msg(u, "/join"))
    for u in (VASYA, SASHA):
        await h.feed(msg(u, "/join", chat=FLAT_B))
    await add_expense(h, VASYA, 100)
    await add_expense(h, SASHA, 300, chat=FLAT_B)

    await h.feed(msg(PETYA, "/debts"))
    assert "Pyotr → Vasiliy: <b>50 ₽</b>" in h.reply()
    assert "Alexander" not in h.reply()
    await h.feed(msg(VASYA, "/debts", chat=FLAT_B))
    assert "Vasiliy → Alexander: <b>150 ₽</b>" in h.reply()
    assert "Pyotr" not in h.reply()

    # Кнопка черновика из чужого чата его не трогает
    h.ai.answers.append(Receipt(is_purchase=True, items=[ReceiptItem(name="Сыр", amount=10)]))
    await h.feed(msg(VASYA, "/add сыр"))
    draft = h.tg.last(EditMessageReplyMarkup)
    assert draft.message_id is not None
    await h.feed(click(VASYA, first_button(draft.reply_markup), draft.message_id, FLAT_B))
    assert "закрыт" in (h.tg.last(AnswerCallbackQuery).text or "")


async def test_group_upgraded_to_supergroup_keeps_expenses(harness: Harness) -> None:
    h = harness
    old = Chat(id=-55, type="group", title="Квартира")
    new = Chat(id=-1000000000055, type="supergroup", title="Квартира")
    for u in (VASYA, PETYA):
        await h.feed(msg(u, "/join", chat=old))
    await add_expense(h, VASYA, 100, chat=old)
    h.ai.answers.append(Receipt(is_purchase=True, items=[ReceiptItem(name="Сыр", amount=10)]))
    await h.feed(msg(VASYA, "/add сыр", chat=old))  # черновик так и остался незакрытым

    def service(chat: Chat, to_id: int | None = None, from_id: int | None = None) -> Update:
        message = Message(
            message_id=next(msg_ids),
            date=datetime.now(),
            chat=chat,
            from_user=VASYA,
            migrate_to_chat_id=to_id,
            migrate_from_chat_id=from_id,
        )
        return Update(update_id=next(upd_ids), message=message)

    sent = len(h.tg.sent_messages)
    await h.feed(service(new, from_id=old.id))
    await h.feed(service(old, to_id=new.id))  # второе служебное сообщение ничего не меняет
    notices = [m.text or "" for m in h.tg.sent_messages[sent:]]
    assert len(notices) == 1
    assert "Перенёс все траты (1)" in notices[0]
    assert "черновики пришлось отменить" in notices[0]

    await h.feed(msg(PETYA, "/debts", chat=new))
    assert "Pyotr → Vasiliy: <b>50 ₽</b>" in h.reply()


def bot_added(chat: Chat) -> Update:
    event = ChatMemberUpdated(
        chat=chat,
        from_user=VASYA,
        date=datetime.now(),
        old_chat_member=ChatMemberLeft(user=BOT),
        new_chat_member=ChatMemberMember(user=BOT),
    )
    return Update(update_id=next(upd_ids), my_chat_member=event)


async def test_welcome_only_in_a_new_chat(harness: Harness) -> None:
    h = harness
    await h.feed(bot_added(FLAT_B))
    assert "Учёт здесь отдельный" in h.reply()
    await h.feed(msg(VASYA, "/join", chat=FLAT_B))
    sent = len(h.tg.sent_messages)
    await h.feed(bot_added(FLAT_B))  # бота удалили и вернули — второй раз не здороваемся
    assert len(h.tg.sent_messages) == sent


async def test_private_chat_explains_groups(harness: Harness) -> None:
    await harness.feed(msg(VASYA, "/start", chat=Chat(id=VASYA.id, type="private")))
    assert "Я работаю в групповых чатах" in harness.reply()
