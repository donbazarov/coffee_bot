"""Общие зависимости доступа: сессия, уровни допуска и CSRF.

Вынесены из `bot/web/app.py`, чтобы их могли использовать и другие роутеры
(например, `bot/web/chat.py`) без циклического импорта. Логика не менялась —
это тот же код, что раньше жил прямо в `app.py`.

Должности больше не перечисляются здесь строкой: состав сотрудников и уровень
доступа приходят из справочника `role_access_config` (`bot/web/access_roles.py`).
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text

from bot.database.models import engine
from bot.web import access_roles
from bot.web.auth import COOKIE_NAME, read_session


def session_user(request: Request) -> tuple[dict[str, Any] | None, str | None]:
    """Возвращает (пользователь, CSRF-токен) по cookie сессии, иначе (None, None).

    Роль сотрудника — активная должность справочника; уровень доступа и название
    должности читаются тем же запросом, поэтому гейты сравнивают числа без
    дополнительных обращений к базе. Заявка на доступ (`guest`) в справочник не
    входит, поэтому неавторизованный пользователь сессии не получает.
    """
    session = read_session(request.cookies.get(COOKIE_NAME))
    if not session:
        return None, None
    with engine.connect() as connection:
        user = connection.execute(text("""
            SELECT u.id, u.name, u.display_name, u.role, u.avatar_rev, u.telegram_id,
                   u.access_code, u.session_epoch,
                   COALESCE(r.access_level, 0) AS access_level,
                   COALESCE(r.title, u.role) AS role_title
            FROM users u
            LEFT JOIN role_access_config r ON r.role_key = u.role
            WHERE u.id = :id AND u.is_active = 1
              AND u.role IN (SELECT role_key FROM role_access_config WHERE is_active = 1)
        """), {"id": session["user_id"]}).mappings().first()
    if not user:
        return None, None
    # Ротация кода наставником и «выйти на всех устройствах» поднимают
    # session_epoch — выданные ранее куки после этого недействительны.
    if int(user["session_epoch"] or 0) != int(session["epoch"] or 0):
        return None, None
    return dict(user), session["csrf_token"]


def check_csrf(request: Request, csrf_token: str | None) -> None:
    supplied = request.headers.get("X-CSRF-Token")
    if not csrf_token or not supplied or not hmac.compare_digest(csrf_token, supplied):
        raise HTTPException(status_code=403, detail="Недействительный CSRF-токен")


def require_user(request: Request) -> dict[str, Any]:
    user, _ = session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    return user


def require_manager(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Управленческий доступ: порог уровня и возможность `manage` — из справочника."""
    if not access_roles.is_manager(user):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user


def require_csrf(request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    _, csrf_token = session_user(request)
    check_csrf(request, csrf_token)
    return user


def require_manager_csrf(request: Request, user: dict[str, Any] = Depends(require_manager)) -> dict[str, Any]:
    _, csrf_token = session_user(request)
    check_csrf(request, csrf_token)
    return user
