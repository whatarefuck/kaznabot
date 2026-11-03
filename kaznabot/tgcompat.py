"""Совместимость со своим сервером Bot API старой версии.

aiogram описывает Bot API 10.x, где у опросов появились обязательные поля. Сервер Bot API 9.0
(github.com/tdlib/telegram-bot-api) их не присылает, и aiogram не может разобрать ответ —
а один такой апдейт в getUpdates останавливает приём всех остальных. Поэтому при разборе JSON
недостающие поля заполняются значениями по умолчанию; ответы нового сервера не меняются.
"""

import json
from typing import Any


def _fill(obj: dict[str, Any]) -> dict[str, Any]:
    if "poll_id" in obj and "option_ids" in obj:  # PollAnswer
        obj.setdefault("option_persistent_ids", [str(i) for i in obj["option_ids"]])
    elif "total_voter_count" in obj and "options" in obj:  # Poll
        obj.setdefault("allows_revoting", obj.get("type") != "quiz")
        obj.setdefault("members_only", False)
    elif "voter_count" in obj and "text" in obj:  # PollOption
        obj.setdefault("persistent_id", "")
    return obj


def json_loads(data: str | bytes) -> Any:
    return json.loads(data, object_hook=_fill)
