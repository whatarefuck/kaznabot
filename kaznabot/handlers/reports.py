"""Просмотр трат и долгов, график, удаление чеков, обнуление."""

import asyncio
from datetime import UTC, datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from kaznabot import repo
from kaznabot.charts import render_chart
from kaznabot.config import Settings
from kaznabot.db import Expense
from kaznabot.handlers.tg import callback_message, sender
from kaznabot.keyboards import ExpenseCb, ResetCb, delete_list_kb, reset_kb, restore_kb
from kaznabot.money import fmt_rub
from kaznabot.reports import (
    build_summary,
    chart_rows,
    chart_subtitle,
    fmt_date,
    period_title,
    render_chart_caption,
    render_debts,
    render_spent,
)

router = Router(name="reports")

DELETE_LIST_SIZE = 10
STALE_BUTTON = "Сообщение слишком старое. Вызовите команду заново."


async def resolve_period(
    arg: str | None, session: AsyncSession, chat_id: int, settings: Settings
) -> tuple[datetime | None, str]:
    """'' → с последнего обнуления, 'month' → с 1-го числа, 'all' → за всё время."""
    arg = (arg or "").strip().lower()
    if arg in ("month", "месяц"):
        start = datetime.now(settings.tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(UTC).replace(tzinfo=None), f"с 1-го числа ({start:%d.%m.%Y})"
    if arg in ("all", "всё", "все"):
        return None, "за всё время"
    since = await repo.last_settlement_at(session, chat_id)
    return since, period_title(since, settings.tz)


async def report_text(kind: str, arg: str | None, session: AsyncSession, chat_id: int, settings: Settings) -> str:
    since, title = await resolve_period(arg, session, chat_id, settings)
    summary = await build_summary(session, chat_id, since)
    render = render_debts if kind == "debts" else render_spent
    return render(summary, settings.tz, title)


@router.message(Command("spent"))
async def cmd_spent(message: Message, command: CommandObject, session: AsyncSession, settings: Settings) -> None:
    await message.answer(await report_text("spent", command.args, session, message.chat.id, settings))


@router.message(Command("debts"))
async def cmd_debts(message: Message, command: CommandObject, session: AsyncSession, settings: Settings) -> None:
    await message.answer(await report_text("debts", command.args, session, message.chat.id, settings))


@router.message(Command("chart"))
async def cmd_chart(
    message: Message, command: CommandObject, bot: Bot, session: AsyncSession, settings: Settings
) -> None:
    since, title = await resolve_period(command.args, session, message.chat.id, settings)
    summary = await build_summary(session, message.chat.id, since)
    if not summary.expenses:
        await message.answer(f"📊 Трат {title} пока нет — рисовать нечего.")
        return
    await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
    # Рисуем в отдельном потоке: matplotlib синхронный, а бот в это время должен отвечать другим
    png = await asyncio.to_thread(render_chart, chart_rows(summary), f"Траты {title}", chart_subtitle(summary))
    await message.answer_photo(BufferedInputFile(png, "kazna.png"), caption=render_chart_caption(summary))


# --- обнуление ---


@router.message(Command("reset"), F.from_user)
async def cmd_reset(message: Message, session: AsyncSession, settings: Settings) -> None:
    text = await report_text("debts", None, session, message.chat.id, settings)
    await message.reply(
        text + "\n\n<b>Обнулить долги?</b> Делайте это, когда все уже перевели деньги.",
        reply_markup=reset_kb(sender(message).id),
    )


@router.callback_query(ResetCb.filter())
async def on_reset(cb: CallbackQuery, callback_data: ResetCb, session: AsyncSession, settings: Settings) -> None:
    if (msg := callback_message(cb)) is None:
        await cb.answer(STALE_BUTTON, show_alert=True)
        return
    if cb.from_user.id != callback_data.by:
        await cb.answer("Подтвердить может только тот, кто запросил обнуление.", show_alert=True)
        return
    if callback_data.action == "cancel":
        await msg.edit_text("Обнуление отменено.")
        await cb.answer()
        return

    chat_id = msg.chat.id
    text = await report_text("debts", None, session, chat_id, settings)
    user = await repo.upsert_user(session, cb.from_user)
    settlement = await repo.add_settlement(session, chat_id, user)
    await session.commit()
    await msg.edit_text(
        f"🧹 <b>Долги обнулены {fmt_date(settlement.created_at, settings.tz, with_year=True)}</b>. "
        f"Новые траты считаются с этого момента.\n\nИтоги закрытого периода:\n\n{text}"
    )
    await cb.answer("Готово")


# --- удаление ---


async def expense_label(session: AsyncSession, e: Expense) -> str:
    payer = (await repo.display_names(session, e.chat_id, {e.payer_id}))[e.payer_id]
    title = f" «{escape(e.title)}»" if e.title else ""
    return f"#{e.id}{title} на {fmt_rub(e.total)} ({escape(payer)})"


@router.message(Command("delete"))
async def cmd_delete(
    message: Message, command: CommandObject, bot: Bot, session: AsyncSession, settings: Settings
) -> None:
    chat_id = message.chat.id
    expense = None
    reply = message.reply_to_message
    arg = (command.args or "").strip().lstrip("#")
    if reply and reply.from_user and reply.from_user.id == bot.id:
        expense = await repo.get_expense_by_bot_msg(session, chat_id, reply.message_id)
    elif arg.isdigit():
        expense = await repo.get_expense(session, chat_id, int(arg))

    if expense is not None:
        label = await expense_label(session, expense)
        if expense.deleted_at:
            await message.reply(f"Чек {label} уже удалён.", reply_markup=restore_kb(expense.id))
            return
        repo.set_deleted(expense, True)
        await session.commit()
        await message.reply(f"🗑 Удалён чек {label}.", reply_markup=restore_kb(expense.id))
        return
    if reply or command.args:
        await message.reply("Не нашёл такой чек.")
        return

    since, _ = await resolve_period(None, session, chat_id, settings)
    recent = (await repo.expenses_since(session, chat_id, since))[-DELETE_LIST_SIZE:][::-1]
    if not recent:
        await message.reply("В текущем периоде нет чеков.")
        return
    await message.reply("Какой чек удалить?", reply_markup=delete_list_kb(recent))


@router.callback_query(ExpenseCb.filter())
async def on_expense_button(cb: CallbackQuery, callback_data: ExpenseCb, session: AsyncSession) -> None:
    if (msg := callback_message(cb)) is None:
        await cb.answer(STALE_BUTTON, show_alert=True)
        return
    expense = await repo.get_expense(session, msg.chat.id, callback_data.expense_id)
    if expense is None:
        await cb.answer("Чек не найден.")
        return
    deleting = callback_data.action == "del"
    repo.set_deleted(expense, deleting)
    await session.commit()
    label = await expense_label(session, expense)
    if deleting:
        await msg.edit_text(f"🗑 Удалён чек {label}.", reply_markup=restore_kb(expense.id))
    else:
        await msg.edit_text(f"↩️ Чек {label} восстановлен.")
    await cb.answer()
