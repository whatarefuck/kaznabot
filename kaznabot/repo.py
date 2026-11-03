"""Запросы к БД. Функции не коммитят — коммит делает вызывающий код."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from aiogram.types import User as TgUser
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kaznabot.ai.schemas import RosterEntry
from kaznabot.db import Draft, Exclusion, Expense, ExpenseItem, ItemParticipant, Member, Settlement, User, utcnow
from kaznabot.settlement import split_amount

log = logging.getLogger(__name__)


# --- пользователи ---


async def upsert_user(session: AsyncSession, tg: TgUser) -> User:
    """Находит пользователя по tg_id, а если его ещё нет — «забирает» заглушку с тем же @username."""
    user = await session.scalar(select(User).where(User.tg_id == tg.id))
    if user is None and tg.username:
        user = await _placeholder(session, tg.username)
        if user is not None:
            user.tg_id = tg.id
    if user is None:
        user = User(tg_id=tg.id, name=tg.full_name, username=tg.username)
        session.add(user)
        await session.flush()
        return user

    user.name = tg.full_name
    if tg.username and (user.username or "").lower() != tg.username.lower():
        user.username = tg.username
        # Человек сменил @username, а его уже успели представить под новым — сливаем заглушку
        if (stale := await _placeholder(session, tg.username)) is not None:
            await _merge_users(session, stale, into=user)
    return user


async def user_by_username(session: AsyncSession, username: str) -> User:
    """Пользователь с таким @username; если бот его ещё не видел — создаёт заглушку."""
    username = username.lstrip("@")
    user = await session.scalar(
        select(User)
        .where(func.lower(User.username) == username.lower())
        .order_by(User.tg_id.is_(None))  # настоящие аккаунты раньше заглушек
        .limit(1)
    )
    if user is None:
        user = User(tg_id=None, name=f"@{username}", username=username)
        session.add(user)
        await session.flush()
    return user


async def _placeholder(session: AsyncSession, username: str) -> User | None:
    q = select(User).where(User.tg_id.is_(None), func.lower(User.username) == username.lower())
    return (await session.scalars(q)).first()


async def _merge_users(session: AsyncSession, stale: User, into: User) -> None:
    log.info("merge user %s into %s", stale.id, into.id)
    for m in await session.scalars(select(Member).where(Member.user_id == stale.id)):
        own = await _member(session, m.chat_id, into.id)
        if own is None:
            m.user_id = into.id
        else:
            await _absorb(session, own, m)
    await session.execute(update(Expense).where(Expense.payer_id == stale.id).values(payer_id=into.id))
    await session.execute(update(Expense).where(Expense.created_by_id == stale.id).values(created_by_id=into.id))
    for p in await session.scalars(select(ItemParticipant).where(ItemParticipant.user_id == stale.id)):
        dup = await session.get(ItemParticipant, (p.item_id, into.id))
        await session.delete(p)
        if dup is None:
            session.add(ItemParticipant(item_id=p.item_id, user_id=into.id))
    await session.flush()
    await session.delete(stale)
    await session.flush()


async def _member(session: AsyncSession, chat_id: int, user_id: int) -> Member | None:
    q = select(Member).where(Member.chat_id == chat_id, Member.user_id == user_id)
    return (await session.scalars(q)).first()


async def _absorb(session: AsyncSession, own: Member, other: Member) -> None:
    """Две записи об одном человеке в одном чате → одна: имя и участие в расчёте не теряются."""
    own.display_name = own.display_name or other.display_name
    own.active = own.active or other.active
    await session.delete(other)


# --- участники чата ---


async def ensure_member(
    session: AsyncSession,
    chat_id: int,
    user: User,
    *,
    activate: bool = False,
    display_name: str | None = None,
) -> bool:
    """Заводит человека в чате (по умолчанию — просто «видели в чате»).

    True — если он только что стал участником расчёта.
    """
    member = await session.scalar(select(Member).where(Member.chat_id == chat_id, Member.user_id == user.id))
    if member is None:
        member = Member(chat_id=chat_id, user_id=user.id, active=False)
        session.add(member)
    became_active = activate and not member.active
    if activate:
        member.active = True
    if display_name:
        member.display_name = display_name
    await session.flush()
    return became_active


async def deactivate_member(session: AsyncSession, chat_id: int, user: User) -> bool:
    q = (
        update(Member)
        .where(Member.chat_id == chat_id, Member.user_id == user.id, Member.active.is_(True))
        .values(active=False)
        .returning(Member.id)
    )
    return (await session.scalars(q)).first() is not None


async def members(session: AsyncSession, chat_id: int) -> list[Member]:
    return list(await session.scalars(select(Member).where(Member.chat_id == chat_id).order_by(Member.id)))


async def active_member_ids(session: AsyncSession, chat_id: int) -> list[int]:
    return list(
        await session.scalars(
            select(Member.user_id).where(Member.chat_id == chat_id, Member.active.is_(True)).order_by(Member.id)
        )
    )


async def roster(session: AsyncSession, chat_id: int) -> list[RosterEntry]:
    """Люди чата в виде, пригодном для промпта и состояния графа."""
    return [
        RosterEntry(id=m.user_id, name=m.name, username=m.user.username, tg_name=m.user.name, active=m.active)
        for m in await members(session, chat_id)
    ]


async def display_names(session: AsyncSession, chat_id: int, user_ids: Iterable[int] = ()) -> dict[int, str]:
    """Имена в этом чате; для тех, кого нет среди участников, — имя из Telegram."""
    names = {m.user_id: m.name for m in await members(session, chat_id)}
    missing = set(user_ids) - names.keys()
    if missing:
        for u in await session.scalars(select(User).where(User.id.in_(missing))):
            names[u.id] = u.name
    return names


# --- траты ---


async def create_expense(
    session: AsyncSession,
    *,
    chat_id: int,
    payer_id: int,
    created_by_id: int,
    title: str | None,
    items: list[tuple[str, float, int, list[int]]],  # (название, кол-во, копейки, участники)
    purchased_on: date | None = None,
) -> Expense:
    expense = Expense(
        chat_id=chat_id,
        payer_id=payer_id,
        created_by_id=created_by_id,
        title=title,
        purchased_on=purchased_on,
        total=sum(amount for _, _, amount, _ in items),
        items=[
            ExpenseItem(
                name=name,
                quantity=qty,
                amount=amount,
                participants=[ItemParticipant(user_id=uid) for uid in people],
            )
            for name, qty, amount, people in items
        ],
    )
    session.add(expense)
    await session.flush()
    return expense


async def expenses_since(session: AsyncSession, chat_id: int, since: datetime | None) -> list[Expense]:
    q = select(Expense).where(Expense.chat_id == chat_id, Expense.deleted_at.is_(None))
    if since is not None:
        q = q.where(Expense.created_at > since)
    # populate_existing: в сессии могли остаться загруженные раньше траты (например, до слияния заглушки)
    q = q.order_by(Expense.created_at).execution_options(populate_existing=True)
    return list(await session.scalars(q))


async def get_expense(session: AsyncSession, chat_id: int, expense_id: int) -> Expense | None:
    q = select(Expense).where(Expense.chat_id == chat_id, Expense.id == expense_id)
    return (await session.scalars(q)).first()


async def get_expense_by_bot_msg(session: AsyncSession, chat_id: int, bot_msg_id: int) -> Expense | None:
    q = select(Expense).where(Expense.chat_id == chat_id, Expense.bot_msg_id == bot_msg_id)
    return (await session.scalars(q)).first()


def set_deleted(expense: Expense, deleted: bool) -> None:
    expense.deleted_at = utcnow() if deleted else None


def expense_day(e: Expense, tz: ZoneInfo) -> date:
    """День покупки; у старых трат без даты — день, когда её внесли."""
    return e.purchased_on or e.created_at.replace(tzinfo=UTC).astimezone(tz).date()


# --- исключение человека из чеков за период ---


@dataclass
class ExclusionHit:
    expense: Expense
    item: ExpenseItem
    share: int  # копейки — доля человека в позиции сейчас


@dataclass
class ExclusionPreview:
    hits: list[ExclusionHit] = field(default_factory=list)
    solo: int = 0  # позиций только на этого человека — их не трогаем
    settled: int = 0  # чеков до последнего обнуления — долги по ним уже закрыты

    @property
    def receipts(self) -> int:
        return len({h.expense.id for h in self.hits})

    @property
    def amount(self) -> int:
        return sum(h.share for h in self.hits)


async def preview_exclusion(
    session: AsyncSession, chat_id: int, user_id: int, start: date, end: date, tz: ZoneInfo
) -> ExclusionPreview:
    since = await last_settlement_at(session, chat_id)
    preview = ExclusionPreview()
    for e in await expenses_since(session, chat_id, None):
        if not start <= expense_day(e, tz) <= end:
            continue
        mine = [(n, item) for n, item in enumerate(e.items) if user_id in item.participant_ids]
        if not mine:
            continue
        if since is not None and e.created_at <= since:
            preview.settled += 1
            continue
        for n, item in mine:
            ids = item.participant_ids
            if ids == (user_id,):
                preview.solo += 1
                continue
            # salt = номер позиции, как в reports.expense_entries: доля совпадает с той, что в /debts
            preview.hits.append(ExclusionHit(e, item, split_amount(item.amount, ids, salt=n)[user_id]))
    return preview


async def apply_exclusion(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    start: date,
    end: date,
    created_by_id: int,
    tz: ZoneInfo,
) -> tuple[Exclusion, ExclusionPreview]:
    """Убирает человека из позиций за период. Список позиций считается заново: пока висело
    подтверждение, могли добавить или удалить чеки."""
    preview = await preview_exclusion(session, chat_id, user_id, start, end, tz)
    for hit in preview.hits:
        hit.item.participants = [p for p in hit.item.participants if p.user_id != user_id]
    record = Exclusion(
        chat_id=chat_id,
        user_id=user_id,
        created_by_id=created_by_id,
        date_from=start,
        date_to=end,
        item_ids=[h.item.id for h in preview.hits],
    )
    session.add(record)
    await session.flush()
    return record, preview


async def get_exclusion(session: AsyncSession, chat_id: int, exclusion_id: int) -> Exclusion | None:
    q = select(Exclusion).where(Exclusion.chat_id == chat_id, Exclusion.id == exclusion_id)
    return (await session.scalars(q)).first()


async def revert_exclusion(session: AsyncSession, exclusion: Exclusion) -> int:
    """Возвращает человека в позиции. Возвращает, во сколько позиций вернули."""
    restored = 0
    items = await session.scalars(select(ExpenseItem).where(ExpenseItem.id.in_(exclusion.item_ids)))
    for item in items:
        if exclusion.user_id not in item.participant_ids:
            item.participants.append(ItemParticipant(user_id=exclusion.user_id))
            restored += 1
    exclusion.reverted_at = utcnow()
    await session.flush()
    return restored


# --- обнуления ---


async def last_settlement_at(session: AsyncSession, chat_id: int) -> datetime | None:
    q = select(Settlement.created_at).where(Settlement.chat_id == chat_id).order_by(Settlement.created_at.desc())
    return (await session.scalars(q)).first()


async def add_settlement(session: AsyncSession, chat_id: int, by: User) -> Settlement:
    s = Settlement(chat_id=chat_id, created_by_id=by.id)
    session.add(s)
    await session.flush()
    return s


# --- смена id чата ---


@dataclass
class ChatMove:
    expenses: int  # перенесено трат
    cancelled_threads: list[str]  # треды графа незакрытых черновиков — их кнопки в новом чате не работают


async def migrate_chat(session: AsyncSession, old: int, new: int) -> ChatMove:
    """Группа стала супергруппой, и Telegram выдал чату новый id: переносим всё на него.

    Telegram присылает об этом два сообщения (в старый и в новый чат) в любом порядке,
    поэтому повторный вызов ничего не меняет.
    """
    for m in list(await session.scalars(select(Member).where(Member.chat_id == old))):
        own = await _member(session, new, m.user_id)
        if own is None:
            m.chat_id = new
        else:  # человек уже успел написать в новый чат
            await _absorb(session, own, m)
    moved = 0
    for model in (Expense, Settlement, Exclusion):
        move = update(model).where(model.chat_id == old).values(chat_id=new).returning(model.id)
        ids = list(await session.scalars(move))
        if model is Expense:
            moved = len(ids)
    # У сообщений в новом чате другие id, поэтому черновики со старыми кнопками закрываем
    close = (
        update(Draft)
        .where(Draft.chat_id == old, Draft.status.in_(("pending", "busy")))
        .values(status="cancelled")
        .returning(Draft.thread_id)
    )
    threads = list(await session.scalars(close))
    await session.flush()
    return ChatMove(expenses=moved, cancelled_threads=threads)


# --- индекс черновиков ---


async def add_draft(
    session: AsyncSession, *, thread_id: str, chat_id: int, bot_msg_id: int, author_tg_id: int
) -> Draft:
    draft = Draft(thread_id=thread_id, chat_id=chat_id, bot_msg_id=bot_msg_id, author_tg_id=author_tg_id)
    session.add(draft)
    await session.flush()
    return draft


async def draft_by_msg(session: AsyncSession, chat_id: int, bot_msg_id: int) -> Draft | None:
    q = select(Draft).where(Draft.chat_id == chat_id, Draft.bot_msg_id == bot_msg_id)
    return (await session.scalars(q)).first()


async def switch_draft(session: AsyncSession, draft_id: int, old: str, new: str) -> bool:
    """Атомарно меняет статус. False — кто-то успел раньше (двойной клик, параллельное уточнение)."""
    q = update(Draft).where(Draft.id == draft_id, Draft.status == old).values(status=new).returning(Draft.id)
    return (await session.scalars(q)).first() is not None


async def release_busy_drafts(session: AsyncSession) -> None:
    """После перезапуска: черновики, которые обрабатывались в момент падения, снова доступны."""
    await session.execute(update(Draft).where(Draft.status == "busy").values(status="pending"))
