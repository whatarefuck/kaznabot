from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from kaznabot import repo
from kaznabot.db import Member, User

router = Router(name="common")

HELP = """\
👋 Я веду общие траты квартиры и считаю, кто кому сколько должен.

<b>Знакомство</b>
Напишите в чат: «@nick — Саша, а @nick2 — Лёша» или «меня зовут Бай». Так я запомню имена и добавлю людей в расчёт.

<b>Как добавить трату</b>
• Пришлите фото или PDF чека. В подписи можно дать указание: «всё, кроме творога», \
«пиво только мне и Саше», «весь чек на меня и Лёшу».
• Или текстом: <code>/add хлеб 80, молоко 2 по 60 — молоко только Саше</code> \
(в группе ещё можно через @упоминание бота).
• Ответьте <code>/add</code> на чужое фото чека, и трата запишется на того, кто его прислал.
По умолчанию покупка делится на всех участников. Перед записью я покажу черновик с долями. \
Чтобы его поправить, ответьте на него сообщением.

<b>Команды</b>
/people — кто участвует и как кого зовут
/join — участвовать в расчёте
/leave — выйти из расчёта (уже внесённые траты остаются)
/spent — траты с последнего обнуления (<code>/spent month</code> — с 1-го числа, <code>/spent all</code> — за всё время)
/debts — кто кому сколько должен
/chart — то же картинкой: кто сколько заплатил и кто кому должен (можно <code>/chart month</code>)
/exclude — убрать человека из общих чеков за период: <code>/exclude Арслан с 12 по 18 октября</code>. \
Его доля перейдёт остальным, отменить можно кнопкой
/delete — удалить чек (или ответьте /delete на сообщение «✅ Внесено»)
/reset — обнулить долги (когда все рассчитались)
"""


PRIVATE_NOTE = (
    "ℹ️ Я работаю в групповых чатах: добавьте меня в чат квартиры "
    "(или в несколько — у каждого чата свой отдельный учёт и свои участники). "
    "Траты, внесённые здесь, в личке, в общий расчёт не попадают.\n\n"
)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer((PRIVATE_NOTE if message.chat.type == ChatType.PRIVATE else "") + HELP)


@router.message(Command("join"), F.from_user)
async def cmd_join(message: Message, session: AsyncSession, db_user: User) -> None:
    added = await repo.ensure_member(session, message.chat.id, db_user, activate=True)
    await session.commit()
    await message.reply(f"✅ {escape(db_user.name)} участвует в расчёте." if added else "Вы уже участвуете.")


@router.message(Command("leave"), F.from_user)
async def cmd_leave(message: Message, session: AsyncSession, db_user: User) -> None:
    removed = await repo.deactivate_member(session, message.chat.id, db_user)
    await session.commit()
    await message.reply(
        "👋 Вы больше не участвуете в общих тратах. Уже внесённые траты и долги остаются."
        if removed
        else "Вы и так не участвуете."
    )


@router.message(Command("people"))
async def cmd_people(message: Message, session: AsyncSession) -> None:
    members = await repo.members(session, message.chat.id)
    active = [m for m in members if m.active]
    others = [m for m in members if not m.active]

    def line(m: Member) -> str:
        extra = f"@{m.user.username}" if m.user.username else m.user.name
        pending = " <i>(ещё не писал в чат)</i>" if m.user.tg_id is None else ""
        return f"• {escape(m.name)} — {escape(extra)}{pending}"

    lines = ["👥 <b>Участвуют в расчёте</b>", *(line(m) for m in active)] if active else ["Пока никто не участвует."]
    if others:
        lines += ["", "<b>Видел в чате, но не в расчёте</b>", *(line(m) for m in others)]
    lines.append("\n<i>Поменять имя: «@nick — Саша». Вступить: /join.</i>")
    await message.answer("\n".join(lines))
