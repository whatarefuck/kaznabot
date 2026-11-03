from datetime import time
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    # Свой сервер Bot API (github.com/tdlib/telegram-bot-api), запущенный с --local, например http://127.0.0.1:8081.
    # Нужен там, где api.telegram.org недоступен. Пусто — обычный api.telegram.org.
    telegram_api_url: str | None = None

    # Ключ авторизации из личного кабинета developers.sber.ru (Authorization key)
    gigachat_credentials: str
    gigachat_scope: str = "GIGACHAT_API_PERS"
    # Нужна модель с поддержкой изображений. GigaChat-2-Max на личном тарифе бывает очень медленным
    # (до минуты на ответ), а GigaChat-3-Pro хуже делит чек по людям — поэтому по умолчанию GigaChat-2-Pro.
    gigachat_model: str = "GigaChat-2-Pro"
    gigachat_verify_ssl: bool = True
    # Обычно GigaChat отвечает за 1-3 с. Оборванный по таймауту запрос GigaChat всё равно дорабатывает
    # и всё это время держит единственный слот, поэтому таймаут — с запасом.
    gigachat_timeout: float = 60
    gigachat_attempts: int = 3
    # Сколько запросов к GigaChat одновременно. Личный тариф (GIGACHAT_API_PERS) пускает только один.
    gigachat_concurrency: int = 1
    # Путь к сертификату НУЦ Минцифры (russian_trusted_root_ca.cer), если он не установлен в систему
    gigachat_ca_bundle: str | None = None

    database_url: str = "sqlite+aiosqlite:///data/kaznabot.db"
    # Чекпоинтер LangGraph: здесь живут черновики, ждущие подтверждения
    checkpoint_path: str = "data/checkpoints.db"
    timezone: str = "Europe/Moscow"
    # Разбирать все фото в чате автоматически (не-чеки бот молча игнорирует).
    # Если выключено — только фото с подписью /add или @упоминанием бота.
    auto_parse_photos: bool = True

    # Расписание быта (время местное, см. timezone)
    duty_reminder_time: time = time(19, 0)  # суббота: кто дежурит в воскресенье
    dinner_poll_time: time = time(12, 0)  # суббота: голосование «кто когда готовит ужин»
    dinner_deadline_time: time = time(13, 0)  # понедельник: итог голосования

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)
