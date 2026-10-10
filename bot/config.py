"""Единая точка чтения настроек и секретов.

Все ключи и приватные данные проекта живут в одном файле — `.env` в корне:

    TELEGRAM_BOT_TOKEN            — токен бота (вход через Telegram)
    TELEGRAM_BOT_USERNAME         — имя бота без @
    TELEGRAM_AUTH_CALLBACK_URL    — публичный HTTPS-адрес callback
    WEB_SESSION_SECRET            — подпись сессионных cookie
    WEB_HOST / WEB_PORT           — адрес и порт веб-сервиса
    DATABASE_URL                  — путь к SQLite-базе (по умолчанию в корне)

Секретов в коде нет: токен и остальные значения приходят только из `.env`
или из переменных окружения.
"""

import os
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def load_env_file() -> None:
    """Читает .env в корне проекта.

    `.env` — единственный источник настроек, поэтому его значения перекрывают
    уже существующие переменные окружения (`override=True`). Иначе устаревшая
    переменная из терминала или из systemd может незаметно подменить домен,
    имя бота или токен.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(_ROOT / ".env", override=True)


# Читаем .env сразу при импорте — до того, как значения ниже будут вычислены.
load_env_file()


def _load_token() -> str | None:
    """Токен бота из TELEGRAM_BOT_TOKEN. Секреты живут только в .env."""
    return os.getenv("TELEGRAM_BOT_TOKEN") or None


def _load_flag(name: str, default: bool = False) -> bool:
    """Булев флаг из окружения: 1/true/yes/on — включено, пусто — значение по умолчанию."""
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


@dataclass
class BotConfig:
    token: str | None = _load_token()

    # Вход через Telegram: кнопка, виджет и роуты /auth/telegram/*.
    # Выключен: с этого сервера api.telegram.org недоступен, вход переводится
    # на «iiko_id + персональный код доступа».
    telegram_login_enabled: bool = _load_flag("AUTH_TELEGRAM_ENABLED")

    # Исходящие вызовы Bot API: публикации в каналы и фото профиля из Telegram.
    # Выключено по умолчанию: недоступный Telegram висит таймаутом 20 с прямо
    # внутри запроса сохранения графика, из-за чего nginx отдаёт 504.
    telegram_outbound_enabled: bool = _load_flag("TELEGRAM_OUTBOUND_ENABLED")

    # Путь к базе можно переопределить через DATABASE_URL.
    # В Docker удобно держать базу рядом с остальными данными:
    #   DATABASE_URL=sqlite:////app/data/coffee_quality.db
    # (четыре слэша перед абсолютным путём — это синтаксис SQLAlchemy для SQLite)
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///coffee_quality.db")
