from aiogram.types import User as TgUser
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncSession

from kaznabot import repo
from kaznabot.ai.assistant import Assistant, Done, Pending
from kaznabot.ai.graph import State
from kaznabot.ai.schemas import DraftView, Intent, Mention, PersonName, Receipt, ReceiptItem
from kaznabot.db import User
from kaznabot.reports import build_summary
from tests.fakes import FakeAI

CHAT = -100


def draft_of(out: Pending | Done) -> DraftView:
    assert isinstance(out, Pending)
    assert out.view["kind"] == "draft"
    return out.view


async def setup(session: AsyncSession) -> dict[str, User]:
    users = {}
    for tg_id, name, username in [(1, "Vasiliy", "vasya"), (2, "Pyotr", "petya"), (3, "Alexander", "sasha")]:
        users[username] = u = await repo.upsert_user(
            session, TgUser(id=tg_id, is_bot=False, first_name=name, username=username)
        )
        await repo.ensure_member(session, CHAT, u)
    await session.commit()
    return users


async def test_introduce_by_mentions_including_unknown_user(session: AsyncSession) -> None:
    u = await setup(session)
    ai = FakeAI(
        Intent(
            intent="introduce",
            people=[
                PersonName(ref="@petya", name="петя"),
                PersonName(ref="@lesha", name="Лёша"),  # ещё не писал в чат → заглушка
                PersonName(ref="@hacker", name="Взлом"),  # нет в сообщении → игнор
            ],
        )
    )
    bot = Assistant(ai, InMemorySaver())
    out = await bot.start(
        "t1",
        State(
            chat_id=CHAT,
            author_id=u["vasya"].id,
            payer_id=u["vasya"].id,
            mode="auto",
            addressed=False,
            text="@petya - петя, а @lesha - Лёша",
            mentions=[Mention(ref="@petya", label="@petya"), Mention(ref="@lesha", label="@lesha")],
        ),
        session,
    )
    assert isinstance(out, Done)
    assert out.result["kind"] == "introduced"
    assert [p["name"] for p in out.result.get("people", [])] == ["Петя", "Лёша"]
    assert out.result.get("skipped") == ["@hacker"]
    names = {p["name"]: p for p in await repo.roster(session, CHAT)}
    assert names["Петя"]["active"]
    assert names["Лёша"]["active"]
    assert "Взлом" not in names

    # Лёша впервые пишет в чат — заглушка становится его аккаунтом
    lesha = await repo.upsert_user(session, TgUser(id=77, is_bot=False, first_name="Alexey", username="Lesha"))
    assert lesha.id == names["Лёша"]["id"]
    assert lesha.tg_id == 77


async def test_not_addressed_non_intro_is_ignored(session: AsyncSession) -> None:
    u = await setup(session)
    ai = FakeAI(Intent(intent="expense"))
    out = await Assistant(ai, InMemorySaver()).start(
        "t",
        State(
            chat_id=CHAT,
            author_id=u["vasya"].id,
            payer_id=u["vasya"].id,
            mode="auto",
            addressed=False,
            text="@petya - 300 за пиццу",
            mentions=[],
        ),
        session,
    )
    assert out == Done({"kind": "ignored"})


async def test_expense_split_refine_and_save(session: AsyncSession) -> None:
    u = await setup(session)
    vasya, petya, sasha = u["vasya"].id, u["petya"].id, u["sasha"].id
    for user in u.values():
        await repo.ensure_member(session, CHAT, user, activate=True)
    await session.commit()

    ai = FakeAI(
        Intent(intent="expense"),
        Receipt(
            is_purchase=True,
            store="Пятёрочка",
            items=[
                ReceiptItem(name="Молоко", amount=100, for_whom=[vasya, sasha, 999]),
                ReceiptItem(name="Творог", amount=200),
                ReceiptItem(name="Хлеб", amount=90),
            ],
        ),
        Receipt(
            is_purchase=True,
            store="Пятёрочка",
            items=[
                ReceiptItem(name="Молоко", amount=100, for_whom=[vasya, sasha]),
                ReceiptItem(name="Хлеб", amount=90),
            ],
        ),
    )
    bot = Assistant(ai, InMemorySaver())
    out = await bot.start(
        "t2",
        State(
            chat_id=CHAT,
            author_id=vasya,
            payer_id=vasya,
            mode="auto",
            addressed=True,
            text="молоко 100 мне и Саше, творог 200, хлеб 90",
            mentions=[],
        ),
        session,
    )
    view = draft_of(out)
    assert view["participants"] == [[vasya, sasha], [vasya, petya, sasha], [vasya, petya, sasha]]
    assert "999" in view["draft"]["note"]
    assert f"{sasha}: Alexander" in ai.prompt(1)  # модель видела список людей с id

    out = await bot.resume("t2", {"action": "refine", "text": "убери творог", "by": vasya}, session)
    assert len(draft_of(out)["draft"]["items"]) == 2
    assert "убери творог" in ai.prompt()

    out = await bot.resume("t2", {"action": "ok", "by": vasya, "bot_msg_id": 55}, session)
    assert isinstance(out, Done)
    assert out.result["kind"] == "saved"
    s = await build_summary(session, CHAT, None)
    # молоко 100 на двоих, хлеб 90 на троих: доли Вася 80, Петя 30, Саша 80
    assert s.owed == {vasya: 8000, sasha: 8000, petya: 3000}
    assert s.balances == {vasya: 19000 - 8000, petya: -3000, sasha: -8000}
    assert not await bot.checkpointer.aget_tuple({"configurable": {"thread_id": "t2"}})


async def test_llm_failure_during_refine_keeps_draft(session: AsyncSession) -> None:
    u = await setup(session)
    ai = FakeAI(Receipt(is_purchase=True, items=[ReceiptItem(name="Хлеб", amount=50)]), RuntimeError("503"))
    bot = Assistant(ai, InMemorySaver())
    vasya = u["vasya"].id
    state = State(chat_id=CHAT, author_id=vasya, payer_id=vasya, mode="expense", addressed=True, text="хлеб 50")
    assert isinstance(await bot.start("t3", state, session), Pending)
    out = await bot.resume("t3", {"action": "refine", "text": "...", "by": vasya}, session)
    view = draft_of(out)
    assert view["error"] == "llm"
    assert view["draft"]["items"][0]["name"] == "Хлеб"
    assert await bot.resume("t3", {"action": "cancel", "by": vasya}, session) == Done({"kind": "cancelled"})


async def test_photo_goes_through_transcription(session: AsyncSession) -> None:
    u = await setup(session)
    ai = FakeAI(Receipt(is_purchase=False))
    ai.transcript = None  # на фото не чек
    out = await Assistant(ai, InMemorySaver()).start(
        "t4",
        State(
            chat_id=CHAT,
            author_id=u["vasya"].id,
            payer_id=u["vasya"].id,
            mode="expense",
            addressed=False,
            file_id="f1",
        ),
        session,
    )
    assert out == Done({"kind": "not_purchase"})
    assert [c[0] for c in ai.calls] == ["transcribe"]
