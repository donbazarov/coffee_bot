import os
import hmac
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from bot.database.models import engine
from bot.web.auth import COOKIE_NAME, SESSION_MAX_AGE, create_session, find_active_user, read_session, verify_telegram_login

BASE_DIR = Path(__file__).resolve().parent
ROLE_VALUES = {"barista", "senior", "mentor"}
ROLE_NAMES = {"barista": "Бариста", "senior": "Старший", "mentor": "Наставник"}
SCORE_SQL = """
    CASE
        WHEN category = 'Эспрессо/Фильтр' AND balance IS NOT NULL AND bouquet IS NOT NULL
             AND body IS NOT NULL AND aftertaste IS NOT NULL
            THEN (balance + bouquet + body + aftertaste) / 4.0
        WHEN category = 'Молочный напиток' AND balance IS NOT NULL AND bouquet IS NOT NULL
             AND foam IS NOT NULL AND latte_art IS NOT NULL
            THEN (balance + bouquet + foam + latte_art) / 4.0
    END
"""

app = FastAPI(title="Coffee Quality", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://telegram.org; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https://t.me https://*.telegram.org; connect-src 'self'; "
        "frame-src https://oauth.telegram.org; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return response


def _session_user(request: Request) -> tuple[dict[str, Any] | None, str | None]:
    session = read_session(request.cookies.get(COOKIE_NAME))
    if not session:
        return None, None
    with engine.connect() as connection:
        user = connection.execute(
            text("SELECT id, name, role FROM users WHERE id = :id AND is_active = 1"),
            {"id": session["user_id"]},
        ).mappings().first()
    return (dict(user), session["csrf_token"]) if user else (None, None)


def require_user(request: Request) -> dict[str, Any]:
    user, _ = _session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    return user


def require_manager(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if user["role"] not in {"senior", "mentor"}:
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user


def require_csrf(request: Request, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    _, csrf_token = _session_user(request)
    _check_csrf(request, csrf_token)
    return user


def _check_csrf(request: Request, csrf_token: str | None) -> None:
    supplied = request.headers.get("X-CSRF-Token")
    if not csrf_token or not supplied or not hmac.compare_digest(csrf_token, supplied):
        raise HTTPException(status_code=403, detail="Недействительный CSRF-токен")


def require_manager_csrf(request: Request, user: dict[str, Any] = Depends(require_manager)) -> dict[str, Any]:
    _, csrf_token = _session_user(request)
    _check_csrf(request, csrf_token)
    return user


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user, csrf_token = _session_user(request)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "csrf_token": csrf_token or "",
            "bot_username": os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@"),
            "role_names": ROLE_NAMES,
        },
    )


@app.post("/api/auth/telegram")
async def telegram_login(request: Request, response: Response):
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Некорректные данные Telegram") from error
    if not isinstance(payload, dict) or not verify_telegram_login(payload):
        raise HTTPException(status_code=401, detail="Не удалось проверить вход через Telegram")

    user = find_active_user(payload)
    if not user:
        raise HTTPException(status_code=403, detail="Аккаунт не найден или деактивирован")

    cookie_value, _ = create_session(user["id"])
    response.set_cookie(
        COOKIE_NAME,
        cookie_value,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=os.getenv("WEB_COOKIE_SECURE", "0") == "1",
        samesite="strict",
        path="/",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response, _: dict[str, Any] = Depends(require_csrf)):
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
    return {"ok": True}


@app.get("/api/dashboard")
def dashboard(period: str = "30d", _: dict[str, Any] = Depends(require_user)):
    periods = {"7d": "-6 days", "30d": "-29 days", "90d": "-89 days", "all": None}
    if period not in periods:
        raise HTTPException(status_code=422, detail="Неизвестный период")

    interval = periods[period]
    where_sql = "" if interval is None else "WHERE date(created_at) >= date('now', :interval)"
    params = {} if interval is None else {"interval": interval}

    with engine.connect() as connection:
        summary = connection.execute(
            text(f"SELECT COUNT(*) AS count, ROUND(AVG({SCORE_SQL}), 2) AS average FROM drink_reviews {where_sql}"),
            params,
        ).mappings().one()
        active_users = connection.execute(text("SELECT COUNT(*) FROM users WHERE is_active = 1")).scalar_one()
        leaderboard = connection.execute(
            text(f"""
                SELECT barista_name AS name, COUNT(*) AS count, ROUND(AVG({SCORE_SQL}), 2) AS average
                FROM drink_reviews {where_sql}
                GROUP BY barista_name ORDER BY average DESC, count DESC LIMIT 8
            """),
            params,
        ).mappings().all()
        recent = connection.execute(
            text(f"""
                SELECT id, barista_name AS barista, respondent_name AS author, point, category,
                       drink_type, comment, created_at, ROUND(({SCORE_SQL}), 1) AS score
                FROM drink_reviews {where_sql}
                ORDER BY created_at DESC LIMIT 8
            """),
            params,
        ).mappings().all()
        daily_rows = connection.execute(
            text(f"""
                SELECT date(created_at) AS day, COUNT(*) AS count, ROUND(AVG({SCORE_SQL}), 2) AS average
                FROM drink_reviews WHERE date(created_at) >= date('now', '-6 days')
                GROUP BY date(created_at) ORDER BY day
            """)
        ).mappings().all()

    by_day = {row["day"]: dict(row) for row in daily_rows}
    today = date.today()
    daily = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        row = by_day.get(day.isoformat(), {})
        daily.append({"day": day.strftime("%d.%m"), "count": row.get("count", 0), "average": row.get("average")})

    return {
        "period": period,
        "review_count": summary["count"],
        "average": summary["average"],
        "active_users": active_users,
        "leaderboard": [dict(row) for row in leaderboard],
        "recent": [dict(row) for row in recent],
        "daily": daily,
    }


@app.get("/api/users")
def list_users(_: dict[str, Any] = Depends(require_manager)):
    with engine.connect() as connection:
        users = connection.execute(text("""
            SELECT id, name, iiko_id, telegram_username, role, is_active, telegram_id
            FROM users ORDER BY is_active DESC, name COLLATE NOCASE
        """)).mappings().all()
    return [{**dict(user), "role_name": ROLE_NAMES.get(user["role"], user["role"])} for user in users]


def _clean_username(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    return str(value).strip().lstrip("@").lower()


def _validate_user_payload(payload: Any, partial: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Ожидался объект пользователя")
    allowed = {"name", "iiko_id", "telegram_username", "role", "is_active"}
    if set(payload) - allowed:
        raise HTTPException(status_code=422, detail="Переданы неизвестные поля")
    values = dict(payload)
    if not partial or "name" in values:
        name = str(values.get("name", "")).strip()
        if not name or len(name) > 100:
            raise HTTPException(status_code=422, detail="Имя должно содержать от 1 до 100 символов")
        values["name"] = name
    if not partial or "role" in values:
        if values.get("role") not in ROLE_VALUES:
            raise HTTPException(status_code=422, detail="Неизвестная роль")
    if "iiko_id" in values:
        try:
            values["iiko_id"] = int(values["iiko_id"]) if values["iiko_id"] not in (None, "") else None
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="Iiko ID должен быть числом") from error
    if "telegram_username" in values:
        values["telegram_username"] = _clean_username(values["telegram_username"])
    if "is_active" in values:
        if not isinstance(values["is_active"], bool):
            raise HTTPException(status_code=422, detail="Статус активности должен быть булевым")
        values["is_active"] = int(values["is_active"])
    return values


@app.post("/api/users")
async def create_user(request: Request, manager: dict[str, Any] = Depends(require_manager_csrf)):
    values = _validate_user_payload(await request.json())
    values.setdefault("iiko_id", None)
    values.setdefault("telegram_username", None)
    with engine.begin() as connection:
        try:
            connection.execute(
                text("""
                    INSERT INTO users (name, iiko_id, telegram_username, role, is_active)
                    VALUES (:name, :iiko_id, :telegram_username, :role, 1)
                """),
                values,
            )
        except IntegrityError as error:
            raise HTTPException(status_code=409, detail="Iiko ID или Telegram username уже используется") from error
    return {"ok": True}


@app.patch("/api/users/{user_id}")
async def update_user(user_id: int, request: Request, manager: dict[str, Any] = Depends(require_manager_csrf)):
    values = _validate_user_payload(await request.json(), partial=True)
    if not values:
        raise HTTPException(status_code=422, detail="Нет полей для обновления")
    assignments = ", ".join(f"{key} = :{key}" for key in values)
    values["user_id"] = user_id
    with engine.begin() as connection:
        current_user = connection.execute(
            text("SELECT role, is_active FROM users WHERE id = :user_id"), {"user_id": user_id}
        ).mappings().first()
        if not current_user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        will_remove_manager = (
            current_user["role"] in {"senior", "mentor"}
            and current_user["is_active"]
            and (values.get("role", current_user["role"]) == "barista" or values.get("is_active", 1) == 0)
        )
        if will_remove_manager:
            other_managers = connection.execute(text("""
                SELECT COUNT(*) FROM users
                WHERE id != :user_id AND is_active = 1 AND role IN ('senior', 'mentor')
            """), {"user_id": user_id}).scalar_one()
            if not other_managers:
                raise HTTPException(status_code=400, detail="Нельзя убрать последнего активного менеджера")
        try:
            result = connection.execute(text(f"UPDATE users SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = :user_id"), values)
        except IntegrityError as error:
            raise HTTPException(status_code=409, detail="Iiko ID или Telegram username уже используется") from error
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
    return {"ok": True}