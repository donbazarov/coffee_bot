"""Общие зависимости доступа: сессия, роли и CSRF.

Вынесены из `bot/web/app.py`, чтобы их могли использовать и другие роутеры
(например, `bot/web/chat.py`) без циклического импорта. Логика не менялась —
это тот же код, что раньше жил прямо в `app.py`.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text

from bot.database.models import engine
from bot.web.auth import COOKIE_NAME, read_session


def session_user(request: Request) -> tuple[dict[str, Any] | None, str | None]:
    """Возвращает (пользователь, CSRF-токен) по cookie сессии, иначе (None, None)."""
    session = read_session(request.cookies.get(COOKIE_NAME))
    if not session:
        return None, None
    with engine.connect() as connection:
        user = connection.execute(text("""
            SELECT id,name,display_name,role,avatar_rev,telegram_id,access_code,session_epoch FROM users
            WHERE id=:id AND is_active=1 AND role IN ('barista','senior','mentor')
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
    if user["role"] not in {"senior", "mentor"}:
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
