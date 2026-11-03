"""Быт квартиры: дежурства по уборке и график ужинов. Чистые функции без Telegram и БД."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

WEEKDAYS_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip


def human_date(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


def week_range(start: date, end: date) -> str:
    """«9–15 ноября» или «28 сентября – 4 октября»."""
    if start.month == end.month:
        return f"{start.day}–{end.day} {MONTHS_GEN[end.month - 1]}"
    return f"{human_date(start)} – {human_date(end)}"


def next_sunday(today: date) -> date:
    """Ближайшее воскресенье; сегодня, если сегодня воскресенье."""
    return today + timedelta(days=(6 - today.weekday()) % 7)


def next_monday(today: date) -> date:
    """Понедельник следующей недели (в понедельник — через неделю)."""
    return today + timedelta(days=7 - today.weekday())


# --- дежурства ---


@dataclass(frozen=True)
class Rotation:
    """Зоны уборки переходят по кругу раз в неделю.

    В воскресенье anchor зона zones[i] у people[i]; каждую следующую неделю — у следующего по списку.
    Если зон больше, чем людей, кому-то достаются две; если меньше — кто-то в эту неделю свободен.
    """

    anchor: date  # воскресенье
    zones: tuple[str, ...]
    people: tuple[int, ...]  # users.id

    def week(self, sunday: date) -> list[tuple[str, int]]:
        """(зона, кто убирает) на это воскресенье — в порядке зон."""
        shift = (sunday - self.anchor).days // 7
        n = len(self.people)
        return [(zone, self.people[(i + shift) % n]) for i, zone in enumerate(self.zones)]

    def by_person(self, sunday: date) -> dict[int, list[str]]:
        result: dict[int, list[str]] = {p: [] for p in self.people}
        for zone, person in self.week(sunday):
            result[person].append(zone)
        return result


PAIR_SEP = re.compile(r"\s*[—–\-:=]\s*")
DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$")


@dataclass
class RotationDraft:
    anchor: date
    pairs: list[tuple[str, str]]  # (как назвали человека, зона)


def parse_rotation(text: str, today: date) -> RotationDraft | str:
    """«08.11 Данил — Кух, Искандар — В, ...» → дата и пары. Дата необязательна (ближайшее воскресенье)."""
    text = text.strip()
    anchor = next_sunday(today)
    first, _, rest = text.partition(" ")
    if m := DATE_RE.match(first):
        day, month, year = int(m[1]), int(m[2]), m[3]
        y = (int(year) + 2000 if len(year) == 2 else int(year)) if year else today.year
        try:
            anchor = date(y, month, day)
        except ValueError:
            return f"не понял дату «{first}»"
        if anchor.weekday() != 6:
            return f"{anchor:%d.%m.%Y} — не воскресенье"
        text = rest
    pairs = []
    for chunk in re.split(r"[,;\n]+", text):
        if not chunk.strip():
            continue
        parts = PAIR_SEP.split(chunk.strip(), maxsplit=1)
        if len(parts) != 2 or not all(parts):
            return f"не понял «{chunk.strip()}» — пишите «Имя — зона»"
        pairs.append((parts[0], parts[1]))
    if len(pairs) < 2:
        return "нужно хотя бы два человека"
    return RotationDraft(anchor, pairs)


# --- ужины ---


def assign_cooks(
    days: Sequence[date],
    available: dict[int, set[date]],
    order: Sequence[int],
    previous: dict[date, int] | None = None,
) -> dict[date, int | None]:
    """Кто в какой день готовит ужин.

    1. Каждый день, на который кто-то записался, закрыт.
    2. Нагрузка как можно ровнее (минимум суммы квадратов — сначала все по разу, потом вторые дни).
    3. При равенстве — как в прошлый раз (переголосование одного не перетасовывает остальных).
    4. Дальше — по порядку order: он меняется каждую неделю, чтобы «лишний» день не доставался одному и тому же.
    """
    previous = previous or {}
    rank = {p: i for i, p in enumerate(order)}
    candidates = {
        d: sorted((p for p, ds in available.items() if d in ds), key=lambda p: rank.get(p, len(rank))) for d in days
    }
    # Сначала дни, где выбор меньше: так раньше находим хорошее решение и сильнее отсекаем
    search = sorted(days, key=lambda d: (len(candidates[d]), days.index(d)))

    best: tuple[int, int, tuple[int, ...]] | None = None
    best_plan: dict[date, int | None] = {}
    plan: dict[date, int | None] = {}
    load: dict[int, int] = dict.fromkeys(available, 0)

    def score() -> tuple[int, int, tuple[int, ...]]:
        ssq = sum(v * v for v in load.values())
        kept = sum(1 for d, p in plan.items() if p is not None and previous.get(d) == p)
        key = tuple(rank.get(p, len(rank)) if p is not None else len(rank) + 1 for p in (plan[d] for d in days))
        return ssq, -kept, key

    def dfs(i: int, ssq: int) -> None:
        nonlocal best, best_plan
        if best is not None and ssq > best[0]:
            return
        if i == len(search):
            s = score()
            if best is None or s < best:
                best, best_plan = s, dict(plan)
            return
        d = search[i]
        if not candidates[d]:
            plan[d] = None
            dfs(i + 1, ssq)
            return
        for p in sorted(candidates[d], key=lambda p: (load[p], previous.get(d) != p, rank.get(p, len(rank)))):
            plan[d] = p
            load[p] += 1
            dfs(i + 1, ssq + 2 * load[p] - 1)
            load[p] -= 1

    dfs(0, 0)
    return {d: best_plan.get(d) for d in days}


def rotate(people: Iterable[int], by: int) -> list[int]:
    items = list(people)
    if not items:
        return items
    k = by % len(items)
    return items[k:] + items[:k]
