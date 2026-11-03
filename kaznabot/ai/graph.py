"""Граф обработки сообщения.

    START → load_roster ─┬─(фото, /add)──────────────► read_receipt → extract → confirm ⇄ refine
                         ├─(текст) → classify ─┬─ expense ─┘                      │
                         │                     ├─ introduce → END                 ├─ ok → save → END
                         │                     ├─ debts / spent / other → END     └─ cancel → END
                         └─(/exclude) ─────────┴─ exclude ──► plan_exclusion → confirm_exclusion
                                                                  ▲  refine         │ ok → apply_exclusion → END
                                                                  └─────────────────┘ cancel → END

confirm останавливает граф через interrupt(): бот показывает черновик с кнопками, а состояние
сохраняется чекпоинтером. Нажатие кнопки или ответ на черновик продолжают тот же тред через
Command(resume=...). Узлы не знают про Telegram: всё общение с чатом — в хендлерах.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, TypedDict
from zoneinfo import ZoneInfo

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession

from kaznabot import repo
from kaznabot.ai import prompts
from kaznabot.ai.llm import AI
from kaznabot.ai.schemas import (
    Decision,
    Draft,
    DraftView,
    ExcludeRequest,
    ExclusionPlan,
    ExclusionView,
    Intent,
    IntroducedPerson,
    Mention,
    Receipt,
    Result,
    RosterEntry,
    to_draft,
    to_exclusion,
)
from kaznabot.db import User

log = logging.getLogger(__name__)


@dataclass
class Ctx:
    """Зависимости на время одного запуска графа (в чекпоинт не попадают)."""

    session: AsyncSession
    ai: AI
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Moscow"))


class State(TypedDict, total=False):
    # вход
    chat_id: int
    author_id: int  # users.id автора сообщения («я» в указаниях)
    payer_id: int  # users.id того, чей это чек (по умолчанию он платил)
    mode: Literal["auto", "expense", "exclude"]  # auto — сначала понять, чего хотят
    today: str  # ГГГГ-ММ-ДД по часовому поясу бота: от него считаются «вчера» и «с 12 по 18»
    addressed: bool  # сообщение адресовано боту; иначе реагируем только на знакомства
    text: str | None  # текст сообщения или PDF
    comment: str | None  # подпись к чеку / аргументы /add
    file_id: str | None  # картинка, загруженная в GigaChat
    mentions: list[Mention]
    # рабочие поля
    roster: list[RosterEntry]
    intent: dict[str, Any]  # Intent.model_dump()
    receipt_text: str | None
    draft: dict[str, Any]  # Draft.model_dump()
    exclusion: dict[str, Any]  # ExclusionPlan.model_dump(mode="json")
    exclusion_view: ExclusionView
    decision: Decision
    error: str | None  # llm — не удалось уточнить; empty — попытка внести пустой черновик
    # итог для хендлера
    result: Result


Graph = CompiledStateGraph[State, Ctx, State, State]


# --- узлы ---


def _today(state: State, runtime: Runtime[Ctx]) -> date:
    # Черновики, начатые до появления поля today, считаем от текущей даты
    return date.fromisoformat(state["today"]) if "today" in state else datetime.now(runtime.context.tz).date()


async def load_roster(state: State, runtime: Runtime[Ctx]) -> State:
    return {"roster": await repo.roster(runtime.context.session, state["chat_id"])}


async def classify(state: State, runtime: Runtime[Ctx]) -> State:
    msgs = prompts.intent_messages(state["text"] or "", state.get("mentions", []), state["roster"], state["author_id"])
    try:
        intent = await runtime.context.ai.ask(Intent, msgs)
    except Exception:
        log.exception("classify failed")
        return {"result": {"kind": "error" if state.get("addressed") else "ignored"}}

    dumped = intent.model_dump()
    if intent.intent == "introduce" and intent.people:
        return {"intent": dumped}
    if not state.get("addressed"):
        return {"intent": dumped, "result": {"kind": "ignored"}}
    match intent.intent:
        case "expense" | "exclude":
            return {"intent": dumped}
        case "debts" | "spent" | "other" as kind:
            return {"intent": dumped, "result": {"kind": kind}}
        case "introduce":  # знакомство, но без людей — не поняли, кого как зовут
            return {"intent": dumped, "result": {"kind": "other"}}


async def introduce(state: State, runtime: Runtime[Ctx]) -> State:
    session = runtime.context.session
    mentions = state.get("mentions", [])
    usernames = {m["ref"].lower() for m in mentions if m["ref"].startswith("@")}
    known_ids = {p["id"] for p in state["roster"]} | {m["user_id"] for m in mentions if "user_id" in m}

    done: list[IntroducedPerson] = []
    skipped: list[str] = []
    for person in Intent.model_validate(state["intent"]).people:
        ref = person.ref.strip()
        user: User | None = None
        if ref.lower() == "self":
            user = await session.get(User, state["author_id"])
        elif ref.startswith("id:") and ref[3:].isdigit() and int(ref[3:]) in known_ids:
            user = await session.get(User, int(ref[3:]))
        elif ("@" + ref.lstrip("@")).lower() in usernames:
            user = await repo.user_by_username(session, ref)
        name = person.name.strip()[:64]
        if user is None or not name:
            skipped.append(ref)
            continue
        name = name[0].upper() + name[1:]
        await repo.ensure_member(session, state["chat_id"], user, activate=True, display_name=name)
        done.append({"name": name, "label": f"@{user.username}" if user.username else user.name})
    await session.commit()
    return {"result": {"kind": "introduced", "people": done, "skipped": skipped}}


async def read_receipt(state: State, runtime: Runtime[Ctx]) -> State:
    file_id = state.get("file_id")
    if not file_id:
        return {"receipt_text": state.get("text")}
    try:
        text = await runtime.context.ai.transcribe(file_id)
    except Exception:
        log.exception("transcribe failed")
        return {"result": {"kind": "error"}}
    if text is None:
        return {"result": {"kind": "not_purchase"}}
    return {"receipt_text": text}


def _known_ids(roster: list[RosterEntry], state: State) -> set[int]:
    return {p["id"] for p in roster} | {state["author_id"], state["payer_id"]}


async def extract(state: State, runtime: Runtime[Ctx]) -> State:
    today = _today(state, runtime)
    msgs = prompts.receipt_messages(
        state.get("receipt_text"), state.get("comment"), state["roster"], state["author_id"], state["payer_id"], today
    )
    try:
        receipt = await runtime.context.ai.ask(Receipt, msgs)
    except Exception:
        log.exception("extract failed")
        return {"result": {"kind": "error"}}
    draft = to_draft(receipt, _known_ids(state["roster"], state), state["payer_id"], today)
    if not receipt.is_purchase or not draft.items:
        return {"result": {"kind": "not_purchase"}}
    return {"draft": draft.model_dump(), "error": None}


def confirm(state: State) -> State:
    # Код до interrupt() выполняется заново при каждом resume — здесь нет побочных эффектов
    decision: Decision = interrupt(draft_view(state))
    if decision["action"] == "ok" and not state["draft"]["items"]:
        return {"decision": decision, "error": "empty"}  # пустой черновик не вносим — снова ждём решения
    if decision["action"] == "cancel":
        return {"decision": decision, "error": None, "result": {"kind": "cancelled"}}
    return {"decision": decision, "error": None}


async def refine(state: State, runtime: Runtime[Ctx]) -> State:
    # Список людей перечитываем: пока черновик висел, кого-то могли представить
    roster = await repo.roster(runtime.context.session, state["chat_id"])
    draft = Draft.model_validate(state["draft"])
    msgs = prompts.refine_messages(
        state.get("receipt_text"),
        state.get("comment"),
        roster,
        state["author_id"],
        draft,
        state["decision"].get("text", ""),
        _today(state, runtime),
    )
    try:
        receipt = await runtime.context.ai.ask(Receipt, msgs)
    except Exception:
        log.exception("refine failed")
        return {"roster": roster, "error": "llm"}  # вернёмся в confirm с прежним черновиком
    new = to_draft(receipt, _known_ids(roster, state), draft.payer_id, _today(state, runtime))
    return {"roster": roster, "draft": new.model_dump()}


async def save(state: State, runtime: Runtime[Ctx]) -> State:
    session = runtime.context.session
    chat_id = state["chat_id"]
    draft = Draft.model_validate(state["draft"])
    default_ids = await repo.active_member_ids(session, chat_id)
    people = draft.participants(default_ids)

    expense = await repo.create_expense(
        session,
        chat_id=chat_id,
        payer_id=draft.payer_id,
        created_by_id=state["decision"]["by"],
        title=draft.store or ", ".join(i.name for i in draft.items[:3]),
        items=[(i.name, i.quantity, i.amount, p) for i, p in zip(draft.items, people, strict=True)],
        purchased_on=draft.purchase_date or _today(state, runtime),
    )
    expense.bot_msg_id = state["decision"].get("bot_msg_id")
    payer = await session.get_one(User, draft.payer_id)
    joined = await repo.ensure_member(session, chat_id, payer, activate=True)
    await session.commit()
    return {
        "result": {
            "kind": "saved",
            "expense_id": expense.id,
            "joined": joined,
            "view": draft_view(state, default_ids),
        }
    }


def draft_view(state: State, default_ids: list[int] | None = None) -> DraftView:
    """Всё, что нужно хендлеру, чтобы нарисовать черновик, — без походов в БД."""
    roster = state["roster"]
    if default_ids is None:
        default_ids = [p["id"] for p in roster if p["active"]]
    draft = Draft.model_validate(state["draft"])
    return {
        "kind": "draft",
        "draft": state["draft"],
        "names": {str(p["id"]): p["name"] for p in roster},
        "participants": draft.participants(default_ids),
        "everyone": sorted({*default_ids, draft.payer_id}),
        "error": state.get("error"),
    }


async def plan_exclusion(state: State, runtime: Runtime[Ctx]) -> State:
    """Понять, кого и за какие дни убрать, и посчитать, что изменится."""
    ctx = runtime.context
    today = _today(state, runtime)
    roster = await repo.roster(ctx.session, state["chat_id"])
    current = ExclusionPlan.model_validate(state["exclusion"]) if "exclusion" in state else None
    refining = current is not None and state.get("decision", {}).get("action") == "refine"
    instruction = state["decision"].get("text") if refining else None
    msgs = prompts.exclude_messages(state.get("text") or "", roster, state["author_id"], today, current, instruction)
    try:
        request = await ctx.ai.ask(ExcludeRequest, msgs)
        plan = to_exclusion(request, roster, state["author_id"], today)
    except Exception:
        log.exception("plan_exclusion failed")
        plan = "GigaChat не ответил"
    error: str | None = None
    if isinstance(plan, str):
        if current is None:
            return {"roster": roster, "result": {"kind": "unclear", "reason": plan}}
        plan, error = current, "llm"  # уточнение не удалось — показываем прежний вариант с предупреждением
    preview = await repo.preview_exclusion(
        ctx.session, state["chat_id"], plan.person_id, plan.date_from, plan.date_to, ctx.tz
    )
    return {
        "roster": roster,
        "exclusion": plan.model_dump(mode="json"),
        "exclusion_view": _exclusion_view(plan, preview, roster, error),
    }


def confirm_exclusion(state: State) -> State:
    view = state["exclusion_view"].copy()
    view["error"] = state.get("error") or view["error"]
    decision: Decision = interrupt(view)
    if decision["action"] == "ok" and state["exclusion_view"]["receipts"] == 0:
        return {"decision": decision, "error": "nothing"}  # нечего применять — снова ждём решения
    if decision["action"] == "cancel":
        return {"decision": decision, "error": None, "result": {"kind": "cancelled"}}
    return {"decision": decision, "error": None}


async def apply_exclusion(state: State, runtime: Runtime[Ctx]) -> State:
    ctx = runtime.context
    plan = ExclusionPlan.model_validate(state["exclusion"])
    record, preview = await repo.apply_exclusion(
        ctx.session,
        chat_id=state["chat_id"],
        user_id=plan.person_id,
        start=plan.date_from,
        end=plan.date_to,
        created_by_id=state["decision"]["by"],
        tz=ctx.tz,
    )
    await ctx.session.commit()
    view = _exclusion_view(plan, preview, state["roster"], None)
    return {"result": {"kind": "excluded", "exclusion_id": record.id, "exclusion": view}}


def _exclusion_view(
    plan: ExclusionPlan, preview: repo.ExclusionPreview, roster: list[RosterEntry], error: str | None
) -> ExclusionView:
    names = {p["id"]: p["name"] for p in roster}
    return {
        "kind": "exclusion",
        "person": names.get(plan.person_id, f"id{plan.person_id}"),
        "date_from": plan.date_from.isoformat(),
        "date_to": plan.date_to.isoformat(),
        "receipts": preview.receipts,
        "items": len(preview.hits),
        "amount": preview.amount,
        "solo": preview.solo,
        "settled": preview.settled,
        "note": plan.note,
        "error": error,
    }


# --- рёбра ---


def _done_or(next_node: str) -> Callable[[State], str]:
    def route(state: State) -> str:
        return END if state.get("result") else next_node

    return route


def after_roster(state: State) -> str:
    return {"expense": "read_receipt", "exclude": "plan_exclusion"}.get(state["mode"], "classify")


def after_classify(state: State) -> str:
    if state.get("result"):
        return END
    return {"introduce": "introduce", "exclude": "plan_exclusion"}.get(state["intent"]["intent"], "read_receipt")


def after_confirm(state: State) -> str:
    if state.get("result"):
        return END
    if state.get("error") == "empty":
        return "confirm"
    return "save" if state["decision"]["action"] == "ok" else "refine"


def after_confirm_exclusion(state: State) -> str:
    if state.get("result"):
        return END
    if state.get("error") == "nothing":
        return "confirm_exclusion"
    return "apply_exclusion" if state["decision"]["action"] == "ok" else "plan_exclusion"


def build_graph(checkpointer: BaseCheckpointSaver[Any]) -> Graph:
    g = StateGraph(State, context_schema=Ctx)
    g.add_node(load_roster)
    g.add_node(classify)
    g.add_node(introduce)
    g.add_node(read_receipt)
    g.add_node(extract)
    g.add_node(confirm)
    g.add_node(refine)
    g.add_node(save)
    g.add_node(plan_exclusion)
    g.add_node(confirm_exclusion)
    g.add_node(apply_exclusion)

    g.add_edge(START, "load_roster")
    g.add_conditional_edges("load_roster", after_roster, ["classify", "read_receipt", "plan_exclusion"])
    g.add_conditional_edges("classify", after_classify, ["introduce", "read_receipt", "plan_exclusion", END])
    g.add_edge("introduce", END)
    g.add_conditional_edges("read_receipt", _done_or("extract"), ["extract", END])
    g.add_conditional_edges("extract", _done_or("confirm"), ["confirm", END])
    g.add_conditional_edges("confirm", after_confirm, ["save", "refine", "confirm", END])
    g.add_edge("refine", "confirm")
    g.add_edge("save", END)
    g.add_conditional_edges("plan_exclusion", _done_or("confirm_exclusion"), ["confirm_exclusion", END])
    g.add_conditional_edges(
        "confirm_exclusion", after_confirm_exclusion, ["apply_exclusion", "plan_exclusion", "confirm_exclusion", END]
    )
    g.add_edge("apply_exclusion", END)
    return g.compile(checkpointer=checkpointer)
