"""Сводки по периоду и отрисовка черновика."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from kaznabot import repo
from kaznabot.ai.schemas import Draft, DraftView, ExclusionView, PendingView
from kaznabot.charts import ChartRow
from kaznabot.db import Expense
from kaznabot.money import fmt_rub
from kaznabot.settlement import Entry, Transfer, settle, totals

MAX_LIST_LINES = 40


@dataclass
class Summary:
    since: datetime | None
    expenses: list[Expense]
    names: dict[int, str]
    people: list[int]  # участники расчёта + все, кто платил или делил что-то
    paid: dict[int, int]
    owed: dict[int, int]
    balances: dict[int, int]
    transfers: list[Transfer[int]]

    @property
    def total(self) -> int:
        return sum(self.paid.values())


def expense_entries(expenses: list[Expense]) -> list[Entry[int]]:
    return [
        Entry(e.payer_id, item.amount, item.participant_ids, salt=n) for e in expenses for n, item in enumerate(e.items)
    ]


async def build_summary(session: AsyncSession, chat_id: int, since: datetime | None) -> Summary:
    expenses = await repo.expenses_since(session, chat_id, since)
    paid, owed = totals(expense_entries(expenses))
    people = list(dict.fromkeys([*await repo.active_member_ids(session, chat_id), *paid, *owed]))
    balances = {p: paid.get(p, 0) - owed.get(p, 0) for p in people}
    names = await repo.display_names(session, chat_id, set(people) | {e.payer_id for e in expenses})
    return Summary(since, expenses, names, people, paid, owed, balances, settle(balances))


def fmt_date(dt: datetime, tz: ZoneInfo, with_year: bool = False) -> str:
    local = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    return local.strftime("%d.%m.%Y" if with_year else "%d.%m")


def period_title(since: datetime | None, tz: ZoneInfo) -> str:
    return f"с обнуления {fmt_date(since, tz, with_year=True)}" if since else "с начала учёта"


# --- черновик ---

DRAFT_HINT = (
    "\n\n<i>Чтобы поправить — ответьте на это сообщение: «убери хлеб», «молоко было 95», "
    "«пиво только мне и Саше», «весь чек на меня и Лёшу».</i>"
)


def render_draft(view: DraftView, header: str = "🧾 <b>Черновик</b>") -> str:
    draft = Draft.model_validate(view["draft"])
    names = view["names"]
    participants = view["participants"]

    def who(ids: Iterable[int]) -> str:
        return ", ".join(escape(names.get(str(i), f"id{i}")) for i in ids)

    common = sorted(draft.shared_with) if draft.shared_with else view["everyone"]
    lines = [header + (f": {escape(draft.store)}" if draft.store else ""), f"Платил: {who([draft.payer_id])}"]
    if draft.purchase_date:
        lines.append(f"Дата покупки: {draft.purchase_date:%d.%m.%Y}")
    lines.append("")
    if not draft.items:
        lines.append("Позиций нет.")
    for n, (item, people) in enumerate(zip(draft.items, participants, strict=True), 1):
        qty = f" ×{item.quantity:g}" if item.quantity != 1 else ""
        only = f" → <i>{who(people)}</i>" if people != common else ""
        lines.append(f"{n}. {escape(item.name)}{qty} — {fmt_rub(item.amount)}{only}")
    if draft.items:
        lines.append(f"\n<b>Итого: {fmt_rub(draft.total)}</b>")
        lines.append(("Весь чек на: " if draft.shared_with else "Делится на всех: ") + who(common))
        items = zip(draft.items, participants, strict=True)
        _, owed = totals(Entry(draft.payer_id, i.amount, tuple(p), salt=n) for n, (i, p) in enumerate(items))
        lines.append("Доли: " + " · ".join(f"{who([uid])} {fmt_rub(v)}" for uid, v in owed.items()))
    if draft.note:
        lines.append(f"ℹ️ <i>{escape(draft.note)}</i>")
    return "\n".join(lines)


EXCLUSION_HINT = "\n\n<i>Чтобы поправить — ответьте на это сообщение: «не Арслан, а Саша», «с 13-го», «по пятницу».</i>"


def _day(iso: str) -> str:
    return f"{date.fromisoformat(iso):%d.%m}"


def render_exclusion(view: ExclusionView, header: str = "🧹 <b>Убрать из чеков</b>") -> str:
    who = escape(view["person"])
    lines = [f"{header}: {who} с {_day(view['date_from'])} по {_day(view['date_to'])}", ""]
    if view["receipts"]:
        lines.append(
            f"Затронет чеков: {view['receipts']} (позиций: {view['items']}). "
            f"Доля {who} в них — <b>{fmt_rub(view['amount'])}</b>, "
            "она поровну перейдёт к остальным участникам каждой позиции."
        )
    else:
        lines.append(f"За эти дни нет общих чеков, где участвует {who}.")
    if view["solo"]:
        lines.append(f"Позиции, купленные только на {who}, не трогаю ({view['solo']}).")
    if view["settled"]:
        lines.append(f"Чеки до последнего обнуления не трогаю — долги по ним уже закрыты ({view['settled']}).")
    lines.append("<i>Дата чека — день покупки, а если её нет — день, когда чек внесли.</i>")
    if view["note"]:
        lines.append(f"ℹ️ <i>{escape(view['note'])}</i>")
    return "\n".join(lines)


def render_pending(view: PendingView, updated: bool = False) -> tuple[str, str]:
    """Текст сообщения с подтверждением и надпись на кнопке «да»."""
    if view["kind"] == "exclusion":
        header = "🧹 <b>Убрать из чеков (обновлено)</b>" if updated else "🧹 <b>Убрать из чеков</b>"
        warn = "⚠️ Не получилось применить уточнение, запрос прежний.\n\n" if view["error"] == "llm" else ""
        return warn + render_exclusion(view, header) + EXCLUSION_HINT, "✅ Применить"
    header = "🧾 <b>Черновик (обновлён)</b>" if updated else "🧾 <b>Черновик</b>"
    if view["error"] == "llm":
        return "⚠️ Не получилось применить уточнение, черновик прежний.\n\n" + render_draft(
            view
        ) + DRAFT_HINT, "✅ Внести"
    return render_draft(view, header) + DRAFT_HINT, "✅ Внести"


# --- /spent и /debts ---


def render_expense_line(e: Expense, names: dict[int, str], tz: ZoneInfo) -> str:
    title = f" — {escape(e.title)}" if e.title else ""
    payer = escape(names.get(e.payer_id, "?"))
    return f"#{e.id} {repo.expense_day(e, tz):%d.%m} {payer}{title}: {fmt_rub(e.total)}"


def render_spent(s: Summary, tz: ZoneInfo, title: str) -> str:
    lines = [f"🧾 <b>Траты {title}</b>", ""]
    if not s.expenses:
        return "\n".join([*lines, "Трат пока нет."])
    shown = s.expenses[-MAX_LIST_LINES:]
    if len(s.expenses) > len(shown):
        lines.append(f"<i>…ещё {len(s.expenses) - len(shown)} более ранних</i>")
    lines += [render_expense_line(e, s.names, tz) for e in shown]
    lines += ["", f"<b>Всего: {fmt_rub(s.total)}</b>", ""]
    for uid in sorted(s.people, key=lambda p: -s.paid.get(p, 0)):
        lines.append(
            f"• {escape(s.names[uid])}: заплатил {fmt_rub(s.paid.get(uid, 0))}, его доля {fmt_rub(s.owed.get(uid, 0))}"
        )
    return "\n".join(lines)


CAPTION_LIMIT = 1024  # подпись к фото в Telegram


def chart_rows(s: Summary) -> list[ChartRow]:
    return [
        ChartRow(name=s.names[uid], paid=s.paid.get(uid, 0), owed=s.owed.get(uid, 0), balance=s.balances[uid])
        for uid in s.people
    ]


def chart_subtitle(s: Summary) -> str:
    return f"Всего {fmt_rub(s.total)} · трат: {len(s.expenses)} · людей: {len(s.people)}"


def render_chart_caption(s: Summary) -> str:
    """Переводы под картинкой: на графике видно, кто сколько должен, а здесь — кому именно."""
    if not s.transfers:
        return "Все в расчёте 🙌"
    lines = ["<b>Кто кому переводит:</b>"]
    for t in s.transfers:
        line = f"{escape(s.names[t.debtor])} → {escape(s.names[t.creditor])}: <b>{fmt_rub(t.amount)}</b>"
        if len("\n".join([*lines, line])) > CAPTION_LIMIT - 40:
            lines.append("…полный список — /debts")
            break
        lines.append(line)
    return "\n".join(lines)


def render_debts(s: Summary, tz: ZoneInfo, title: str) -> str:
    lines = [f"💸 <b>Расчёт {title}</b>", ""]
    if not s.people:
        return "\n".join([*lines, "Нет участников. Каждому нужно написать /join или представить соседей боту."])
    lines.append(f"Потрачено всего {fmt_rub(s.total)}")
    lines.append("")
    for uid in sorted(s.people, key=lambda p: -s.balances[p]):
        lines.append(
            f"• {escape(s.names[uid])}: заплатил {fmt_rub(s.paid.get(uid, 0))}, "
            f"доля {fmt_rub(s.owed.get(uid, 0))}, баланс {fmt_rub(s.balances[uid], signed=True)}"
        )
    lines.append("")
    if s.transfers:
        lines.append("<b>Кто кому переводит:</b>")
        for t in s.transfers:
            lines.append(f"{escape(s.names[t.debtor])} → {escape(s.names[t.creditor])}: <b>{fmt_rub(t.amount)}</b>")
    else:
        lines.append("Все в расчёте 🙌")
    return "\n".join(lines)
