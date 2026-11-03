"""Фейковая LLM: отдаёт заранее заготовленные ответы и запоминает, что у неё спросили."""

from collections.abc import Callable

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

type Answer = BaseModel | Callable[[list[BaseMessage]], BaseModel] | Exception


class FakeAI:
    def __init__(self, *answers: Answer) -> None:
        self.answers: list[Answer] = list(answers)
        self.calls: list[tuple[str, list[BaseMessage]]] = []
        self.transcript: str | None = "Пятёрочка\nМолоко | 1 | 100\nТворог | 1 | 200"

    async def ask[M: BaseModel](self, schema: type[M], messages: list[BaseMessage]) -> M:
        self.calls.append((schema.__name__, messages))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if not isinstance(answer, BaseModel):
            answer = answer(messages)
        assert isinstance(answer, schema), f"ждали {schema.__name__}, в очереди {type(answer).__name__}"
        return answer

    async def transcribe(self, file_id: str) -> str | None:
        self.calls.append(("transcribe", []))
        return self.transcript

    async def upload_image(self, data: bytes, filename: str, mime: str) -> str:
        return "giga-file-1"

    def prompt(self, i: int = -1) -> str:
        return "\n".join(str(m.content) for m in self.calls[i][1])
