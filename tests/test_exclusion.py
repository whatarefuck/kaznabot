from datetime import date, datetime

from aiogram.types import User as TgUser
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from kaznabot import repo
from kaznabot.ai.assistant import Assistant, Done, Pending
from kaznabot.ai.graph import State
from kaznabot.ai.schemas import ExcludeRequest, ExclusionView
from kaznabot.db import Settlement, create_tables, make_engine
from kaznabot.reports import build_summary
from tests.fakes import FakeAI

CHAT = -100
TODAY = date(2026, 10, 19)


def exclusion_of(out: Pending | Done) -> ExclusionView:
    assert isinstance(out, Pending)
    assert out.view["kind"] == "exclusion"
    return out.view


async def setup(session: AsyncSession) -> dict[str, int]:
    ids = {}
    for tg_id, name in enumerate(["vasya", "petya", "sasha", "arslan"], 1):
        user = await repo.upsert_user(session, TgUser(id=tg_id, is_bot=False, first_name=name))
        await repo.ensure_member(session, CHAT, user, activate=True)
        ids[name] = user.id
    await session.commit()
    return ids


async def add(
    session: AsyncSession,
    payer: int,
    items: list[tuple[str, int, list[int]]],
    purchased_on: date | None,
    created_at: datetime | None = None,
) -> int:
    e = await repo.create_expense(
        session,
        chat_id=CHAT,
        payer_id=payer,
        created_by_id=payer,
        title=None,
        items=[(name, 1, amount, people) for name, amount, people in items],
        purchased_on=purchased_on,
    )
    if created_at is not None:
        e.created_at = created_at
    await session.commit()
    return e.id


def request(person: int | None, start: str | None, end: str | None) -> ExcludeRequest:
    return ExcludeRequest(person_id=person, date_from=start, date_to=end)


async def test_exclude_preview_refine_apply_and_undo(session: AsyncSession) -> None:
    u = await setup(session)
    v, p, s, a = u["vasya"], u["petya"], u["sasha"], u["arslan"]
    everyone = [v, p, s, a]
    # До обнуления: в периоде, но долги по нему закрыты
    await add(session, v, [("Чай", 40000, everyone)], date(2026, 10, 12), datetime(2026, 10, 12, 8))
    session.add(Settlement(chat_id=CHAT, created_by_id=v, created_at=datetime(2026, 10, 12, 12)))
    await session.commit()
    await add(session, v, [("Сыр", 40000, everyone)], date(2026, 10, 10))  # до периода
    await add(
        session,
        p,
        [("Хлеб", 40000, everyone), ("Пиво", 20000, [a]), ("Молоко", 30000, [v, a])],  # пиво — только Арслану
        date(2026, 10, 13),
    )
    # Старая трата без даты покупки: считается днём ввода (15.10 по Москве)
    await add(session, s, [("Мясо", 80000, everyone)], None, datetime(2026, 10, 15, 9))

    ai = FakeAI(request(a, "2026-10-12", "2026-10-18"), request(a, "2026-10-12", "2026-10-14"))
    bot = Assistant(ai, InMemorySaver())
    state = State(
        today=TODAY.isoformat(),
        chat_id=CHAT,
        author_id=v,
        payer_id=v,
        mode="exclude",
        addressed=True,
        text="Арслан 12-18",
    )
    view = exclusion_of(await bot.start("t", state, session))
    assert (view["receipts"], view["items"], view["solo"], view["settled"]) == (2, 3, 1, 1)
    assert view["amount"] == 10000 + 15000 + 20000  # хлеб /4, молоко /2, мясо /4
    assert "Сегодня: 2026-10-19, понедельник." in ai.prompt()

    view = exclusion_of(await bot.resume("t", {"action": "refine", "text": "по 14-е", "by": v}, session))
    assert (view["date_to"], view["receipts"], view["amount"]) == ("2026-10-14", 1, 25000)
    assert "по 14-е" in ai.prompt()

    before = await build_summary(session, CHAT, await repo.last_settlement_at(session, CHAT))
    out = await bot.resume("t", {"action": "ok", "by": v}, session)
    assert isinstance(out, Done)
    assert out.result["kind"] == "excluded"
    after = await build_summary(session, CHAT, await repo.last_settlement_at(session, CHAT))
    assert after.owed[a] == before.owed[a] - 25000
    assert after.total == before.total  # деньги не пропали — перераспределились
    assert sum(after.balances.values()) == 0

    exclusion = await repo.get_exclusion(session, CHAT, out.result["exclusion_id"])
    assert exclusion is not None
    assert await repo.revert_exclusion(session, exclusion) == 2
    await session.commit()
    restored = await build_summary(session, CHAT, await repo.last_settlement_at(session, CHAT))
    assert restored.owed == before.owed


async def test_unclear_request_and_empty_period(session: AsyncSession) -> None:
    u = await setup(session)
    v = u["vasya"]
    ai = FakeAI(request(None, "2026-10-12", None), request(u["arslan"], "2026-10-01", None))
    bot = Assistant(ai, InMemorySaver())

    def state(thread: str) -> State:
        return State(
            today=TODAY.isoformat(), chat_id=CHAT, author_id=v, payer_id=v, mode="exclude", addressed=True, text="x"
        )

    out = await bot.start("a", state("a"), session)
    assert isinstance(out, Done)
    assert out.result["kind"] == "unclear"

    view = exclusion_of(await bot.start("b", state("b"), session))
    assert (view["date_to"], view["receipts"]) == ("2026-10-19", 0)  # конец не назван — по сегодня
    view = exclusion_of(await bot.resume("b", {"action": "ok", "by": v}, session))  # пустое не применяем
    assert view["error"] == "nothing"


async def test_failed_refine_keeps_previous_plan(session: AsyncSession) -> None:
    u = await setup(session)
    ai = FakeAI(request(u["arslan"], "2026-10-12", "2026-10-18"), RuntimeError("429"))
    bot = Assistant(ai, InMemorySaver())
    st = State(
        today=TODAY.isoformat(), chat_id=CHAT, author_id=u["vasya"], payer_id=u["vasya"], mode="exclude", addressed=True
    )
    exclusion_of(await bot.start("t", st, session))
    view = exclusion_of(await bot.resume("t", {"action": "refine", "text": "?", "by": u["vasya"]}, session))
    assert (view["error"], view["date_to"]) == ("llm", "2026-10-18")


async def test_old_database_gets_purchase_date_column(tmp_path: object) -> None:
    engine: AsyncEngine = make_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:  # таблица в том виде, как была до этой версии
        await conn.execute(text("CREATE TABLE expenses (id INTEGER PRIMARY KEY, chat_id BIGINT, total INTEGER)"))
    await create_tables(engine)
    async with engine.connect() as conn:
        cols = {row[1] for row in await conn.execute(text("PRAGMA table_info(expenses)"))}
    assert "purchased_on" in cols
    await create_tables(engine)  # повторный запуск ничего не ломает
    await engine.dispose()
