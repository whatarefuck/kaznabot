"""Жизнь бота в чатах: добавили в новый чат, группа стала супергруппой.

У каждого чата свой отдельный учёт: участники, траты, обнуления и черновики привязаны к id чата.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy.ext.asyncio import AsyncSession

from kaznabot import repo
from kaznabot.ai.assistant import Assistant

log = logging.getLogger(__name__)
router = Router(name="chats")

WELCOME = """\
👋 Привет! Я веду общие траты этого чата и считаю, кто кому сколько должен. \
Учёт здесь отдельный: траты и участники других чатов сюда не попадают.

<b>С чего начать</b>
1. Каждый, кто участвует в расчёте, пишет /join.
2. Траты: <code>/add хлеб 80, молоко 120</code> или фото чека, а потом ответ на него <code>/add</code>.
3. Кто кому должен — /debts, картинкой — /chart.

Все команды — /help."""

GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}


@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION), F.chat.type.in_(GROUPS))
async def on_bot_added(event: ChatMemberUpdated, bot: Bot, session: AsyncSession) -> None:
    # Бот здесь уже работал (его удаляли и вернули) — не здороваемся второй раз.
    # Смотрим на участников расчёта: просто «видел в чате» появляется от любого сообщения, в том числе о добавлении бота
    if await repo.active_member_ids(session, event.chat.id):
        return
    await bot.send_message(event.chat.id, WELCOME)


# Telegram присылает два служебных сообщения — в старый чат и в новый, в любом порядке; переносим по первому
@router.message(F.migrate_to_chat_id.as_("new_id"))
async def on_migrated_out(message: Message, new_id: int, bot: Bot, session: AsyncSession, assistant: Assistant) -> None:
    await move_chat(message.chat.id, new_id, bot, session, assistant)


@router.message(F.migrate_from_chat_id.as_("old_id"))
async def on_migrated_in(message: Message, old_id: int, bot: Bot, session: AsyncSession, assistant: Assistant) -> None:
    await move_chat(old_id, message.chat.id, bot, session, assistant)


async def move_chat(old: int, new: int, bot: Bot, session: AsyncSession, assistant: Assistant) -> None:
    moved = await repo.migrate_chat(session, old, new)
    await session.commit()
    for thread_id in moved.cancelled_threads:
        await assistant.checkpointer.adelete_thread(thread_id)
    log.info("chat %s → %s: трат %d, закрыто черновиков %d", old, new, moved.expenses, len(moved.cancelled_threads))
    if moved.expenses or moved.cancelled_threads:
        note = "Незакрытые черновики пришлось отменить — отправьте их заново.\n" if moved.cancelled_threads else ""
        await bot.send_message(
            new,
            f"🔁 Чат стал супергруппой, и Telegram сменил его номер. Перенёс все траты ({moved.expenses}) "
            f"и участников — долги не изменились.\n{note}Проверить: /debts",
        )
