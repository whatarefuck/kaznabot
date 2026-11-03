from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram.methods import EditMessageText, SendMessage, SendPoll, StopPoll
from aiogram.types import PollAnswer, Update, User
from sqlalchemy.ext.asyncio import AsyncSession

from kaznabot import repo
from kaznabot.config import Settings
from kaznabot.db import make_sessionmaker
from kaznabot.household import Rotation, RotationDraft, assign_cooks, next_monday, next_sunday, parse_rotation
from kaznabot.scheduler import Scheduler, last_weekly
from tests.tg import CHAT, PETYA, SASHA, VASYA, Harness, msg, upd_ids

MSK = ZoneInfo("Europe/Moscow")
SUN = date(2026, 11, 8)
DAYS = [date(2026, 11, 9) + timedelta(days=i) for i in range(7)]

# --- ротация дежурств ---

ZONES = ("Кух", "В", "Г", "Кор", "1", "2")
DANIL, ISKAN, BAI, AMIR, RAV, ARSLAN = range(1, 7)


def test_rotation_matches_photo_and_cycles() -> None:
    r = Rotation(anchor=SUN, zones=ZONES, people=(DANIL, ISKAN, BAI, AMIR, RAV, ARSLAN))
    assert dict(r.week(SUN)) == {"Кух": DANIL, "В": ISKAN, "Г": BAI, "Кор": AMIR, "1": RAV, "2": ARSLAN}
    assert dict(r.week(SUN + timedelta(weeks=1)))["Кух"] == ISKAN  # кухня переходит к следующему
    # За 6 недель каждый побывал в каждой зоне ровно по разу
    for person in r.people:
        seen = [z for w in range(6) for z in r.by_person(SUN + timedelta(weeks=w))[person]]
        assert sorted(seen) == sorted(ZONES)
    assert r.week(SUN - timedelta(weeks=1)) == r.week(SUN + timedelta(weeks=5))  # и до anchor тоже по кругу


def test_rotation_with_more_zones_than_people() -> None:
    r = Rotation(anchor=SUN, zones=("Кух", "В", "Г"), people=(1, 2))
    assert r.by_person(SUN) == {1: ["Кух", "Г"], 2: ["В"]}
    assert r.by_person(SUN + timedelta(weeks=1)) == {1: ["В"], 2: ["Кух", "Г"]}


def test_parse_rotation() -> None:
    today = date(2026, 11, 3)
    draft = parse_rotation("08.11 Данил — Кух, Искандар - В\nАмир: Кор", today)
    assert draft == RotationDraft(SUN, [("Данил", "Кух"), ("Искандар", "В"), ("Амир", "Кор")])
    no_date = parse_rotation("Данил — Кух, Амир — В", today)
    assert isinstance(no_date, RotationDraft)
    assert no_date.anchor == next_sunday(today) == SUN
    assert parse_rotation("09.11 Данил — Кух, Амир — В", today) == "09.11.2026 — не воскресенье"
    assert isinstance(parse_rotation("Данил Кух, Амир В", today), str)
    assert next_monday(today) == date(2026, 11, 9)


# --- кто когда готовит ---


def test_everyone_once_then_second_dinner() -> None:
    people = [1, 2, 3, 4, 5, 6]
    plan = assign_cooks(DAYS, {p: set(DAYS) for p in people}, people)
    loads = sorted(list(plan.values()).count(p) for p in people)
    assert loads == [1, 1, 1, 1, 1, 2]
    assert None not in plan.values()


def test_respects_availability_and_empty_days() -> None:
    mon, tue, wed = DAYS[:3]
    plan = assign_cooks(DAYS, {1: {mon, tue}, 2: {mon}, 3: set()}, [1, 2, 3])
    assert plan[mon] == 2  # Пн может только ещё 2 — 1 берёт вторник
    assert plan[tue] == 1
    assert plan[wed] is None


def test_revote_keeps_others_in_place() -> None:
    people = [1, 2, 3, 4]
    available = {p: set(DAYS) for p in people}
    first = assign_cooks(DAYS, available, people)
    moved = next(d for d, p in first.items() if p == 4)
    available[4] = {d for d in DAYS if d != moved}  # 4-й больше не может в свой день
    second = assign_cooks(DAYS, available, people, {d: p for d, p in first.items() if p is not None})
    changed = [d for d in DAYS if first[d] != second[d]]
    assert moved in changed
    assert len(changed) <= 2  # пересчитался только его день и тот, куда он перешёл


def test_last_weekly() -> None:
    saturday_noon = datetime(2026, 11, 7, 12, 0, tzinfo=MSK)
    assert last_weekly(saturday_noon, 5, saturday_noon.timetz()) == saturday_noon
    assert last_weekly(saturday_noon - timedelta(minutes=1), 5, saturday_noon.timetz()) == saturday_noon - timedelta(
        weeks=1
    )


# --- сквозные: команды, планировщик, опрос ---


def scheduler(h: Harness, now: datetime) -> Scheduler:
    return Scheduler(
        h.bot, make_sessionmaker(h.engine), Settings(bot_token="999:x", gigachat_credentials="x"), lambda: now
    )


def vote(user: User, poll_id: str, options: list[int]) -> Update:
    answer = PollAnswer(poll_id=poll_id, option_ids=options, option_persistent_ids=[str(o) for o in options], user=user)
    return Update(update_id=next(upd_ids), poll_answer=answer)


def sent_texts(h: Harness, since: int) -> list[str]:
    return [m.text or "" for m in h.tg.sent_messages[since:]]


async def join_all(h: Harness) -> None:
    for u in (VASYA, PETYA, SASHA):
        await h.feed(msg(u, "/join"))


async def test_duty_set_show_and_saturday_reminder(harness: Harness) -> None:
    h = harness
    await join_all(h)
    await h.feed(msg(VASYA, "/duty set 08.11.2026 Vasiliy — Кух, Pyotr — В, Alexander — Г"))
    reply = h.reply()
    assert "Ротация с 8 ноября сохранена, напоминания включены" in reply
    assert "<pre>" in reply

    before = len(h.tg.sent_messages)
    await scheduler(h, datetime(2026, 11, 7, 18, 59, tzinfo=MSK)).tick()  # ещё рано
    assert sent_texts(h, before) == []
    await scheduler(h, datetime(2026, 11, 7, 19, 1, tzinfo=MSK)).tick()
    await scheduler(h, datetime(2026, 11, 7, 19, 2, tzinfo=MSK)).tick()  # второй раз не шлём
    reminders = sent_texts(h, before)
    assert len(reminders) == 1
    assert "воскресенье, 8 ноября" in reminders[0]
    assert "<b>Кух</b> — Vasiliy @vasya" in reminders[0]
    # Через неделю кухня у следующего
    await scheduler(h, datetime(2026, 11, 14, 19, 0, tzinfo=MSK)).tick()
    assert "<b>Кух</b> — Pyotr @petya" in sent_texts(h, before)[-1]
    # Бот лежал до воскресенья после обеда — напоминание уже не нужно
    count = len(h.tg.sent_messages)
    await scheduler(h, datetime(2026, 11, 22, 12, 30, tzinfo=MSK)).tick()
    assert len(h.tg.sent_messages) == count


async def test_duty_set_unknown_name(harness: Harness) -> None:
    await join_all(harness)
    await harness.feed(msg(VASYA, "/duty set 08.11.2026 Vasiliy — Кух, Зульфия — В"))
    assert "«Зульфия» нашёл никого" in harness.reply()


async def test_dinner_poll_votes_revote_and_deadline(harness: Harness) -> None:
    h = harness
    await join_all(h)
    await h.feed(msg(VASYA, "/dinner on"))
    saturday = datetime(2026, 11, 7, 12, 1, tzinfo=MSK)
    await scheduler(h, saturday).tick()
    await scheduler(h, saturday + timedelta(minutes=1)).tick()  # второй опрос не отправляем
    polls = [m for m in h.tg.sent if isinstance(m, SendPoll)]
    assert len(polls) == 1
    assert "9–15 ноября" in polls[0].question
    poll_id = h.tg.polls[-1].id

    before = len(h.tg.sent_messages)
    await h.feed(vote(VASYA, poll_id, [0, 1]))  # Пн, Вт
    await h.feed(vote(PETYA, poll_id, [0]))  # Пн
    assert sent_texts(h, before) == []  # ждём всех
    await h.feed(vote(SASHA, poll_id, [7]))  # не смогу
    schedule = sent_texts(h, before)
    assert len(schedule) == 1
    assert "Пн 9.11 — Pyotr" in schedule[0]
    assert "Вт 10.11 — Vasiliy" in schedule[0]
    assert "Ср 11.11 — 🤷 никто не записался" in schedule[0]
    assert "Проголосовали все (3)" in schedule[0]

    await h.feed(vote(SASHA, poll_id, [7]))  # тот же голос — график не изменился, не спамим
    assert len(sent_texts(h, before)) == 1
    await h.feed(vote(SASHA, poll_id, [2]))  # передумал: может в среду
    updated = sent_texts(h, before)
    assert len(updated) == 2
    assert "(обновлено)" in updated[1]
    assert "Ср 11.11 — Alexander" in updated[1]
    assert "обновлён" in (h.tg.last(EditMessageText).text or "")

    await scheduler(h, datetime(2026, 11, 9, 13, 1, tzinfo=MSK)).tick()  # дедлайн
    assert any(isinstance(m, StopPoll) for m in h.tg.sent)
    count = len(h.tg.sent_messages)
    await h.feed(vote(SASHA, poll_id, [3]))  # после дедлайна голоса не учитываем
    assert len(h.tg.sent_messages) == count


async def test_dinner_deadline_without_everyone(harness: Harness) -> None:
    h = harness
    await join_all(h)
    await h.feed(msg(VASYA, "/dinner poll"))
    poll_id = h.tg.polls[-1].id
    await h.feed(vote(VASYA, poll_id, [4]))
    async with make_sessionmaker(h.engine)() as s:
        poll = await repo.dinner_poll_by_tg(s, poll_id)
        assert poll is not None
        deadline = repo.utc_to_local(poll.deadline, MSK)
    before = len(h.tg.sent_messages)
    await scheduler(h, deadline + timedelta(minutes=1)).tick()
    final = sent_texts(h, before)
    assert len(final) == 1
    assert "Не проголосовали: Pyotr, Alexander" in final[0]
    assert "Голосование закрыто" in final[0]
    await h.feed(msg(VASYA, "/dinner poll"))  # на эту неделю уже было
    assert "уже есть" in (h.tg.last(SendMessage).text or "")


async def test_migrated_chat_keeps_household(session: AsyncSession) -> None:
    await repo.set_roster(session, CHAT.id, SUN, ["Кух"], [1])
    (await repo.features(session, CHAT.id)).duty_reminders = True
    await session.commit()
    await repo.migrate_chat(session, CHAT.id, -999)
    await session.commit()
    assert await repo.roster_of(session, CHAT.id) is None
    assert await repo.roster_of(session, -999) is not None
    assert await repo.chats_with(session, "duty_reminders") == [-999]
