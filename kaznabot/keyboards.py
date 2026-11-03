from typing import Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from kaznabot.db import Expense
from kaznabot.money import fmt_rub


class DraftCb(CallbackData, prefix="draft"):
    action: Literal["ok", "cancel"]
    draft_id: int


class ExpenseCb(CallbackData, prefix="exp"):
    action: Literal["del", "restore"]
    expense_id: int


class ResetCb(CallbackData, prefix="reset"):
    action: Literal["ok", "cancel"]
    by: int  # tg_id того, кто запросил обнуление


class ExclusionCb(CallbackData, prefix="excl"):
    action: Literal["undo"]
    exclusion_id: int


def draft_kb(draft_id: int, ok_text: str = "✅ Внести") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=ok_text, callback_data=DraftCb(action="ok", draft_id=draft_id))
    kb.button(text="❌ Отмена", callback_data=DraftCb(action="cancel", draft_id=draft_id))
    return kb.as_markup()


def delete_list_kb(expenses: list[Expense]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for e in expenses:
        title = (e.title or "")[:24]
        kb.button(
            text=f"🗑 #{e.id} {title} {fmt_rub(e.total)}",
            callback_data=ExpenseCb(action="del", expense_id=e.id),
        )
    kb.adjust(1)
    return kb.as_markup()


def restore_kb(expense_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Вернуть", callback_data=ExpenseCb(action="restore", expense_id=expense_id))
    return kb.as_markup()


def exclusion_undo_kb(exclusion_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Отменить", callback_data=ExclusionCb(action="undo", exclusion_id=exclusion_id))
    return kb.as_markup()


def reset_kb(by: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, обнулить", callback_data=ResetCb(action="ok", by=by))
    kb.button(text="Отмена", callback_data=ResetCb(action="cancel", by=by))
    return kb.as_markup()
