"""Сообщения, которые разбирает граф: чеки, траты текстом, знакомства; кнопки и ответы на черновик."""

import logging
import re
from contextlib import suppress
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import CallbackQuery, Message
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from kaznabot import repo
from kaznabot.ai.assistant import Assistant, Done, Pending
from kaznabot.ai.graph import State
from kaznabot.ai.schemas import Decision, Mention, Result
from kaznabot.config import Settings
from kaznabot.db import Draft, User
from kaznabot.handlers.reports import report_text
from kaznabot.handlers.tg import bot_username, callback_message, sender
from kaznabot.keyboards import DraftCb, ExclusionCb, draft_kb, exclusion_undo_kb
from kaznabot.reports import render_draft, render_exclusion, render_pending
from kaznabot.sources import Attachment, find_attachment, load_attachment

log = logging.getLogger(__name__)
router = Router(name="chat")
router.message.filter(F.from_user.is_bot.is_(False))

NOT_A_RECEIPT = (
    "🤷 Не нашёл покупок. Попробуйте фото почётче или напишите текстом: <code>/add хлеб 80, молоко 120</code>"
)
LLM_FAILED = "⚠️ Не получилось разобрать: GigaChat не ответил даже после повторных попыток. Попробуйте через пару минут."
OTHER_HINT = (
    "Я понимаю покупки («хлеб 80, молоко 120 — молоко только Саше»), знакомства («@nick — Саша») "
    "и просьбы убрать человека из чеков за период («Арслана не было с 12 по 18»). "
    "Долги — /debts, траты — /spent, всё остальное — /help."
)
# Похоже на «@nick — Саша» / «@nick это Саша» / «его зовут…» — только такие сообщения без обращения к боту идут в LLM
INTRO_HINT = re.compile(r"[-—–=:]|\bэто\b|\bзовут\b", re.IGNORECASE)


EXCLUDE_EXAMPLE = "Например: <code>/exclude Арслан с 12 по 18 октября</code>"


def today(settings: Settings) -> str:
    return datetime.now(settings.tz).date().isoformat()


# --- кому адресовано сообщение ---


async def is_addressed(message: Message, bot: Bot) -> bool:
    if message.chat.type == ChatType.PRIVATE:
        return True
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == bot.id:
        return True
    return f"@{await bot_username(bot)}".lower() in (message.text or message.caption or "").lower()


async def strip_bot_mention(text: str | None, bot: Bot) -> str:
    if not text:
        return ""
    username = re.escape(await bot_username(bot))
    return re.sub(rf"@{username}\b", "", text, flags=re.IGNORECASE).strip(" ,:\n")


async def collect_mentions(message: Message, bot: Bot, session: AsyncSession) -> list[Mention]:
    """Упоминания людей в сообщении. @username отдаём как есть (найдём или заведём заглушку в графе),
    а у text_mention (человек без @username) Telegram присылает сам аккаунт — сразу сохраняем его."""
    me = (await bot_username(bot)).lower()
    text = message.text or message.caption or ""
    result: list[Mention] = []
    seen: set[str] = set()
    for entity in message.entities or message.caption_entities or []:
        label = entity.extract_from(text)
        if entity.type == "mention" and label[1:].lower() != me:
            item = Mention(ref=label, label=label)
        elif entity.type == "text_mention" and entity.user and not entity.user.is_bot:
            user = await repo.upsert_user(session, entity.user)
            await repo.ensure_member(session, message.chat.id, user)
            item = Mention(ref=f"id:{user.id}", label=label, user_id=user.id)
        else:
            continue
        if item["ref"].lower() not in seen:
            seen.add(item["ref"].lower())
            result.append(item)
    await session.commit()
    return result


# --- запуск графа и показ результата ---


async def run_graph(
    message: Message,
    state: State,
    *,
    explicit: bool,
    placeholder: Message | None,
    assistant: Assistant,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """explicit=False — сообщение не адресовано боту: молчим, если граф ничего не нашёл."""
    thread_id = f"{message.chat.id}:{message.message_id}"
    try:
        outcome = await assistant.start(thread_id, state, session)
    except Exception:
        log.exception("graph failed")
        if placeholder:
            await placeholder.edit_text(LLM_FAILED)
        return

    if isinstance(outcome, Pending):
        text, ok_text = render_pending(outcome.view)
        if placeholder:
            await placeholder.edit_text(text)
            msg = placeholder
        else:
            msg = await message.reply(text)
        draft = await repo.add_draft(
            session,
            thread_id=thread_id,
            chat_id=message.chat.id,
            bot_msg_id=msg.message_id,
            author_tg_id=sender(message).id,
        )
        await session.commit()
        # Кнопки — после сохранения: в callback_data нужен id черновика
        await msg.edit_reply_markup(reply_markup=draft_kb(draft.id, ok_text))
        return

    reply = await result_text(outcome, explicit, session, message.chat.id, settings)
    if reply is None:
        if placeholder:
            await placeholder.delete()
    elif placeholder:
        await placeholder.edit_text(reply)
    else:
        await message.reply(reply)


async def result_text(
    outcome: Done, explicit: bool, session: AsyncSession, chat_id: int, settings: Settings
) -> str | None:
    r = outcome.result
    match r["kind"]:
        case "introduced":
            return render_intro(r, explicit)
        case "debts" | "spent":
            return await report_text(r["kind"], None, session, chat_id, settings)
        case "not_purchase":
            return NOT_A_RECEIPT if explicit else None
        case "unclear":
            return f"🤔 {escape(r.get('reason', 'не понял запрос'))}.\n{EXCLUDE_EXAMPLE}" if explicit else None
        case "error":
            return LLM_FAILED if explicit else None
        case "other":
            return OTHER_HINT if explicit else None
    return None


def render_intro(r: Result, explicit: bool) -> str | None:
    lines = []
    if people := r.get("people"):
        named = ", ".join(f"{escape(p['name'])} — {escape(p['label'])}" for p in people)
        lines.append(f"👋 Запомнил: {named}. Теперь они участвуют в общем расчёте.")
    if skipped := r.get("skipped"):
        lines.append("Не понял, кто это: " + ", ".join(escape(s) for s in skipped))
    if not lines:
        return "Не понял, кого как зовут. Напишите, например: «@nick — Саша»." if explicit else None
    return "\n".join(lines)


# --- ответ на черновик = уточнение ---


class ReplyToDraft(Filter):
    """Ответ на черновик бота, который ещё не закрыт. Передаёт ``draft`` в хендлер."""

    async def __call__(self, message: Message, bot: Bot, session: AsyncSession) -> bool | dict[str, Draft]:
        reply = message.reply_to_message
        if reply is None or reply.from_user is None or reply.from_user.id != bot.id:
            return False
        draft = await repo.draft_by_msg(session, message.chat.id, reply.message_id)
        if draft is None or draft.status not in ("pending", "busy"):
            return False
        return {"draft": draft}


@router.message(F.text, ReplyToDraft())
async def refine_draft(
    message: Message, draft: Draft, db_user: User, bot: Bot, session: AsyncSession, assistant: Assistant
) -> None:
    if sender(message).id != draft.author_tg_id:
        await message.reply("Уточнять черновик может только тот, кто его создал.")
        return
    if not await repo.switch_draft(session, draft.id, "pending", "busy"):
        await message.reply("Секунду, я ещё обрабатываю этот черновик.")
        return
    await session.commit()
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    decision = Decision(action="refine", text=message.text or "", by=db_user.id)
    try:
        outcome = await assistant.resume(draft.thread_id, decision, session)
    except Exception:
        outcome = None
        log.exception("refine failed")
    if not isinstance(outcome, Pending):
        await repo.switch_draft(session, draft.id, "busy", "pending")
        await session.commit()
        await message.reply(LLM_FAILED)
        return

    text, ok_text = render_pending(outcome.view, updated=True)
    # Новый черновик — свежим сообщением (старое могло уехать вверх), старое помечаем устаревшим
    new_msg = await message.reply(text, reply_markup=draft_kb(draft.id, ok_text))
    old_msg_id = draft.bot_msg_id
    await session.execute(
        update(Draft).where(Draft.id == draft.id).values(status="pending", bot_msg_id=new_msg.message_id)
    )
    await session.commit()
    with suppress(TelegramBadRequest):  # старое сообщение могли удалить
        await bot.edit_message_text("✏️ Черновик обновлён, см. ниже.", chat_id=message.chat.id, message_id=old_msg_id)


# --- новые сообщения ---


@router.message(Command("exclude"))
async def cmd_exclude(
    message: Message,
    command: CommandObject,
    db_user: User,
    bot: Bot,
    session: AsyncSession,
    assistant: Assistant,
    settings: Settings,
) -> None:
    text = await strip_bot_mention(command.args, bot)
    if not text:
        await message.reply(
            "Напишите, кого и за какие дни убрать из общих чеков — его доля перейдёт остальным.\n" + EXCLUDE_EXAMPLE
        )
        return
    placeholder = await message.reply("⏳ Считаю…")
    state = State(
        today=today(settings),
        chat_id=message.chat.id,
        author_id=db_user.id,
        payer_id=db_user.id,
        mode="exclude",
        addressed=True,
        text=text,
    )
    await run_graph(
        message,
        state,
        explicit=True,
        placeholder=placeholder,
        assistant=assistant,
        session=session,
        settings=settings,
    )


@router.message(Command("add"))
async def cmd_add(
    message: Message,
    command: CommandObject,
    db_user: User,
    bot: Bot,
    session: AsyncSession,
    assistant: Assistant,
    settings: Settings,
) -> None:
    text = await strip_bot_mention(command.args, bot)
    receipt_msg = message
    att = find_attachment(message)
    if att is None and message.reply_to_message:
        receipt_msg = message.reply_to_message
        att = find_attachment(receipt_msg)

    if att:
        await handle_receipt(message, receipt_msg, att, text or None, True, db_user, bot, session, assistant, settings)
    elif text:
        placeholder = await message.reply("⏳ Записываю…")
        state = State(
            today=today(settings),
            chat_id=message.chat.id,
            author_id=db_user.id,
            payer_id=db_user.id,
            mode="expense",
            addressed=True,
            text=text,
        )
        await run_graph(
            message,
            state,
            explicit=True,
            placeholder=placeholder,
            assistant=assistant,
            session=session,
            settings=settings,
        )
    else:
        await message.reply(
            "Напишите покупки после команды: <code>/add хлеб 80, молоко 120 — молоко только Саше</code>\n"
            "или пришлите фото чека, или ответьте /add на сообщение с чеком."
        )


@router.message(F.photo | F.document)
async def on_attachment(
    message: Message, db_user: User, bot: Bot, session: AsyncSession, assistant: Assistant, settings: Settings
) -> None:
    att = find_attachment(message)
    if att is None:
        return
    explicit = await is_addressed(message, bot)
    if not explicit and not settings.auto_parse_photos:
        return
    comment = await strip_bot_mention(message.caption, bot)
    await handle_receipt(message, message, att, comment or None, explicit, db_user, bot, session, assistant, settings)


@router.message(F.text)
async def on_text(
    message: Message, db_user: User, bot: Bot, session: AsyncSession, assistant: Assistant, settings: Settings
) -> None:
    explicit = await is_addressed(message, bot)
    mentions = await collect_mentions(message, bot, session)
    # Без обращения к боту реагируем только на то, что похоже на знакомство
    if not explicit and not (mentions and INTRO_HINT.search(message.text or "")):
        return
    text = await strip_bot_mention(message.text, bot)
    if not text:
        return
    placeholder = await message.reply("⏳ Думаю…") if explicit else None
    state = State(
        today=today(settings),
        chat_id=message.chat.id,
        author_id=db_user.id,
        payer_id=db_user.id,
        mode="auto",
        addressed=explicit,
        text=text,
        mentions=mentions,
    )
    await run_graph(
        message,
        state,
        explicit=explicit,
        placeholder=placeholder,
        assistant=assistant,
        session=session,
        settings=settings,
    )


async def handle_receipt(
    message: Message,
    receipt_msg: Message,
    att: Attachment,
    comment: str | None,
    explicit: bool,
    author: User,
    bot: Bot,
    session: AsyncSession,
    assistant: Assistant,
    settings: Settings,
) -> None:
    # Платит тот, чей чек: при «/add» в ответ на чужое фото — автор фото
    payer = author
    if receipt_msg is not message and receipt_msg.from_user and not receipt_msg.from_user.is_bot:
        payer = await repo.upsert_user(session, receipt_msg.from_user)
        await repo.ensure_member(session, message.chat.id, payer)
        await session.commit()

    placeholder = await message.reply("⏳ Разбираю чек…") if explicit else None
    if placeholder is None:
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        loaded = await load_attachment(bot, att)
        file_id = await assistant.ai.upload_image(*loaded.image) if loaded.image else None
    except Exception:
        log.exception("attachment failed")
        if placeholder:
            await placeholder.edit_text(LLM_FAILED)
        return

    state = State(
        today=today(settings),
        chat_id=message.chat.id,
        author_id=author.id,
        payer_id=payer.id,
        mode="expense",
        addressed=explicit,
        text=loaded.text,
        comment=comment,
        file_id=file_id,
    )
    await run_graph(
        message,
        state,
        explicit=explicit,
        placeholder=placeholder,
        assistant=assistant,
        session=session,
        settings=settings,
    )


# --- кнопки черновика ---


@router.callback_query(DraftCb.filter())
async def on_draft_button(
    cb: CallbackQuery, callback_data: DraftCb, bot: Bot, session: AsyncSession, assistant: Assistant
) -> None:
    draft = await session.get(Draft, callback_data.draft_id)
    msg = callback_message(cb)
    # Черновик принадлежит своему чату: кнопка из другого чата его не трогает
    if draft is None or draft.status not in ("pending", "busy") or (msg is not None and msg.chat.id != draft.chat_id):
        await cb.answer("Этот черновик уже закрыт.")
        return
    if cb.from_user.id != draft.author_tg_id:
        await cb.answer("Подтвердить или отменить может только автор черновика.", show_alert=True)
        return
    if not await repo.switch_draft(session, draft.id, "pending", "busy"):
        await cb.answer("Секунду, я ещё обрабатываю этот черновик.")
        return
    user = await repo.upsert_user(session, cb.from_user)
    await session.commit()

    decision = Decision(action=callback_data.action, by=user.id, bot_msg_id=draft.bot_msg_id)
    try:
        outcome = await assistant.resume(draft.thread_id, decision, session)
    except Exception:
        log.exception("confirm failed")
        await repo.switch_draft(session, draft.id, "busy", "pending")
        await session.commit()
        await cb.answer("Не получилось, попробуйте ещё раз.", show_alert=True)
        return

    if isinstance(outcome, Pending):  # граф отказался применять пустое
        await repo.switch_draft(session, draft.id, "busy", "pending")
        await session.commit()
        empty = (
            "Нечего применять: за эти дни нет чеков."
            if outcome.view["kind"] == "exclusion"
            else ("В черновике нет позиций.")
        )
        await cb.answer(f"{empty} Уточните или отмените.", show_alert=True)
        return

    r = outcome.result
    markup = None
    if r["kind"] == "excluded":
        status = "confirmed"
        text = render_exclusion(r["exclusion"], "✅ <b>Убрал из чеков</b>")
        text += "\n\nДолги пересчитаны — /debts."
        markup = exclusion_undo_kb(r["exclusion_id"])
    elif r["kind"] == "saved":
        status = "confirmed"
        text = render_draft(r["view"], f"✅ <b>Внесено #{r['expense_id']}</b>")
        text += "\n\n<i>Удалить: ответьте /delete на это сообщение.</i>"
        if r["joined"]:
            text += "\n👋 Плательщик теперь участвует в общем расчёте."
    else:
        status, text = "cancelled", "❌ Отменено."
    await repo.switch_draft(session, draft.id, "busy", status)
    await session.commit()
    await bot.edit_message_text(text, chat_id=draft.chat_id, message_id=draft.bot_msg_id, reply_markup=markup)
    await cb.answer()


@router.callback_query(ExclusionCb.filter())
async def on_exclusion_undo(cb: CallbackQuery, callback_data: ExclusionCb, session: AsyncSession) -> None:
    if (msg := callback_message(cb)) is None:
        await cb.answer("Сообщение слишком старое.", show_alert=True)
        return
    exclusion = await repo.get_exclusion(session, msg.chat.id, callback_data.exclusion_id)
    if exclusion is None or exclusion.reverted_at is not None:
        await cb.answer("Уже отменено.")
        return
    restored = await repo.revert_exclusion(session, exclusion)
    names = await repo.display_names(session, msg.chat.id, {exclusion.user_id})
    await session.commit()
    await msg.edit_text(
        f"↩️ Отменено: {escape(names[exclusion.user_id])} снова участвует в позициях за "
        f"{exclusion.date_from:%d.%m}–{exclusion.date_to:%d.%m} (вернул в {restored}). Долги пересчитаны — /debts."
    )
    await cb.answer()
