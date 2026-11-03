"""Схемы для structured output GigaChat и внутренний черновик траты (в копейках)."""

from datetime import date, timedelta
from typing import Any, Literal, NotRequired, Required, TypedDict

from pydantic import BaseModel, Field

from kaznabot.money import rub_to_kop

# --- то, что возвращает модель ---


class PersonName(BaseModel):
    ref: str = Field(
        description="Кого называют: упоминание из сообщения (@username), id:N из списка людей или self (автор про себя)"
    )
    name: str = Field(description="Имя в именительном падеже с заглавной буквы, например «Саша»")


class Intent(BaseModel):
    """Что хочет автор сообщения."""

    intent: Literal["introduce", "expense", "exclude", "debts", "spent", "other"] = Field(
        description="introduce — знакомит с людьми; expense — сообщает о покупке; "
        "exclude — просит убрать человека из чеков за период; "
        "debts — спрашивает, кто кому должен; spent — спрашивает про траты; other — другое"
    )
    people: list[PersonName] = Field(default_factory=list, description="Только для introduce: кого как зовут")


class ReceiptItem(BaseModel):
    name: str = Field(description="Короткое понятное название позиции")
    quantity: float = Field(default=1, description="Количество")
    amount: float = Field(description="Итоговая стоимость позиции в рублях (цена × количество, со скидками)")
    for_whom: list[int] | None = Field(
        default=None,
        description="id людей, на которых делится ТОЛЬКО эта позиция; null — как весь чек",
    )


class Receipt(BaseModel):
    """Покупки, которые нужно внести в общие траты."""

    is_purchase: bool = Field(description="true, если в сообщении есть покупки с суммами")
    store: str | None = Field(default=None, description="Магазин, если известен")
    purchase_date: str | None = Field(
        default=None,
        description="Дата покупки ГГГГ-ММ-ДД, если она есть в чеке или тексте («вчера», «12 октября»); иначе null",
    )
    payer_id: int | None = Field(default=None, description="id того, кто заплатил; null — тот, кто прислал чек")
    shared_with: list[int] | None = Field(
        default=None,
        description="id людей, на которых делится весь чек; null — на всех участников общего расчёта",
    )
    items: list[ReceiptItem] = Field(default_factory=list)
    note: str | None = Field(default=None, description="Коротко: что исключено, кого не нашёл, что не прочиталось")


# --- черновик внутри бота ---


class DraftItem(BaseModel):
    name: str
    quantity: float = 1
    amount: int  # копейки
    for_whom: list[int] | None = None


class Draft(BaseModel):
    store: str | None = None
    purchase_date: date | None = None  # None — купили в день ввода
    payer_id: int
    shared_with: list[int] | None = None
    items: list[DraftItem] = []
    note: str | None = None

    @property
    def total(self) -> int:
        return sum(i.amount for i in self.items)

    def participants(self, default_ids: list[int]) -> list[list[int]]:
        """На кого делится каждая позиция: своя группа → группа чека → все участники + плательщик."""
        everyone = sorted({*default_ids, self.payer_id})
        return [sorted(set(i.for_whom or self.shared_with or everyone)) for i in self.items]

    def to_llm(self) -> Receipt:
        return Receipt(
            is_purchase=True,
            store=self.store,
            purchase_date=self.purchase_date.isoformat() if self.purchase_date else None,
            payer_id=self.payer_id,
            shared_with=self.shared_with,
            note=self.note,
            items=[
                ReceiptItem(name=i.name, quantity=i.quantity, amount=i.amount / 100, for_whom=i.for_whom)
                for i in self.items
            ],
        )


MAX_BACKDATE = timedelta(days=366)


def parse_day(value: str | None, today: date) -> date | None:
    """Дата от модели: ГГГГ-ММ-ДД не из будущего и не старше года. Иначе None."""
    if not value:
        return None
    try:
        day = date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None
    return day if today - MAX_BACKDATE <= day <= today else None


def to_draft(r: Receipt, known_ids: set[int], default_payer: int, today: date) -> Draft:
    """Переводит ответ модели в копейки и выкидывает id, которых нет в чате (модель могла их выдумать)."""
    unknown: set[int] = set()

    def people(ids: list[int] | None) -> list[int] | None:
        if not ids:
            return None
        unknown.update(i for i in ids if i not in known_ids)
        valid = sorted({i for i in ids if i in known_ids})
        return valid or None

    payer = r.payer_id if r.payer_id in known_ids else default_payer
    if r.payer_id is not None and r.payer_id not in known_ids:
        unknown.add(r.payer_id)
    shared_with = people(r.shared_with)
    items = [
        DraftItem(
            name=i.name.strip(),
            quantity=i.quantity or 1,
            amount=rub_to_kop(i.amount),
            for_whom=people(i.for_whom),
        )
        for i in r.items
        if i.amount > 0
    ]
    note = r.note
    if unknown:
        extra = "не нашёл в чате людей с id " + ", ".join(map(str, sorted(unknown)))
        note = f"{note}; {extra}" if note else extra
    day = parse_day(r.purchase_date, today)
    if day == today:
        day = None
    return Draft(store=r.store, purchase_date=day, payer_id=payer, shared_with=shared_with, items=items, note=note)


class ExcludeRequest(BaseModel):
    """Убрать человека из общих чеков за период: его доля в этих чеках переходит остальным."""

    person_name: str | None = Field(default=None, description="Как этот человек назван в просьбе, слово в слово")
    person_id: int | None = Field(default=None, description="id человека из списка людей; null — если его нет в списке")
    date_from: str | None = Field(default=None, description="Первый день периода, ГГГГ-ММ-ДД")
    date_to: str | None = Field(
        default=None, description="Последний день периода включительно, ГГГГ-ММ-ДД; null — по сегодня"
    )
    note: str | None = Field(default=None, description="Коротко, если что-то в просьбе непонятно")


class ExclusionPlan(BaseModel):
    """Проверенный запрос на исключение."""

    person_id: int
    date_from: date
    date_to: date
    note: str | None = None


SELF_WORDS = {"я", "меня", "мне", "мной", "себя", "сам", "сама"}


def _norm(word: str) -> str:
    return word.strip().lstrip("@").lower().replace("ё", "е")


def name_matches(said: str, person: "RosterEntry", is_author: bool) -> bool:
    """Похоже ли имя из просьбы на человека: «Арслана» ~ «Арслан», «Лёши» ~ «Лёша».
    Сравниваем начало слова (падежи меняют окончание) с именем в чате, в Telegram и с @username."""
    said = _norm(said)
    if said in SELF_WORDS:
        return is_author
    candidates = [person["name"], person["tg_name"], person.get("username") or ""]
    words = [_norm(w) for c in candidates for w in c.split() if w]
    stem = said.split()[0][:3] if said else ""
    return bool(stem) and any(w.startswith(stem) for w in words)


def to_exclusion(r: ExcludeRequest, roster: "list[RosterEntry]", author_id: int, today: date) -> ExclusionPlan | str:
    """ExclusionPlan или текст, чего не хватает."""
    person = next((p for p in roster if p["id"] == r.person_id), None)
    who = f"«{r.person_name}»" if r.person_name else "человека"
    # Модель иногда подставляет первого попавшегося, если нужного человека нет в списке
    if person is None or (r.person_name and not name_matches(r.person_name, person, person["id"] == author_id)):
        return f"не нашёл {who} среди людей чата — проверьте /people"
    start = parse_day(r.date_from, today)
    end = parse_day(r.date_to, today) if r.date_to else today
    if start is None or end is None:
        return "не понял даты — напишите, например, «с 12 по 18 октября»"
    if start > end:
        start, end = end, start
    return ExclusionPlan(person_id=r.person_id, date_from=start, date_to=end, note=r.note)


# --- словари, которые ходят между графом, промптами и хендлерами (хранятся в чекпоинте как есть) ---


class RosterEntry(TypedDict):
    id: int  # users.id
    name: str  # имя в чате: как представили, иначе имя из Telegram
    username: str | None
    tg_name: str
    active: bool  # участвует в общем расчёте


class Mention(TypedDict):
    ref: str  # «@nick» или «id:5» (text_mention — человек без @username)
    label: str  # как упоминание выглядит в тексте
    user_id: NotRequired[int]


class Decision(TypedDict):
    action: Literal["ok", "cancel", "refine"]
    by: int  # users.id того, кто нажал или ответил
    text: NotRequired[str]  # уточнение
    bot_msg_id: NotRequired[int]  # сообщение с черновиком — станет «✅ Внесено»


class DraftView(TypedDict):
    """Всё, что нужно, чтобы нарисовать черновик без походов в БД."""

    kind: Literal["draft"]
    draft: dict[str, Any]  # Draft.model_dump()
    names: dict[str, str]  # str(users.id) → имя
    participants: list[list[int]]  # на кого делится каждая позиция
    everyone: list[int]  # «все» — участники расчёта + плательщик
    error: str | None


class ExclusionView(TypedDict):
    """Что изменится, если убрать человека из чеков за период."""

    kind: Literal["exclusion"]
    person: str
    date_from: str  # ГГГГ-ММ-ДД
    date_to: str
    receipts: int  # чеков, где его доля уменьшится
    items: int  # позиций
    amount: int  # копейки — его доля в этих позициях, перейдёт к остальным
    solo: int  # позиций только на него — не трогаем
    settled: int  # чеков до последнего обнуления — не трогаем
    note: str | None
    error: str | None  # llm — не удалось уточнить; nothing — нечего применять


type PendingView = DraftView | ExclusionView


class IntroducedPerson(TypedDict):
    name: str
    label: str


class Result(TypedDict, total=False):
    kind: Required[
        Literal[
            "introduced",
            "saved",
            "excluded",
            "unclear",
            "cancelled",
            "not_purchase",
            "error",
            "ignored",
            "debts",
            "spent",
            "other",
        ]
    ]
    reason: str  # unclear: чего не хватает
    people: list[IntroducedPerson]  # introduced
    skipped: list[str]  # introduced
    expense_id: int  # saved
    joined: bool  # saved: плательщик только что стал участником
    view: DraftView  # saved
    exclusion_id: int  # excluded
    exclusion: ExclusionView  # excluded
