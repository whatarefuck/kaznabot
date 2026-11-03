"""Сборка сообщений для GigaChat. Чистые функции — легко проверить глазами и тестами."""

from datetime import date, timedelta

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from kaznabot.ai.schemas import Draft, ExclusionPlan, Mention, RosterEntry

INTENT_SYSTEM = """\
Ты — бот в чате соседей по квартире, который ведёт учёт общих покупок.
Определи, что хочет автор сообщения, и вызови функцию Intent.

intent:
- introduce — автор знакомит тебя с людьми: говорит, как кого зовут
  («@nick — Саша», «@a это Петя, а @b — Коля», «меня зовут Лёша», «Alexander K — это Саня»).
  Заполни people: ref — упоминание из сообщения ровно как в списке (@username),
  или id:N из списка людей, или self, если автор говорит о себе; name — имя в именительном падеже
  с заглавной буквы («саша» → «Саша», «Лёшей» → «Лёша»).
- expense — автор сообщает о покупке или трате: есть товары, услуги или суммы.
- exclude — просит убрать человека из чеков за какие-то дни, потому что его не было или он не пользовался
  («Арслан уезжал с 12 по 18, убери его из чеков», «не считай Сашу на этой неделе»).
- debts — спрашивает, кто кому сколько должен.
- spent — спрашивает, сколько потратили.
- other — всё остальное.
"""

RECEIPT_SYSTEM = """\
Ты помогаешь соседям по квартире вести учёт общих покупок.
Тебе дают текст кассового чека или описание покупок, иногда с указаниями пользователя.
Вызови функцию Receipt со списком позиций, которые нужно внести.

Суммы:
- amount — ИТОГОВАЯ стоимость позиции в рублях (цена × количество, с учётом скидок), не цена за штуку.
- name — короткое понятное название на русском, без артикулов и кодов.
- Не выдумывай позиции и суммы. Не включай итоги, НДС, сдачу, бонусы, способы оплаты.
- «хлеб 80, молоко 2 по 60» — хлеб за 80 ₽ и молоко 2 шт на 120 ₽.
- Если пользователь просит что-то не вносить («всё, кроме творога», «пакет не считай», «только молоко») —
  не включай эти позиции и напиши об этом в note.

На кого делится (используй только id из списка людей):
- По умолчанию покупка общая: shared_with = null и for_whom = null — делится на всех участников общего расчёта.
- Если сказано, что ВЕСЬ чек на определённых людей («на меня и Лёшу», «это мы с Сашей скидывались») —
  перечисли их id в shared_with.
- Если про отдельную позицию сказано, для кого она («молоко только Саше», «творог мой», «пиво мне и Пете») —
  перечисли id в for_whom этой позиции. «Мой», «мне», «для меня» — это автор указания.
- «Всем, кроме Саши» — перечисли всех участников общего расчёта, кроме Саши.
- payer_id — только если сказано, что платил другой человек («Петя заплатил»); иначе null.
- Если человека нет в списке — не подставляй чужой id: оставь поле null и напиши в note, кого не нашёл.
- Если это не покупки — is_purchase = false и пустой items.

Дата:
- purchase_date — день покупки, если он есть в чеке или назван в тексте («вчера», «в субботу», «12 октября»).
  Считай от сегодняшней даты из сообщения. Если дата не указана — null.
"""

EXCLUDE_SYSTEM = """\
Ты помогаешь соседям по квартире вести учёт общих покупок.
Пользователь просит убрать человека из общих чеков за период — например, тот уезжал и ничем не пользовался.
Вызови функцию ExcludeRequest.

- person_name — как человек назван в просьбе, слово в слово («Арслана», «Сашу», «меня»).
- person_id — id этого человека из списка людей. Если его нет в списке — null; не подставляй другого человека.
- date_from и date_to — первый и последний день периода включительно, ГГГГ-ММ-ДД.
  Считай от сегодняшней даты; календарь последних недель — в сообщении.
  «с 12 по 18» без месяца — текущий месяц; «на этой неделе» — с понедельника этой недели по сегодня;
  «с понедельника» — с ближайшего прошедшего понедельника по сегодня; если конец не назван — date_to = null.
- Если что-то непонятно — напиши в note.
"""

EXCLUDE_REFINE_TEMPLATE = """\
Сейчас запрос понят так: убрать id {person_id} с {date_from} по {date_to}.
Уточнение от пользователя: «{instruction}»
Примени уточнение и верни запрос ЦЕЛИКОМ через функцию ExcludeRequest."""

REFINE_TEMPLATE = """\
Текущий черновик (JSON):
{draft}

Уточнение от автора: «{instruction}»
Примени уточнение и верни исправленный черновик ЦЕЛИКОМ через функцию Receipt."""

TRANSCRIBE_PROMPT = """\
Перепиши содержимое кассового чека на изображении.
Первая строка — название магазина. Вторая — «Дата: ДД.ММ.ГГГГ», если дата есть на чеке.
Дальше каждая товарная позиция отдельной строкой в формате:
название | количество | итоговая сумма в рублях
Не переписывай итоги, НДС, сдачу, бонусы и способы оплаты.
Если на изображении нет чека или списка покупок — ответь одним словом: НЕТ"""


WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def calendar_block(today: date, days: int = 21) -> str:
    """Сегодняшняя дата и календарь последних недель: модели плохо считают даты сами."""
    monday = today - timedelta(days=today.weekday())
    weeks = {0: "эта неделя", 1: "прошлая неделя", 2: "позапрошлая неделя"}
    lines = [
        f"Сегодня: {today.isoformat()}, {WEEKDAYS[today.weekday()]}.",
        f"Эта неделя: с {monday.isoformat()} по {today.isoformat()}.",
        f"Прошлая неделя: с {(monday - timedelta(days=7)).isoformat()} по {(monday - timedelta(days=1)).isoformat()}.",
        "Последние дни:",
    ]
    for back in range(days - 1, -1, -1):
        d = today - timedelta(days=back)
        week = weeks.get((monday - (d - timedelta(days=d.weekday()))).days // 7, "")
        lines.append(f"- {d.isoformat()} {WEEKDAYS[d.weekday()]}" + (f" ({week})" if week else ""))
    return "\n".join(lines)


def roster_block(roster: list[RosterEntry], author_id: int, payer_id: int | None = None) -> str:
    lines = ["Люди в чате (id: имя):"]
    for p in roster:
        extra = [f"@{p['username']}"] if p.get("username") else []
        if p.get("tg_name") and p["tg_name"] != p["name"]:
            extra.append(f"в Telegram «{p['tg_name']}»")
        extra.append("участвует в общем расчёте" if p["active"] else "не участвует в общем расчёте")
        lines.append(f"- {p['id']}: {p['name']} ({', '.join(extra)})")
    if len(lines) == 1:
        lines.append("- пока никого")
    lines.append(f"\nАвтор сообщения: id {author_id} («я», «мне», «мой» — это он).")
    if payer_id is not None and payer_id != author_id:
        lines.append(f"Чек прислал: id {payer_id} (по умолчанию платил он).")
    return "\n".join(lines)


def intent_messages(text: str, mentions: list[Mention], roster: list[RosterEntry], author_id: int) -> list[BaseMessage]:
    parts = [roster_block(roster, author_id)]
    if mentions:
        refs = ", ".join(m["label"] if m["ref"] == m["label"] else f"{m['label']} ({m['ref']})" for m in mentions)
        parts.append(f"Упоминания в сообщении: {refs}")
    parts.append(f"Сообщение:\n{text}")
    return [SystemMessage(INTENT_SYSTEM), HumanMessage("\n\n".join(parts))]


def _source(receipt_text: str | None, comment: str | None) -> str:
    parts = []
    if receipt_text:
        parts.append(f"Чек / покупки:\n{receipt_text}")
    if comment:
        parts.append(f"Указание пользователя: {comment}")
    return "\n\n".join(parts)


def receipt_messages(
    receipt_text: str | None,
    comment: str | None,
    roster: list[RosterEntry],
    author_id: int,
    payer_id: int,
    today: date,
) -> list[BaseMessage]:
    parts = [
        roster_block(roster, author_id, payer_id),
        f"Сегодня: {today.isoformat()}.",
        _source(receipt_text, comment),
    ]
    return [SystemMessage(RECEIPT_SYSTEM), HumanMessage("\n\n".join(parts))]


def refine_messages(
    receipt_text: str | None,
    comment: str | None,
    roster: list[RosterEntry],
    author_id: int,
    draft: Draft,
    instruction: str,
    today: date,
) -> list[BaseMessage]:
    return [
        *receipt_messages(receipt_text, comment, roster, author_id, draft.payer_id, today),
        HumanMessage(
            REFINE_TEMPLATE.format(
                draft=draft.to_llm().model_dump_json(exclude={"is_purchase"}), instruction=instruction
            )
        ),
    ]


def transcribe_messages(file_id: str) -> list[BaseMessage]:
    # Картинка уже загружена в GigaChat; блок image с file_id langchain-gigachat превращает в attachments
    return [HumanMessage(content=[{"type": "text", "text": TRANSCRIBE_PROMPT}, {"type": "image", "file_id": file_id}])]


def exclude_messages(
    text: str,
    roster: list[RosterEntry],
    author_id: int,
    today: date,
    current: ExclusionPlan | None = None,
    instruction: str | None = None,
) -> list[BaseMessage]:
    body = "\n\n".join([roster_block(roster, author_id), calendar_block(today), f"Просьба:\n{text}"])
    msgs: list[BaseMessage] = [SystemMessage(EXCLUDE_SYSTEM), HumanMessage(body)]
    if current is not None and instruction:
        msgs.append(
            HumanMessage(
                EXCLUDE_REFINE_TEMPLATE.format(
                    person_id=current.person_id,
                    date_from=current.date_from.isoformat(),
                    date_to=current.date_to.isoformat(),
                    instruction=instruction,
                )
            )
        )
    return msgs
