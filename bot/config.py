"""Единая точка чтения настроек и секретов.

Все ключи и приватные данные проекта живут в одном файле — `.env` в корне:

    TELEGRAM_BOT_TOKEN            — токен бота (вход через Telegram)
    TELEGRAM_BOT_USERNAME         — имя бота без @
    TELEGRAM_AUTH_CALLBACK_URL    — публичный HTTPS-адрес callback
    WEB_SESSION_SECRET            — подпись сессионных cookie
    GOOGLE_SERVICE_ACCOUNT_JSON   — сервисный аккаунт Google одной строкой JSON

Файл `credentials.json` поддерживается как запасной вариант, чтобы старые
запуски не сломались, но приоритет всегда у переменных окружения.
"""

import os
import json
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CREDENTIALS_PATH = _ROOT / "credentials.json"


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


def _credentials_file() -> dict:
    """Содержимое credentials.json или пустой словарь."""
    if not _CREDENTIALS_PATH.exists():
        return {}
    try:
        with _CREDENTIALS_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_token() -> str | None:
    """Токен бота: сначала переменная окружения, потом credentials.json."""
    return os.getenv("TELEGRAM_BOT_TOKEN") or _credentials_file().get("bot_token") or None


def google_service_account() -> dict | None:
    """Данные сервисного аккаунта Google для синхронизации с таблицами.

    Приоритет — `GOOGLE_SERVICE_ACCOUNT_JSON` (одна строка JSON в .env),
    иначе — credentials.json без поля bot_token.
    """
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("client_email") and parsed.get("private_key"):
            return parsed

    data = _credentials_file()
    data.pop("bot_token", None)
    return data or None


@dataclass
class BotConfig:
    token: str | None = _load_token()

    # ИСПОЛЬЗУЕМ SQLITE вместо PostgreSQL
    database_url: str = "sqlite:///coffee_quality.db"
