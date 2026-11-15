"""Общие зависимости доступа: сессия, права и CSRF.

Вынесены из `bot/web/app.py`, чтобы их могли использовать и другие роутеры
(например, `bot/web/chat.py`) без циклического импорта.

Права больше не перечисляются здесь строкой: уровень доступа сотрудника берётся
из справочника `role_access_config`, а порог права — из `PERMISSIONS`
(`bot/web/access_roles.py`). Роут объявляет право, а не должность:

    @app.patch("/api/roles/{role_key}")
    async def update_role(..., _: dict = Depends(require_permission_csrf("admin"))):
"""

from __future__ import annotations

import hmac
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text

from bot.database.models import engine
from bot.web import access_roles
from bot.web.auth import COOKIE_NAME, read_session


def session_user(request: Request) -> tuple[dict[str, Any] | None, str | None]:
    """Возвращает (пользователь, CSRF-токен) по cookie сессии, иначе (None, None).

    Сессия живёт, пока запись сотрудника активна: должность вне справочника или
    отключённая даёт уровень 0 (права fail-closed), но не выбрасывает человека из
    системы — иначе нерабочий справочник разлогинивал бы всех. Уровень и название
    должности читаются тем же запросом, поэтому гейты сравнивают числа без
    дополнительных обращений к базе.
    """
    session = read_session(request.cookies.get(COOKIE_NAME))
    if not session:
        return None, None
    with engine.connect() as connection:
        user = connection.execute(text("""
            SELECT u.id, u.name, u.display_name, u.role, u.avatar_rev, u.telegram_id,
                   u.access_code, u.session_epoch,
                   -- Уровень даёт только активная должность: отключённая = 0.
                   COALESCE(CASE WHEN r.is_active = 1 THEN r.access_level END, 0) AS access_level,
                   COALESCE(r.title, u.role) AS role_title
            FROM users u
            LEFT JOIN role_access_config r ON r.role_key = u.role
            WHERE u.id = :id AND u.is_active = 1
        """), {"id": session["user_id"]}).mappings().first()
    if not user:
        return None, None
    # Ротация кода супер-пользователем и «выйти на всех устройствах» поднимают
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


def require_csrf(request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    _, csrf_token = session_user(request)
    check_csrf(request, csrf_token)
    return user


def require_permission(permission: str) -> Callable[..., dict[str, Any]]:
    """Зависимость «право уровня доступа»: порог берётся из `PERMISSIONS`.

    Новое право не требует новых зависимостей: достаточно добавить строку в
    `access_roles.PERMISSIONS` и объявить её в роуте.
    """
    access_roles.min_level(permission)  # опечатка в ключе — ошибка на старте, а не в проде

    def dependency(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        if not access_roles.allows(user, permission):
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        return user

    dependency.__name__ = f"require_{permission}"
    return dependency


def require_permission_csrf(permission: str) -> Callable[..., dict[str, Any]]:
    """То же право плюс проверка CSRF — для изменяющих запросов."""
    check = require_permission(permission)

    def dependency(request: Request, user: dict[str, Any] = Depends(check)) -> dict[str, Any]:
        _, csrf_token = session_user(request)
        check_csrf(request, csrf_token)
        return user

    dependency.__name__ = f"require_{permission}_csrf"
    return dependency





