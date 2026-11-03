"""Дежурства по уборке и голосование за ужины: команды, ответы в опросе и то, что шлёт планировщик."""

import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from datetime import date, datetime, timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import InputPollOption, Message, PollAnswer, ReplyParameters
from sqlalchemy.ext.asyncio import AsyncSession

from kaznabot import repo
from kaznabot.ai.schemas import name_matches
from kaznabot.config import Settings
from kaznabot.db import DinnerPoll, DutyRoster, User, utcnow
from kaznabot.household import (
    WEEKDAYS_SHORT,
    Rotation,
    assign_cooks,
    human_date,
    next_monday,
    next_sunday,
    parse_rotation,
    rotate,
    week_range,
)

log = logging.getLogger(__name__)
router = Router(name="household")

SHOW_WEEKS = 4
CANT = "Не смогу на этой неделе"

DUTY_HELP = """\
🧹 <b>Дежурства</b>
/duty — кто что убирает в ближайшие воскресенья
<code>/duty set 08.11 Данил — Кух, Искандар — В, …</code> — задать ротацию: кто какую зону \
убирает в это воскресенье; дальше зоны переходят по кругу к следующему по списку
/duty on · /duty off — напоминание в субботу вечером"""

DINNER_HELP = """\
🍽 <b>Ужины</b>
/dinner — график ужинов на неделю
/dinner poll — устроить голосование прямо сейчас
/dinner on · /dinner off — голосование каждую субботу в 12:00, итог — после всех голосов \
или в понедельник в 13:00"""


def to(message_id: int) -> ReplyParameters:
    # Если сообщение удалили, ответ всё равно уйдёт — просто без цитаты
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True)


def short_day(d: date) -> str:
    return f"{WEEKDAYS_SHORT[d.weekday()]} {d.day}.{d.month:02d}"


async def people_info(session: AsyncSession, chat_id: int, ids: set[int]) -> tuple[dict[int, str], dict[int, User]]:
    names = await repo.display_names(session, chat_id, ids)
    users = {uid: u for uid in ids if (u := await session.get(User, uid)) is not None}
    return names, users


def mention(uid: int, names: dict[int, str], users: dict[int, User]) -> str:
    """Имя и упоминание, чтобы человеку пришло уведомление."""
    name = escape(names.get(uid, "?"))
    user = users.get(uid)
    if user is not None and user.username:
        return f"{name} @{escape(user.username)}"
    if user is not None and user.tg_id:
        return f'<a href="tg://user?id={user.tg_id}">{name}</a>'
    return name


def as_rotation(roster: DutyRoster) -> Rotation:
    return Rotation(anchor=roster.anchor, zones=tuple(roster.zones), people=tuple(roster.people))


# --- дежурства ---


async def duty_reminder_text(session: AsyncSession, chat_id: int, roster: DutyRoster, sunday: date) -> str:
    rotation = as_rotation(roster)
    names, users = await people_info(session, chat_id, set(rotation.people))
    lines = [f"🧹 <b>Завтра дежурство</b> — воскресенье, {human_date(sunday)}", ""]
    for person, zones in rotation.by_person(sunday).items():
        if zones:
            lines.append(f"• <b>{escape(' + '.join(zones))}</b> — {mention(person, names, users)}")
    free = [p for p, zones in rotation.by_person(sunday).items() if not zones]
    if free:
        lines.append(f"\nОтдыхают: {', '.join(escape(names.get(p, '?')) for p in free)}")
    lines.append("\nВесь график — /duty")
    return "\n".join(lines)


async def send_duty_reminder(bot: Bot, session: AsyncSession, chat_id: int, sunday: date) -> bool:
    roster = await repo.roster_of(session, chat_id)
    if roster is None:
        return False
    await bot.send_message(chat_id, await duty_reminder_text(session, chat_id, roster, sunday))
    return True


async def duty_table(session: AsyncSession, chat_id: int, roster: DutyRoster, first: date, weeks: int) -> str:
    """Таблица «человек × воскресенье» моноширинным текстом."""
    rotation = as_rotation(roster)
    names, _ = await people_info(session, chat_id, set(rotation.people))
    sundays = [first + timedelta(weeks=w) for w in range(weeks)]
    cells = {s: rotation.by_person(s) for s in sundays}
    heads = [f"{s.day}.{s.month:02d}" for s in sundays]
    name_w = max(len(names.get(p, "?")) for p in rotation.people)
    cols = [
        max(len(h), *(len("+".join(cells[s][p]) or "—") for p in rotation.people))
        for h, s in zip(heads, sundays, strict=True)
    ]
    rows = [" " * name_w + "  " + "  ".join(h.ljust(w) for h, w in zip(heads, cols, strict=True))]
    for p in rotation.people:
        zones = ["+".join(cells[s][p]) or "—" for s in sundays]
        rows.append(
            names.get(p, "?").ljust(name_w) + "  " + "  ".join(z.ljust(w) for z, w in zip(zones, cols, strict=True))
        )
    return "<pre>" + escape("\n".join(r.rstrip() for r in rows)) + "</pre>"


async def match_people(session: AsyncSession, chat_id: int, said: list[str]) -> list[int] | str:
    entries = await repo.roster(session, chat_id)
    result = []
    for name in said:
        exact = [e for e in entries if e["name"].lower() == name.lower()]
        found = exact or [e for e in entries if name_matches(name, e, is_author=False)]
        if len(found) != 1:
            hint = "нескольких" if found else "никого"
            return f"по имени «{name}» нашёл {hint} — проверьте /people"
        if found[0]["id"] in result:
            return f"«{name}» указан дважды"
        result.append(found[0]["id"])
    return result


@router.message(Command("duty"))
async def cmd_duty(message: Message, command: CommandObject, session: AsyncSession, settings: Settings) -> None:
    chat_id = message.chat.id
    arg = (command.args or "").strip()
    today = datetime.now(settings.tz).date()
    feats = await repo.features(session, chat_id)
    roster = await repo.roster_of(session, chat_id)

    if arg.lower() in ("on", "off"):
        if arg.lower() == "on" and roster is None:
            await message.reply("Сначала задайте ротацию.\n\n" + DUTY_HELP)
            return
        feats.duty_reminders = arg.lower() == "on"
        await session.commit()
        state = "буду напоминать в субботу вечером" if feats.duty_reminders else "напоминания выключены"
        await message.reply(f"🧹 Дежурства: {state}.")
        return

    if arg.lower().startswith("set"):
        draft = parse_rotation(arg[3:], today)
        if isinstance(draft, str):
            await message.reply(f"🤔 {escape(draft)}.\n\n{DUTY_HELP}")
            return
        people = await match_people(session, chat_id, [who for who, _ in draft.pairs])
        if isinstance(people, str):
            await message.reply(f"🤔 {escape(people)}.")
            return
        await repo.set_roster(session, chat_id, draft.anchor, [zone for _, zone in draft.pairs], people)
        feats.duty_reminders = True
        await session.commit()
        roster = await repo.roster_of(session, chat_id)
        assert roster is not None
        table = await duty_table(session, chat_id, roster, draft.anchor, SHOW_WEEKS)
        await message.reply(f"✅ Ротация с {human_date(draft.anchor)} сохранена, напоминания включены.\n\n{table}")
        return

    if roster is None:
        await message.reply("Ротации пока нет.\n\n" + DUTY_HELP)
        return
    first = max(next_sunday(today), roster.anchor)
    table = await duty_table(session, chat_id, roster, first, SHOW_WEEKS)
    state = "в субботу вечером пришлю напоминание" if feats.duty_reminders else "напоминания выключены (/duty on)"
    await message.reply(f"🧹 <b>Дежурства по воскресеньям</b>\n\n{table}\n{state}")


# --- ужины ---

_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


async def post_dinner_poll(
    bot: Bot, session: AsyncSession, chat_id: int, week_start: date, settings: Settings
) -> DinnerPoll:
    days = [week_start + timedelta(days=i) for i in range(7)]
    deadline = repo.deadline_utc(week_start, settings.dinner_deadline_time, settings.tz)
    question = (
        f"🍳 Кто когда готовит ужин {week_range(days[0], days[-1])}? Отметьте все дни, когда сможете. "
        f"Голосуем до пн {settings.dinner_deadline_time:%H:%M}."
    )
    msg = await bot.send_poll(
        chat_id,
        question,
        options=[*(InputPollOption(text=short_day(d)) for d in days), InputPollOption(text=CANT)],
        is_anonymous=False,
        allows_multiple_answers=True,
    )
    assert msg.poll is not None
    poll = DinnerPoll(
        chat_id=chat_id,
        week_start=week_start,
        poll_id=msg.poll.id,
        message_id=msg.message_id,
        days=[d.isoformat() for d in days],
        deadline=deadline,
    )
    session.add(poll)
    await session.commit()
    return poll


def dinner_text(
    poll: DinnerPoll,
    plan: dict[date, int | None],
    names: dict[int, str],
    voted: int,
    missing: list[str],
    *,
    updated: bool,
    closed: bool,
    deadline: datetime,
) -> str:
    days = sorted(plan)
    title = f"🍽 <b>Ужины {week_range(days[0], days[-1])}</b>" + (" (обновлено)" if updated else "")
    lines = [title, ""]
    for d in days:
        cook = plan[d]
        lines.append(
            f"{short_day(d)} — {escape(names.get(cook, '?')) if cook is not None else '🤷 никто не записался'}"
        )
    lines.append("")
    if missing:
        lines.append(f"Проголосовали {voted}. Не проголосовали: {escape(', '.join(missing))}.")
    else:
        lines.append(f"Проголосовали все ({voted}).")
    if closed:
        lines.append("Голосование закрыто.")
    else:
        lines.append(
            f"Поменять дни можно в опросе до {short_day(deadline.date())} {deadline:%H:%M} — пришлю новый график."
        )
    return "\n".join(lines)


async def refresh_dinner(
    bot: Bot, session: AsyncSession, poll: DinnerPoll, settings: Settings, *, closing: bool = False
) -> None:
    """Пересчитать график ужинов и прислать его, если пора и если он изменился.

    Первый раз график приходит, когда проголосовали все участники расчёта или наступил дедлайн.
    Потом — заново после каждого переголосования, которое меняет график.
    """
    async with _locks[poll.id]:
        await session.refresh(poll)
        if poll.closed_at is not None:
            return
        votes = await repo.dinner_votes(session, poll)
        members = await repo.active_member_ids(session, poll.chat_id)
        everyone = set(members) | set(votes)
        all_voted = set(members) <= set(votes)
        if closing:
            poll.closed_at = utcnow()
            try:
                await bot.stop_poll(poll.chat_id, poll.message_id)
            except Exception:  # опрос могли удалить или уже закрыть — итог всё равно публикуем
                log.warning("stop_poll failed in chat %s", poll.chat_id, exc_info=True)
        if poll.schedule_msg_id is None and not (all_voted or closing):
            await session.commit()
            return

        days = [date.fromisoformat(d) for d in poll.days]
        available = {uid: {days[i] for i in opts if i < len(days)} for uid, opts in votes.items()}
        # Порядок для ничьих сдвигается каждую неделю, чтобы второй ужин не доставался одному и тому же
        order = rotate(sorted(everyone), poll.week_start.isocalendar().week)
        previous = {date.fromisoformat(k): v for k, v in (poll.schedule or {}).items() if v is not None}
        plan = assign_cooks(days, available, order, previous)
        schedule = {d.isoformat(): p for d, p in plan.items()}
        if poll.schedule_msg_id is not None and schedule == poll.schedule:
            await session.commit()
            return

        names = await repo.display_names(session, poll.chat_id, everyone)
        missing = [names.get(uid, "?") for uid in members if uid not in votes]
        deadline = repo.utc_to_local(poll.deadline, settings.tz)
        text = dinner_text(
            poll,
            plan,
            names,
            len(votes),
            missing,
            updated=poll.schedule_msg_id is not None,
            closed=closing,
            deadline=deadline,
        )
        sent = await bot.send_message(poll.chat_id, text)
        if poll.schedule_msg_id is not None:
            with suppress(TelegramBadRequest):
                await bot.edit_message_text(
                    "⤵️ График ужинов обновлён — см. ниже.", chat_id=poll.chat_id, message_id=poll.schedule_msg_id
                )
        poll.schedule, poll.schedule_msg_id = schedule, sent.message_id
        await session.commit()


@router.poll_answer(F.user)
async def on_poll_answer(answer: PollAnswer, bot: Bot, session: AsyncSession, settings: Settings) -> None:
    poll = await repo.dinner_poll_by_tg(session, answer.poll_id)
    if poll is None or poll.closed_at is not None or answer.user is None:
        return
    user = await repo.upsert_user(session, answer.user)
    await repo.ensure_member(session, poll.chat_id, user)
    await repo.set_dinner_vote(session, poll, user, list(answer.option_ids))
    await session.commit()
    await refresh_dinner(bot, session, poll, settings)


@router.message(Command("dinner"))
async def cmd_dinner(
    message: Message, command: CommandObject, bot: Bot, session: AsyncSession, settings: Settings
) -> None:
    chat_id = message.chat.id
    arg = (command.args or "").strip().lower()
    feats = await repo.features(session, chat_id)
    if arg in ("on", "off"):
        feats.dinner_polls = arg == "on"
        await session.commit()
        state = (
            f"голосование каждую субботу в {settings.dinner_poll_time:%H:%M}"
            if feats.dinner_polls
            else "еженедельное голосование выключено"
        )
        await message.reply(f"🍽 Ужины: {state}.")
        return
    if arg == "poll":
        week_start = next_monday(datetime.now(settings.tz).date())
        existing = await repo.dinner_poll_for_week(session, chat_id, week_start)
        if existing is not None:
            await message.answer(
                "Голосование на эту неделю уже есть — вот оно.", reply_parameters=to(existing.message_id)
            )
            return
        await post_dinner_poll(bot, session, chat_id, week_start, settings)
        return

    poll = await repo.latest_dinner_poll(session, chat_id)
    auto = (
        f"голосование каждую субботу в {settings.dinner_poll_time:%H:%M}"
        if feats.dinner_polls
        else "еженедельное голосование выключено (/dinner on)"
    )
    if poll is None:
        await message.reply(f"Голосований за ужины ещё не было; {auto}.\n\n{DINNER_HELP}")
    elif poll.schedule_msg_id is not None:
        await message.answer(f"Последний график — вот он; {auto}.", reply_parameters=to(poll.schedule_msg_id))
    else:
        votes = await repo.dinner_votes(session, poll)
        deadline = repo.utc_to_local(poll.deadline, settings.tz)
        await message.reply(
            f"Голосуем: {len(votes)} голосов, итог — когда проголосуют все или в {short_day(deadline.date())} "
            f"{deadline:%H:%M}; {auto}.",
            reply_parameters=to(poll.message_id),
        )
