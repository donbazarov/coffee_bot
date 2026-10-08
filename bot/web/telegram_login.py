"""Вход через Telegram по ссылке на бота.

Зачем это нужно. Штатный виджет Telegram грузится с `telegram.org`, который
в России блокируется, а на телефоне уходит в резервный сценарий с ручным
вводом номера. Схема через бота работает иначе:

    1. сайт создаёт одноразовый токен и отправляет браузер на
       https://t.me/<бот>?start=<токен>
    2. открывается ПРИЛОЖЕНИЕ Telegram, пользователь жмёт «Start»
    3. процесс бота (bot/web/telegram_login_bot.py) получает `/start <токен>`
       и помечает вход подтверждённым
    4. сайт, опрашивая статус, ставит сессионную cookie

Приложение Telegram общается своим протоколом, поэтому шаг 2 работает даже
там, где `telegram.org` в браузере недоступен.

Токен живёт 10 минут, хранится в таблице `web_login_requests` и удаляется
после использования.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Сколько живёт ссылка на вход
LOGIN_REQUEST_TTL = timedelta(minutes=10)

# Статусы заявки на вход
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"

# Причины отказа — совпадают со статусами register_or_find_telegram_user,
# по ним страница входа показывает понятный текст.
REASON_MESSAGES = {
    "pending": "Запрос отправлен. Администратор выдаст вам роль — зайдите ещё раз позже.",
    "disabled": "Доступ к вашей учётной записи отключён. Обратитесь к администратору.",
    "conflict": "Не удалось однозначно сопоставить аккаунт. Обратитесь к администратору.",
    "expired": "Ссылка на вход устарела. Попробуйте войти заново.",
    "unknown": "Ссылка недействительна. Попробуйте войти заново.",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def initialize_login_schema(engine: Engine) -> None:
    """Создаёт таблицу заявок на вход. Идемпотентно."""
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_login_requests (
                token       TEXT PRIMARY KEY,
                status      TEXT NOT NULL DEFAULT 'pending',
                reason      TEXT,
                user_id     INTEGER,
                next_path   TEXT NOT NULL DEFAULT '/',
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_web_login_expires
            ON web_login_requests (expires_at)
        """))


def _clean_next_path(raw: str | None) -> str:
    """Разрешаем возврат только на внутренние адреса — без open redirect."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw[:200]


def create_login_request(engine: Engine, next_path: str | None = None) -> str:
    """Создаёт заявку на вход и возвращает её токен."""
    token = secrets.token_urlsafe(24)
    now = _now()
    with engine.begin() as connection:
        # Заодно подчищаем просроченные — таблица иначе растёт бесконечно.
        connection.execute(
            text("DELETE FROM web_login_requests WHERE expires_at < :cutoff"),
            {"cutoff": (now - timedelta(days=1)).isoformat(timespec="seconds")},
        )
        connection.execute(
            text("""
                INSERT INTO web_login_requests (token, status, next_path, created_at, expires_at)
                VALUES (:token, :status, :next_path, :created_at, :expires_at)
            """),
            {
                "token": token,
                "status": STATUS_PENDING,
                "next_path": _clean_next_path(next_path),
                "created_at": now.isoformat(timespec="seconds"),
                "expires_at": (now + LOGIN_REQUEST_TTL).isoformat(timespec="seconds"),
            },
        )
    return token


def get_login_request(engine: Engine, token: str | None) -> dict[str, Any] | None:
    """Возвращает заявку, если токен существует и не просрочен."""
    if not token:
        return None
    with engine.connect() as connection:
        row = connection.execute(
            text("""
                SELECT token, status, reason, user_id, next_path, expires_at
                FROM web_login_requests WHERE token = :token
            """),
            {"token": token},
        ).mappings().first()
    if row is None:
        return None

    try:
        expires_at = datetime.fromisoformat(row["expires_at"])
    except (TypeError, ValueError):
        return None
    if expires_at.tzinfo is None:
        # Значения вида CURRENT_TIMESTAMP из SQLite приходят без зоны —
        # считаем их UTC, иначе сравнение с aware-временем падает.
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < _now():
        return {"token": row["token"], "status": STATUS_DENIED, "reason": "expired",
                "user_id": None, "next_path": row["next_path"]}
    return dict(row)


def approve_login_request(engine: Engine, token: str, user_id: int) -> bool:
    """Помечает вход подтверждённым. Возвращает False, если заявка не найдена."""
    with engine.begin() as connection:
        result = connection.execute(
            text("""
                UPDATE web_login_requests
                SET status = :status, reason = NULL, user_id = :user_id
                WHERE token = :token AND status = :pending
            """),
            {"status": STATUS_APPROVED, "user_id": user_id,
             "token": token, "pending": STATUS_PENDING},
        )
        return result.rowcount > 0


def deny_login_request(engine: Engine, token: str, reason: str) -> bool:
    """Помечает вход отклонённым с причиной."""
    with engine.begin() as connection:
        result = connection.execute(
            text("""
                UPDATE web_login_requests
                SET status = :status, reason = :reason
                WHERE token = :token AND status = :pending
            """),
            {"status": STATUS_DENIED, "reason": reason,
             "token": token, "pending": STATUS_PENDING},
        )
        return result.rowcount > 0


def consume_login_request(engine: Engine, token: str) -> None:
    """Удаляет заявку после успешного входа — токен одноразовый."""
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM web_login_requests WHERE token = :token"),
            {"token": token},
        )


def reason_message(reason: str | None) -> str:
    return REASON_MESSAGES.get(reason or "", REASON_MESSAGES["unknown"])
