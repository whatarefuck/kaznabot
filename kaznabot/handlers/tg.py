"""Сужение типов aiogram: многие поля апдейтов Optional, хотя в наших сценариях они есть всегда."""

from aiogram import Bot
from aiogram.types import CallbackQuery, Message, User


def sender(message: Message) -> User:
    """Автор сообщения. Роутеры пропускают только сообщения от людей, так что он есть."""
    if message.from_user is None:
        raise ValueError("сообщение без автора")
    return message.from_user


def callback_message(cb: CallbackQuery) -> Message | None:
    """Сообщение с кнопкой; None — если оно старше 48 часов и Telegram прислал только его id."""
    return cb.message if isinstance(cb.message, Message) else None


async def bot_username(bot: Bot) -> str:
    """У ботов @username есть всегда; aiogram хранит его как Optional, потому что это общий тип User."""
    return (await bot.me()).username or ""
