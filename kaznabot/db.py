from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, BigInteger, Date, DateTime, ForeignKey, String, UniqueConstraint, event, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Все даты в БД — naive UTC (SQLite не хранит таймзону)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # None — «заглушка»: человека представили по @username, но он ещё не писал в чат
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    name: Mapped[str] = mapped_column(String(128))  # имя из Telegram
    username: Mapped[str | None] = mapped_column(String(64), index=True)


class Member(Base):
    """Человек в конкретном чате. active — участвует в общем расчёте («на всех»)."""

    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("chat_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    display_name: Mapped[str | None] = mapped_column(String(64))  # как его представили: «Саша»
    active: Mapped[bool] = mapped_column(default=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(lazy="joined")

    @property
    def name(self) -> str:
        return self.display_name or self.user.name


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    payer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str | None] = mapped_column(String(256))
    total: Mapped[int]  # копейки
    bot_msg_id: Mapped[int | None] = mapped_column(BigInteger)  # сообщение «✅ Внесено» — для /delete ответом
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # День покупки (из чека или «вчера»). У трат, внесённых до появления поля, — NULL: берём день ввода
    purchased_on: Mapped[date | None] = mapped_column(Date)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    items: Mapped[list["ExpenseItem"]] = relationship(
        back_populates="expense", cascade="all, delete-orphan", lazy="selectin", order_by="ExpenseItem.id"
    )


class ExpenseItem(Base):
    __tablename__ = "expense_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    expense_id: Mapped[int] = mapped_column(ForeignKey("expenses.id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    quantity: Mapped[float] = mapped_column(default=1)
    amount: Mapped[int]  # копейки

    expense: Mapped[Expense] = relationship(back_populates="items")
    participants: Mapped[list["ItemParticipant"]] = relationship(cascade="all, delete-orphan", lazy="selectin")

    @property
    def participant_ids(self) -> tuple[int, ...]:
        return tuple(sorted(p.user_id for p in self.participants))


class ItemParticipant(Base):
    """На кого делится позиция. Фиксируется при подтверждении, поэтому новые соседи не платят за старое."""

    __tablename__ = "item_participants"

    item_id: Mapped[int] = mapped_column(ForeignKey("expense_items.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)


class Exclusion(Base):
    """Человека убрали из чеков за период. item_ids — позиции, из которых его убрали (для отмены)."""

    __tablename__ = "exclusions"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    date_from: Mapped[date] = mapped_column(Date)
    date_to: Mapped[date] = mapped_column(Date)
    item_ids: Mapped[list[int]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime)


class Settlement(Base):
    """«Обнуление долгов». Текущий период — всё, что добавлено после последнего обнуления."""

    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Draft(Base):
    """Черновик, ждущий подтверждения. Само содержимое живёт в состоянии графа (чекпоинтер LangGraph),
    здесь — индекс: какое сообщение бота какому треду графа соответствует и кто его автор."""

    __tablename__ = "drafts"
    __table_args__ = (UniqueConstraint("chat_id", "bot_msg_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), unique=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    bot_msg_id: Mapped[int] = mapped_column(BigInteger)
    author_tg_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / busy / confirmed / cancelled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def make_engine(url: str) -> AsyncEngine:
    if url.startswith("sqlite") and ":///" in url:
        Path(url.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url)
    if url.startswith("sqlite"):
        # WAL: чтения не блокируются записью, пока другой апдейт ждёт ответа GigaChat
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


# Колонки, добавленные после первого выпуска: create_all новые колонки в старые таблицы не добавляет
ADDED_COLUMNS = {"expenses": {"purchased_on": "DATE"}}


async def create_tables(engine: AsyncEngine) -> None:
    # Для прототипа хватает create_all и ручного ADD COLUMN; если схема начнёт меняться сильнее — Alembic.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(conn: Any) -> None:
    insp = inspect(conn)
    for table, columns in ADDED_COLUMNS.items():
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, sql_type in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
