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
