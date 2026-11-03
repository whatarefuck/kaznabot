"""Достаёт из Telegram-сообщения чек: фото, картинку-документ или PDF."""

from dataclasses import dataclass
from typing import Literal

import pymupdf
from aiogram import Bot
from aiogram.types import Message

MIN_PDF_TEXT = 40  # меньше — считаем, что PDF отсканирован картинкой


@dataclass
class Attachment:
    kind: Literal["image", "pdf"]
    file_id: str
    mime: str
    filename: str


@dataclass
class Loaded:
    text: str | None = None  # текстовый PDF
    image: tuple[bytes, str, str] | None = None  # (данные, имя файла, mime) — отправить в GigaChat


def find_attachment(message: Message) -> Attachment | None:
    if message.photo:
        return Attachment("image", message.photo[-1].file_id, "image/jpeg", "receipt.jpg")
    doc = message.document
    if doc is None:
        return None
    mime = doc.mime_type or ""
    name = doc.file_name or "receipt"
    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        return Attachment("pdf", doc.file_id, "application/pdf", name)
    if mime in ("image/jpeg", "image/png"):
        return Attachment("image", doc.file_id, mime, name)
    return None


async def load_attachment(bot: Bot, att: Attachment) -> Loaded:
    file = await bot.download(att.file_id)
    if file is None:  # без destination aiogram всегда возвращает BytesIO
        raise RuntimeError(f"не удалось скачать файл {att.file_id}")
    data = file.read()
    if att.kind == "image":
        return Loaded(image=(data, att.filename, att.mime))
    text, png = pdf_text_or_image(data)
    return Loaded(text=text) if text else Loaded(image=(png, "receipt.png", "image/png"))


def pdf_text_or_image(data: bytes) -> tuple[str | None, bytes]:
    """Электронный чек (текстовый PDF) — отдаём текстом, скан — первой страницей в PNG."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        text = "\n".join(page.get_text() for page in doc.pages(0, min(3, doc.page_count)))
        if len(text.strip()) >= MIN_PDF_TEXT:
            return text.strip(), b""
        return None, doc[0].get_pixmap(dpi=200).tobytes("png")
