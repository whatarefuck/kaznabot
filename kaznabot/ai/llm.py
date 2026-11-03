"""Обёртка над langchain-gigachat: structured output с повтором, распознавание картинок."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any, Protocol, cast

import httpx
from gigachat.exceptions import RateLimitError, ServerError
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_gigachat import GigaChat
from pydantic import BaseModel

from kaznabot.ai import prompts
from kaznabot.config import Settings

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


# Временные сбои: GigaChat занят (429), упал (5xx), завис или оборвал соединение
RETRYABLE = (RateLimitError, ServerError, httpx.TimeoutException, httpx.NetworkError)


async def call_with_retries[T](
    call: Callable[[], Awaitable[T]],
    slot: asyncio.Semaphore,
    *,
    attempts: int,
    base_delay: float,
    what: str,
) -> T:
    """Один запрос к GigaChat: ждёт свободный слот и повторяет попытку при временном сбое.

    Слот держится только на время самого запроса: пауза перед повтором его не занимает.
    """
    for attempt in range(1, attempts + 1):
        async with slot:
            try:
                return await call()
            except RETRYABLE as e:
                if attempt == attempts:
                    raise
                log.warning("GigaChat %s: %s, попытка %d из %d", what, type(e).__name__, attempt, attempts)
        await asyncio.sleep(base_delay * 2 ** (attempt - 1))
    raise AssertionError("attempts must be >= 1")


class AI(Protocol):
    """То, чем пользуется граф. В тестах подменяется фейком."""

    async def ask[M: BaseModel](self, schema: type[M], messages: list[BaseMessage]) -> M: ...

    async def transcribe(self, file_id: str) -> str | None: ...

    async def upload_image(self, data: bytes, filename: str, mime: str) -> str: ...


class GigaChatAI:
    def __init__(self, settings: Settings):
        self.model = GigaChat(
            credentials=settings.gigachat_credentials,
            scope=settings.gigachat_scope,
            model=settings.gigachat_model,
            verify_ssl_certs=settings.gigachat_verify_ssl,
            ca_bundle_file=settings.gigachat_ca_bundle,
            timeout=settings.gigachat_timeout,
            temperature=0.1,
        )
        # На личном тарифе GigaChat обрабатывает один запрос за раз, параллельный получает 429.
        # Поэтому запросы бота идут к нему по очереди.
        self._slot = asyncio.Semaphore(settings.gigachat_concurrency)
        self._attempts = settings.gigachat_attempts

    async def _call[T](self, what: str, call: Callable[[], Awaitable[T]]) -> T:
        return await call_with_retries(call, self._slot, attempts=self._attempts, base_delay=3.0, what=what)

    async def upload_image(self, data: bytes, filename: str, mime: str) -> str:
        uploaded = await self._call("upload", lambda: self.model.aupload_file((filename, data, mime)))
        return uploaded.id_

    async def ask[M: BaseModel](self, schema: type[M], messages: list[BaseMessage]) -> M:
        """Structured output через function calling GigaChat; при невалидном ответе — ещё одна попытка."""
        runnable = self.model.with_structured_output(schema, include_raw=True)
        for attempt in range(2):
            # include_raw=True → всегда {"raw": ..., "parsed": ..., "parsing_error": ...}
            out = cast(dict[str, Any], await self._call(schema.__name__, partial(runnable.ainvoke, messages)))
            if isinstance(parsed := out["parsed"], schema):
                return parsed
            error = out["parsing_error"] or "модель не вызвала функцию"
            log.warning("GigaChat: невалидный ответ (попытка %d): %s\n%s", attempt + 1, error, out["raw"])
            messages = [
                *messages,
                HumanMessage(f"Ответ не прошёл проверку: {error}. Вызови функцию {schema.__name__} ещё раз."),
            ]
        raise LLMError(f"GigaChat не вернул {schema.__name__}")

    async def transcribe(self, file_id: str) -> str | None:
        """Фото чека → текст. Отдельным шагом: vision и function calling в одном запросе работают хуже."""
        resp = await self._call("transcribe", lambda: self.model.ainvoke(prompts.transcribe_messages(file_id)))
        text = resp.text.strip()
        return None if not text or text.upper().startswith("НЕТ") else text
