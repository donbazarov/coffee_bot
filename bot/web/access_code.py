"""Вход по коду доступа: схема, выдача кодов и защита от подбора.

Штатный вход — «iiko_id (или @telegram_username) + код из четырёх цифр».
Код наставник видит в разделе «Сотрудники» и выдаёт сотруднику лично;
сотрудник может сменить свой код в личном кабинете.

**Почему код лежит в открытом виде.** Наставник должен иметь возможность
посмотреть и продиктовать код в любой момент, а хеш такую возможность убирает —
пришлось бы каждый раз генерировать новый. Ценность кода — доступ к графику
смен внутри команды, поэтому открытое хранение выбрано сознательно.

**Почему это всё-таки не «вход по iiko_id».** Четыре цифры подбираются за минуты,
поэтому подбор ограничен двумя барьерами: счётчиком неудач в самой записи
сотрудника (`failed_logins` / `locked_until`) и журналом `web_login_attempts`,
по которому считается лимит на IP.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger("bot.web.access")

CODE_LENGTH = 4
MAX_ATTEMPTS = 5  # неудач подряд до блокировки записи сотрудника
LOCK_MINUTES = 15  # первая блокировка
LOCK_MINUTES_HARD = 60  # если неудачи продолжаются
ATTEMPT_WINDOW_MINUTES = 15  # окно для лимита по IP
MAX_IP_ATTEMPTS = 20  # неудач с одного адреса за окно

# Формат совпадает с SQLite CURRENT_TIMESTAMP, поэтому сравнение в SQL строковое.
STAMP = "%Y-%m-%d %H:%M:%S"

STAFF_ROLES = ("barista", "senior", "mentor")


def _stamp(moment: datetime) -> str:
    return moment.strftime(STAMP)


def _now() -> datetime:
    return datetime.utcnow()


def _add_missing_columns(connection: Any, table: str, columns: dict[str, str]) -> None:
    """Докидывает колонки в существующую таблицу (SQLite).

    Тот же приём, что и в `calendar_service._ensure_columns`: база живёт между
    релизами, а `CREATE TABLE IF NOT EXISTS` её уже созданную версию не меняет.
    """
    existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def initialize_access_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            _add_missing_columns(connection, "users", {
                "access_code": "TEXT",
                "code_updated_at": "TEXT",
                "session_epoch": "INTEGER NOT NULL DEFAULT 0",
                "failed_logins": "INTEGER NOT NULL DEFAULT 0",
                "locked_until": "TEXT",
            })
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                login TEXT,
                ip TEXT,
                ok INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_web_login_attempts_time
            ON web_login_attempts (created_at)
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_web_login_attempts_ip
            ON web_login_attempts (ip, created_at)
        """))


# --- Коды -------------------------------------------------------------------- #


def generate_code() -> str:
    """Четыре цифры из криптографического источника (не random)."""
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def normalize_login(raw: Any) -> str:
    return str(raw or "").strip().lstrip("@")


def code_matches(user: dict[str, Any], code: Any) -> bool:
    stored = str(user.get("access_code") or "").strip()
    supplied = str(code or "").strip()
    if not stored or not supplied:
        return False
    return hmac.compare_digest(stored, supplied)


def is_staff(user: dict[str, Any]) -> bool:
    return bool(user.get("is_active")) and user.get("role") in STAFF_ROLES


def find_user_by_login(engine: Engine, raw: Any) -> dict[str, Any] | None:
    """Цифры ищем по `iiko_id`, всё остальное — по `telegram_username`."""
    login = normalize_login(raw)
    if not login:
        return None

    columns = """
        id, name, display_name, role, is_active, iiko_id, telegram_username,
        access_code, failed_logins, locked_until, session_epoch
    """
    with engine.connect() as connection:
        if login.isdigit():
            row = connection.execute(
                text(f"SELECT {columns} FROM users WHERE iiko_id = :value"),
                {"value": int(login)},
            ).mappings().first()
        else:
            row = connection.execute(
                text(f"""
                    SELECT {columns} FROM users
                    WHERE lower(replace(replace(telegram_username, '@', ''), ' ', '')) = :value
                """),
                {"value": login.lower()},
            ).mappings().first()
    return dict(row) if row else None


def set_access_code(engine: Engine, user_id: int, code: str | None = None) -> str:
    """Выдаёт новый код. `code` можно задать вручную (для тестов и миграций)."""
    value = (code or generate_code()).strip()
    with engine.begin() as connection:
        connection.execute(
            text("""
                UPDATE users
                SET access_code = :code, code_updated_at = :stamp, failed_logins = 0, locked_until = NULL
                WHERE id = :id
            """),
            {"code": value, "stamp": _stamp(_now()), "id": user_id},
        )
    return value


def ensure_active_codes(engine: Engine) -> int:
    """Выдаёт коды всем активным сотрудникам, у кого их ещё нет.

    Вызывается на старте: раскатка новой схемы входа не требует ручной работы
    наставника по каждому человеку.
    """
    with engine.connect() as connection:
        missing = connection.execute(text("""
            SELECT id FROM users
            WHERE is_active = 1 AND role IN ('barista', 'senior', 'mentor')
              AND (access_code IS NULL OR access_code = '')
        """)).scalars().all()
    for user_id in missing:
        set_access_code(engine, int(user_id))
    if missing:
        logger.info("Выданы коды доступа для %s сотрудников", len(missing))
    return len(missing)


# --- Защита от подбора ------------------------------------------------------- #


def _recent_ip_failures(engine: Engine, ip: str) -> int:
    if not ip:
        return 0
    since = _stamp(_now() - timedelta(minutes=ATTEMPT_WINDOW_MINUTES))
    with engine.connect() as connection:
        return int(connection.execute(text("""
            SELECT COUNT(*) FROM web_login_attempts
            WHERE ip = :ip AND ok = 0 AND created_at >= :since
        """), {"ip": ip, "since": since}).scalar_one())


def throttle_reason(engine: Engine, user: dict[str, Any] | None, ip: str) -> str | None:
    """Возвращает причину блокировки или None, если вход разрешён."""
    if user:
        locked_until = str(user.get("locked_until") or "").strip()
        if locked_until and locked_until > _stamp(_now()):
            return "locked"
    if _recent_ip_failures(engine, ip) >= MAX_IP_ATTEMPTS:
        return "locked"
    return None


def record_attempt(
    engine: Engine,
    user_id: int | None,
    login: str,
    ip: str,
    ok: bool,
    reason: str | None = None,
) -> None:
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO web_login_attempts (user_id, login, ip, ok, reason)
            VALUES (:user_id, :login, :ip, :ok, :reason)
        """), {
            "user_id": user_id,
            "login": login or None,
            "ip": ip or None,
            "ok": 1 if ok else 0,
            "reason": reason,
        })


def register_failure(engine: Engine, user: dict[str, Any]) -> None:
    """Считает неудачу и при перерасходе попыток блокирует запись."""
    failed = int(user.get("failed_logins") or 0) + 1
    locked_until = None
    if failed >= MAX_ATTEMPTS:
        minutes = LOCK_MINUTES_HARD if failed >= MAX_ATTEMPTS * 2 else LOCK_MINUTES
        locked_until = _stamp(_now() + timedelta(minutes=minutes))
    with engine.begin() as connection:
        connection.execute(text("""
            UPDATE users SET failed_logins = :failed, locked_until = :locked WHERE id = :id
        """), {"failed": failed, "locked": locked_until, "id": user["id"]})


def register_success(engine: Engine, user_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(text("""
            UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = :id
        """), {"id": user_id})
