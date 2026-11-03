"""Запуск графа: новый тред на каждое сообщение, продолжение треда по кнопке или ответу на черновик."""

from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from kaznabot.ai.graph import Ctx, State, build_graph
from kaznabot.ai.llm import AI
from kaznabot.ai.schemas import Decision, PendingView, Result


@dataclass
class Pending:
    """Граф ждёт решения: по черновику траты или по исключению человека из чеков."""

    view: PendingView


@dataclass
class Done:
    result: Result


DEFAULT_TZ = ZoneInfo("Europe/Moscow")


class Assistant:
    def __init__(self, ai: AI, checkpointer: BaseCheckpointSaver[Any], tz: ZoneInfo = DEFAULT_TZ):
        self.ai = ai
        self.tz = tz
        self.checkpointer = checkpointer
        self.graph = build_graph(checkpointer)

    async def start(self, thread_id: str, state: State, session: AsyncSession) -> Pending | Done:
        return await self._run(thread_id, state, session)

    async def resume(self, thread_id: str, decision: Decision, session: AsyncSession) -> Pending | Done:
        return await self._run(thread_id, Command(resume=decision), session)

    async def _run(self, thread_id: str, payload: State | Command[Any], session: AsyncSession) -> Pending | Done:
        # chat_id в метаданных — чтобы в LangSmith отфильтровать трейсы одного чата (тред = «чат:сообщение»)
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "metadata": {"chat_id": thread_id.split(":", 1)[0]},
        }
        out = await self.graph.ainvoke(payload, config, context=Ctx(session=session, ai=self.ai, tz=self.tz))
        if interrupts := out.get("__interrupt__"):
            return Pending(interrupts[0].value)
        # Тред закончен — чекпоинты больше не нужны
        await self.checkpointer.adelete_thread(thread_id)
        return Done(out.get("result") or Result(kind="ignored"))
