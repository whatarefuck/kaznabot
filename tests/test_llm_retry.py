import asyncio

import httpx
import pytest
from gigachat.exceptions import BadRequestError, RateLimitError

from kaznabot.ai.llm import call_with_retries


def rate_limited() -> RateLimitError:
    return RateLimitError("https://api.giga.chat/v1/chat/completions", 429, b"Too Many Requests", None)


class Flaky:
    """Падает заданными ошибками, потом отвечает «ok»; считает вызовы и одновременные запросы."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def __call__(self) -> str:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            if self.errors:
                raise self.errors.pop(0)
            return "ok"
        finally:
            self.active -= 1


async def run(call: Flaky, slot: asyncio.Semaphore | None = None, attempts: int = 3) -> str:
    return await call_with_retries(call, slot or asyncio.Semaphore(1), attempts=attempts, base_delay=0, what="test")


async def test_retries_rate_limit_and_timeout() -> None:
    call = Flaky(rate_limited(), httpx.ReadTimeout("slow"))
    assert await run(call) == "ok"
    assert call.calls == 3


async def test_gives_up_after_last_attempt() -> None:
    call = Flaky(rate_limited(), rate_limited(), rate_limited())
    with pytest.raises(RateLimitError):
        await run(call)
    assert call.calls == 3


async def test_does_not_retry_real_errors() -> None:
    call = Flaky(BadRequestError("u", 400, b"bad", None))
    with pytest.raises(BadRequestError):
        await run(call)
    assert call.calls == 1


async def test_requests_go_one_at_a_time() -> None:
    slot = asyncio.Semaphore(1)
    call = Flaky()
    assert await asyncio.gather(*(run(call, slot) for _ in range(5))) == ["ok"] * 5
    assert call.max_active == 1
