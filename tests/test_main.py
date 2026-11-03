from kaznabot.__main__ import make_bot
from kaznabot.config import Settings


def settings(telegram_api_url: str | None) -> Settings:
    return Settings(bot_token="1:x", gigachat_credentials="x", telegram_api_url=telegram_api_url)


def test_bot_uses_cloud_api_by_default() -> None:
    api = make_bot(settings(None)).session.api
    assert api.api_url(token="1:x", method="getMe") == "https://api.telegram.org/bot1:x/getMe"
    assert not api.is_local


def test_bot_uses_self_hosted_api() -> None:
    api = make_bot(settings("http://127.0.0.1:8081")).session.api
    assert api.api_url(token="1:x", method="getMe") == "http://127.0.0.1:8081/bot1:x/getMe"
    assert api.is_local  # файлы читаются с диска, а не скачиваются по HTTP


def test_old_bot_api_poll_updates_parse() -> None:
    """Ответы сервера Bot API 9.0: без option_persistent_ids, persistent_id, allows_revoting, members_only."""
    from aiogram.methods import GetUpdates, SendPoll
    from aiogram.methods.base import Response
    from aiogram.types import Message, Update

    from kaznabot.tgcompat import json_loads

    updates = """{"ok": true, "result": [
      {"update_id": 1, "poll_answer": {"poll_id": "p1", "option_ids": [0, 3],
        "user": {"id": 7, "is_bot": false, "first_name": "Amir"}}},
      {"update_id": 2, "message": {"message_id": 5, "date": 0, "chat": {"id": -1, "type": "group"},
        "poll": {"id": "p2", "question": "?", "options": [{"text": "Пн", "voter_count": 0}],
        "total_voter_count": 0, "is_closed": false, "is_anonymous": false, "type": "regular",
        "allows_multiple_answers": true}}}]}"""
    parsed = Response[list[Update]].model_validate(json_loads(updates)).result
    assert parsed is not None
    assert parsed[0].poll_answer is not None
    assert parsed[0].poll_answer.option_ids == [0, 3]
    assert parsed[1].message is not None
    assert parsed[1].message.poll is not None
    assert GetUpdates.__returning__ == list[Update]
    assert SendPoll.__returning__ == Message
