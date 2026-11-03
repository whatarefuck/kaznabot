from datetime import date

from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from kaznabot import repo
from kaznabot.ai.schemas import ExcludeRequest, ExclusionPlan, Receipt, ReceiptItem, RosterEntry, to_draft, to_exclusion
from kaznabot.reports import build_summary

CHAT = -100
TODAY = date(2026, 10, 19)


def tg(uid: int, name: str, username: str | None = None) -> TgUser:
    return TgUser(id=uid, is_bot=False, first_name=name, username=username)


async def test_placeholder_is_claimed_by_first_message(session: AsyncSession) -> None:
    lesha = await repo.user_by_username(session, "@Lesha")
    await repo.ensure_member(session, CHAT, lesha, activate=True, display_name="Лёша")
    await session.commit()
    assert lesha.tg_id is None

    real = await repo.upsert_user(session, tg(5, "Alexey", "lesha"))
    assert real.id == lesha.id
    assert real.tg_id == 5
    assert (await repo.roster(session, CHAT))[0]["name"] == "Лёша"


async def test_placeholder_merged_when_user_changes_username(session: AsyncSession) -> None:
    # Человек писал как @old, потом его представили как @new, потом он сменил ник и написал снова
    real = await repo.upsert_user(session, tg(7, "Pyotr", "old"))
    stub = await repo.user_by_username(session, "new")
    await repo.ensure_member(session, CHAT, stub, activate=True, display_name="Петя")
    await repo.create_expense(
        session,
        chat_id=CHAT,
        payer_id=stub.id,
        created_by_id=stub.id,
        title="x",
        items=[("Пицца", 1, 1000, [stub.id, real.id])],
    )
    await session.commit()

    merged = await repo.upsert_user(session, tg(7, "Pyotr", "new"))
    await session.commit()
    assert merged.id == real.id
    roster = await repo.roster(session, CHAT)
    assert [(p["id"], p["name"], p["active"]) for p in roster] == [(real.id, "Петя", True)]
    s = await build_summary(session, CHAT, None)
    assert s.paid == {real.id: 1000}
    assert s.owed == {real.id: 1000}


async def test_to_draft_validates_people() -> None:
    r = Receipt(
        is_purchase=True,
        payer_id=42,
        shared_with=[1, 2],
        items=[
            ReceiptItem(name=" Молоко ", amount=179.98, for_whom=[3, 99]),
            ReceiptItem(name="Бонусы", amount=0),
        ],
    )
    d = to_draft(r, known_ids={1, 2, 3}, default_payer=1, today=TODAY)
    assert d.payer_id == 1  # 42 не из чата
    assert d.items[0].name == "Молоко"
    assert d.items[0].amount == 17998
    assert d.items[0].for_whom == [3]
    assert len(d.items) == 1
    assert d.note is not None
    assert "42" in d.note
    assert "99" in d.note
    assert d.participants(default_ids=[1, 2, 3]) == [[3]]
    assert to_draft(d.to_llm(), {1, 2, 3}, 1, TODAY) == d.model_copy(update={"note": d.note})


async def test_frozen_participants_and_reset(session: AsyncSession) -> None:
    a = await repo.upsert_user(session, tg(1, "A"))
    b = await repo.upsert_user(session, tg(2, "B"))
    for u in (a, b):
        await repo.ensure_member(session, CHAT, u, activate=True)
    await repo.create_expense(
        session, chat_id=CHAT, payer_id=a.id, created_by_id=a.id, title="x", items=[("Хлеб", 1, 10000, [a.id, b.id])]
    )
    await session.commit()

    # Новый сосед не платит за покупку, сделанную до него
    c = await repo.upsert_user(session, tg(3, "C"))
    await repo.ensure_member(session, CHAT, c, activate=True)
    await session.commit()
    s = await build_summary(session, CHAT, None)
    assert s.balances == {a.id: 5000, b.id: -5000, c.id: 0}

    await repo.add_settlement(session, CHAT, a)
    await session.commit()
    s = await build_summary(session, CHAT, await repo.last_settlement_at(session, CHAT))
    assert s.total == 0
    assert s.transfers == []


def test_purchase_date_is_validated() -> None:
    def day(value: str | None) -> date | None:
        return to_draft(Receipt(is_purchase=True, purchase_date=value), {1}, 1, TODAY).purchase_date

    assert day("2026-10-14") == date(2026, 10, 14)
    assert day("2026-10-19") is None  # сегодня — не храним отдельно
    assert day("2026-10-25") is None  # из будущего
    assert day("2024-01-01") is None  # старше года
    assert day("14 октября") is None  # не ISO
    assert day(None) is None


def test_exclusion_rejects_substituted_person() -> None:
    roster = [
        RosterEntry(id=1, name="Бай", username="bai", tg_name="Bainazar", active=True),
        RosterEntry(id=4, name="Арслан", username=None, tg_name="Arslan K", active=True),
        RosterEntry(id=2, name="Лёша", username="alex", tg_name="Alexey", active=True),
    ]

    def plan(name: str | None, person: int | None) -> object:
        r = ExcludeRequest(person_name=name, person_id=person, date_from="2026-10-12", date_to="2026-10-18")
        return to_exclusion(r, roster, author_id=1, today=TODAY)

    assert isinstance(plan("Арслана", 4), ExclusionPlan)  # падеж
    assert isinstance(plan("Лёши", 2), ExclusionPlan)  # ё/е и падеж
    assert isinstance(plan("@alex", 2), ExclusionPlan)
    assert isinstance(plan("меня", 1), ExclusionPlan)  # автор про себя
    assert isinstance(plan(None, 4), ExclusionPlan)  # имени нет — проверить нечем
    assert plan("Петю", 1) == "не нашёл «Петю» среди людей чата — проверьте /people"  # модель подставила Бая
    assert isinstance(plan("меня", 4), str)
    assert isinstance(plan("Петю", None), str)
