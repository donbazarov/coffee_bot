import os
import hmac
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from bot.database.models import engine
from bot.web import stories_service, telegram_login
from bot.web.auth import COOKIE_NAME, SESSION_MAX_AGE, create_calendar_token, create_session, read_calendar_token, read_session, register_or_find_telegram_user, verify_telegram_login
from bot.web.calendar_service import (
    app_timezone,
    apply_schedule_changes,
    build_calendar_feed,
    get_month_calendar,
    get_shift_history,
    get_user_preferences,
    initialize_calendar_schema,
    save_user_preferences,
)
from bot.web.migrations import migrate_legacy_telegram_ids

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
ICONS_DIR = BASE_DIR / "static" / "icons"


@app.exception_handler(stories_service.StoriesError)
async def stories_error_handler(_request: Request, error: stories_service.StoriesError) -> JSONResponse:
    """Истории отвечают форматом {"error": ...} — именно его ждёт клиент гостевой страницы."""
    return JSONResponse({"error": error.message}, status_code=error.status)


@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz():
    """Проверка живости для Docker, systemd и мониторинга.

    Отвечает и на GET, и на HEAD; без авторизации и без обращения к базе —
    подтверждает только то, что жив HTTP-стек.
    """
    return {"ok": True}


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
        user = connection.execute(text("""
            SELECT id,name,role FROM users
            WHERE id=:id AND is_active=1 AND role IN ('barista','senior','mentor')
        """), {"id": session["user_id"]}).mappings().first()
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


@app.on_event("startup")
def migrate_database():
    migrate_legacy_telegram_ids()
    initialize_calendar_schema(engine)
    telegram_login.initialize_login_schema(engine)
    stories_migration = stories_service.migrate_from_prototype()
    if stories_migration["photos"] or stories_migration["removed_key"]:
        logging.getLogger("bot.web").info(
            "Истории гостей: перенесено фото %s, ключ модерации удалён: %s",
            stories_migration["photos"],
            bool(stories_migration["removed_key"]),
        )


@app.get("/api/shift-history")
def shift_history(year: int, month: int, _: dict[str, Any] = Depends(require_user)):
    if year < 2000 or year > 2100 or month < 1 or month > 12:
        raise HTTPException(status_code=422, detail="Некорректный месяц")
    return get_shift_history(engine, year, month)


@app.get("/api/preferences")
def preferences(user: dict[str, Any] = Depends(require_user)):
    return get_user_preferences(engine, user["id"])


@app.patch("/api/preferences")
async def update_preferences(request: Request, user: dict[str, Any] = Depends(require_csrf)):
    payload = await _read_json_object(request)
    allowed = {"quality_enabled", "calendar_enabled"}
    if not payload or set(payload) - allowed or any(not isinstance(value, bool) for value in payload.values()):
        raise HTTPException(status_code=422, detail="Ожидаются настройки модулей типа boolean")
    return save_user_preferences(engine, user["id"], payload)


def _check_csrf(request: Request, csrf_token: str | None) -> None:
    supplied = request.headers.get("X-CSRF-Token")
    if not csrf_token or not supplied or not hmac.compare_digest(csrf_token, supplied):
        raise HTTPException(status_code=403, detail="Недействительный CSRF-токен")


def require_manager_csrf(request: Request, user: dict[str, Any] = Depends(require_manager)) -> dict[str, Any]:
    _, csrf_token = _session_user(request)
    _check_csrf(request, csrf_token)
    return user


async def _read_json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Некорректный JSON") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Ожидался JSON-объект")
    return payload


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user, csrf_token = _session_user(request)

    # Сотрудник вернулся на сайт после подтверждения в Telegram — входим сразу,
    # без повторного нажатия кнопки.
    if user is None:
        entry = telegram_login.get_login_request(engine, request.cookies.get(TG_LOGIN_COOKIE))
        if entry and entry["status"] == telegram_login.STATUS_APPROVED:
            done = _complete_telegram_login(entry)
            if done is not None:
                cookie_value, next_path = done
                response = RedirectResponse(next_path, status_code=303)
                _set_session_cookie(response, cookie_value)
                response.delete_cookie(TG_LOGIN_COOKIE, path="/")
                return response

    bot_username = _bot_username()
    telegram_callback_url = os.getenv("TELEGRAM_AUTH_CALLBACK_URL") or str(request.url_for("telegram_callback"))
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "csrf_token": csrf_token or "",
            "bot_username": bot_username,
            "telegram_callback_url": telegram_callback_url,
            "telegram_login_enabled": bool(bot_username and telegram_callback_url.startswith("https://")),
            # Вход через бота работает и там, где telegram.org недоступен
            "bot_login_enabled": bool(bot_username),
            "auth_error": {
                "verify": "Не удалось подтвердить вход через Telegram. Попробуйте ещё раз.",
                "account": "Ваш Telegram не привязан к активной учётной записи команды.",
                "pending": "Запрос на доступ сохранён. Ожидайте, пока администратор назначит вам роль.",
                "conflict": "Не удалось однозначно сопоставить аккаунт. Обратитесь к администратору.",
                "disabled": "Доступ к аккаунту отключён. Обратитесь к администратору.",
                "setup": "Не задан TELEGRAM_BOT_USERNAME — вход через Telegram недоступен.",
                "unknown": telegram_login.reason_message("unknown"),
                "expired": telegram_login.reason_message("expired"),
            }.get(request.query_params.get("auth_error"), ""),
            "role_names": ROLE_NAMES,
        },
    )


@app.get("/auth/telegram/callback", name="telegram_callback")
def telegram_callback(request: Request):
    payload = dict(request.query_params)
    if not verify_telegram_login(payload):
        return RedirectResponse("/?auth_error=verify", status_code=303)
    user, status = register_or_find_telegram_user(payload)
    if status != "approved" or user is None:
        return RedirectResponse(f"/?auth_error={status}", status_code=303)

    cookie_value, _ = create_session(user["id"])
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        cookie_value,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=os.getenv("WEB_COOKIE_SECURE", "0") == "1",
        samesite="strict",
        path="/",
    )
    return response


# --------------------------------------------------------------------------- #
# Вход через Telegram по ссылке на бота
#
# Нужен для телефона и для регионов, где telegram.org недоступен: виджет
# грузится именно с telegram.org и без него просто не появляется. Здесь
# браузер уходит на t.me/<бот>?start=<токен>, открывается ПРИЛОЖЕНИЕ Telegram,
# сотрудник жмёт «Start», и процесс bot/web/telegram_login_bot.py помечает
# вход подтверждённым. Приложение ходит своим протоколом, поэтому блокировка
# сайта Telegram этому не мешает.
# --------------------------------------------------------------------------- #

TG_LOGIN_COOKIE = "tg_login"


def _bot_username() -> str:
    return os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")


def _cookie_secure() -> bool:
    return os.getenv("WEB_COOKIE_SECURE", "0") == "1"


def _set_session_cookie(response: Response, cookie_value: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        cookie_value,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=_cookie_secure(),
        samesite="strict",
        path="/",
    )


def _complete_telegram_login(entry: dict[str, Any]) -> tuple[str, str] | None:
    """Выдаёт сессию по подтверждённой заявке. Возвращает (cookie, куда вернуться)."""
    user_id = entry.get("user_id")
    if not user_id:
        return None
    cookie_value, _ = create_session(int(user_id))
    telegram_login.consume_login_request(engine, entry["token"])
    return cookie_value, entry.get("next_path") or "/"


@app.get("/auth/telegram/start")
def telegram_start(request: Request, next: str = "/", force: int = 0):
    """Создаёт заявку на вход и отправляет браузер в приложение Telegram."""
    username = _bot_username()
    if not username:
        return RedirectResponse("/?auth_error=setup", status_code=303)

    # Если сотрудник вернулся кнопкой «назад», не отправляем его в Telegram
    # повторно — показываем экран ожидания с опросом статуса.
    existing = telegram_login.get_login_request(engine, request.cookies.get(TG_LOGIN_COOKIE))
    if not force and existing and existing["status"] == telegram_login.STATUS_PENDING:
        return templates.TemplateResponse(
            request=request,
            name="login-wait.html",
            context={"bot_username": username, "next_path": existing["next_path"]},
        )

    token = telegram_login.create_login_request(engine, next)
    response = RedirectResponse(f"https://t.me/{username}?start={token}", status_code=303)
    response.set_cookie(
        TG_LOGIN_COOKIE,
        token,
        max_age=int(telegram_login.LOGIN_REQUEST_TTL.total_seconds()),
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )
    return response


@app.get("/auth/telegram/wait", response_class=HTMLResponse)
def telegram_wait(request: Request):
    """Экран ожидания: сотрудник подтверждает вход в приложении Telegram."""
    entry = telegram_login.get_login_request(engine, request.cookies.get(TG_LOGIN_COOKIE))

    if entry is None:
        return RedirectResponse("/?auth_error=unknown", status_code=303)

    if entry["status"] == telegram_login.STATUS_APPROVED:
        done = _complete_telegram_login(entry)
        if done is not None:
            cookie_value, next_path = done
            response = RedirectResponse(next_path, status_code=303)
            _set_session_cookie(response, cookie_value)
            response.delete_cookie(TG_LOGIN_COOKIE, path="/")
            return response

    if entry["status"] == telegram_login.STATUS_DENIED:
        response = RedirectResponse(f"/?auth_error={entry.get('reason') or 'unknown'}", status_code=303)
        response.delete_cookie(TG_LOGIN_COOKIE, path="/")
        return response

    return templates.TemplateResponse(
        request=request,
        name="login-wait.html",
        context={"bot_username": _bot_username(), "next_path": entry.get("next_path") or "/"},
    )


@app.get("/api/auth/telegram/status")
def telegram_status(request: Request):
    """Опрос статуса заявки со страницы ожидания. Ставит сессию при успехе."""
    entry = telegram_login.get_login_request(engine, request.cookies.get(TG_LOGIN_COOKIE))

    if entry is None:
        return JSONResponse({"status": "unknown", "message": telegram_login.reason_message("unknown")})

    if entry["status"] == telegram_login.STATUS_APPROVED:
        done = _complete_telegram_login(entry)
        if done is None:
            return JSONResponse({"status": "unknown", "message": telegram_login.reason_message("unknown")})
        cookie_value, next_path = done
        response = JSONResponse({"status": "approved", "next": next_path})
        _set_session_cookie(response, cookie_value)
        response.delete_cookie(TG_LOGIN_COOKIE, path="/")
        return response

    if entry["status"] == telegram_login.STATUS_DENIED:
        response = JSONResponse({
            "status": "denied",
            "message": telegram_login.reason_message(entry.get("reason")),
        })
        response.delete_cookie(TG_LOGIN_COOKIE, path="/")
        return response

    return JSONResponse({"status": "pending"})


@app.get("/api/calendar")
def calendar_month(year: int | None = None, month: int | None = None, user: dict[str, Any] = Depends(require_user)):
    today = datetime.now(app_timezone()).date()
    year = year or today.year
    month = month or today.month
    if year < 2000 or year > 2100 or month < 1 or month > 12:
        raise HTTPException(status_code=422, detail="Некорректный месяц")
    return get_month_calendar(engine, user, year, month)


@app.get("/api/calendar/feed-link")
def calendar_feed_link(request: Request, user: dict[str, Any] = Depends(require_user)):
    token = create_calendar_token(user["id"])
    https_url = str(request.url_for("calendar_feed", token=token))
    return {"https_url": https_url, "webcal_url": https_url.replace("https://", "webcal://", 1).replace("http://", "webcal://", 1)}


@app.get("/calendar/feed/{token}.ics", name="calendar_feed")
def calendar_feed(token: str):
    user_id = read_calendar_token(token)
    if user_id is None:
        raise HTTPException(status_code=404, detail="Календарь не найден")
    content = build_calendar_feed(engine, user_id, app_timezone())
    return Response(content, media_type="text/calendar; charset=utf-8", headers={"Cache-Control": "private, no-cache"})


@app.post("/api/shifts/save")
async def save_calendar_changes(request: Request, user: dict[str, Any] = Depends(require_csrf)):
    payload = await _read_json_object(request)
    operations = payload.get("operations")
    if not isinstance(operations, list) or any(not isinstance(item, dict) for item in operations):
        raise HTTPException(status_code=422, detail="Ожидался список ячеек")
    try:
        return apply_schedule_changes(engine, user["id"], operations)
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


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
    role_names = {**ROLE_NAMES, "guest": "Ожидает доступа"}
    return [
        {**dict(user), "role_name": role_names.get(user["role"], user["role"]), "access_pending": user["role"] == "guest"}
        for user in users
    ]


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
        target_role = values.get("role", current_user["role"])
        target_active = values.get("is_active", current_user["is_active"])
        if target_role == "guest" and target_active:
            raise HTTPException(status_code=400, detail="Назначьте роль сотрудника до выдачи доступа")
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


# --------------------------------------------------------------------------- #
# Истории гостей
#
# Публичная страница живёт по прямой ссылке /stories. В интерфейсе сотрудников
# на неё нет ни одной ссылки: страница для гостей, а не для команды.
# Модерация перенесена из отдельного прототипа в личный кабинет наставника и
# работает по обычной сессии сайта — приватный ключ Neft_moderation_key больше
# не нужен.
# --------------------------------------------------------------------------- #

def _guest_token(request: Request) -> str:
    return request.cookies.get(stories_service.GUEST_COOKIE) or ""


@app.get("/stories", response_class=HTMLResponse)
def stories_page(request: Request):
    return templates.TemplateResponse(request=request, name="stories.html", context={})


@app.get("/stories/api/stories")
def stories_public_list():
    """Только опубликованные истории — для карусели."""
    return {"stories": stories_service.list_published()}


@app.post("/stories/api/stories", status_code=201)
async def stories_create(request: Request):
    """Приём новой истории. Публикуется только после модерации."""
    body = await _read_json_object(request)
    story, token = stories_service.create_story(body, _guest_token(request))
    response = JSONResponse({"ok": True, "id": story["id"]}, status_code=201)
    if token != _guest_token(request):
        # Кука «памяти гостя»: одно устройство — одна история.
        response.set_cookie(
            stories_service.GUEST_COOKIE,
            token,
            max_age=stories_service.GUEST_COOKIE_MAX_AGE,
            path=stories_service.GUEST_COOKIE_PATH,
            httponly=True,
            secure=os.getenv("WEB_COOKIE_SECURE", "0") == "1",
            samesite="lax",
        )
    return response


@app.get("/stories/api/me")
def stories_me(request: Request):
    """История текущего устройства, чтобы предложить её изменить."""
    story = stories_service.find_guest_story(_guest_token(request))
    return {
        "hasStory": story is not None,
        "story": stories_service.author_story(story) if story else None,
    }


@app.patch("/stories/api/stories/mine")
async def stories_update_mine(request: Request):
    """Правка своей истории. После изменения она снова уходит на модерацию."""
    body = await _read_json_object(request)
    return {"ok": True, "story": stories_service.update_my_story(body, _guest_token(request))}


@app.get("/stories/uploads/{filename}")
def stories_upload(filename: str):
    found = stories_service.read_upload(filename)
    if found is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    path, media_type = found
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/stories/moderation")
def stories_moderation_list(_: dict[str, Any] = Depends(require_manager)):
    """Все истории для панели модерации в личном кабинете."""
    return {"stories": stories_service.list_all(), "stats": stories_service.stats()}


@app.patch("/api/stories/moderation/{story_id}")
async def stories_moderation_update(story_id: str, request: Request, _: dict[str, Any] = Depends(require_manager_csrf)):
    body = await _read_json_object(request)
    return {
        "ok": True,
        "story": stories_service.update_moderation_story(story_id, body),
        "stats": stories_service.stats(),
    }


@app.delete("/api/stories/moderation/{story_id}")
def stories_moderation_delete(story_id: str, _: dict[str, Any] = Depends(require_manager_csrf)):
    if not stories_service.delete_story(story_id):
        raise HTTPException(status_code=404, detail="История не найдена")
    return {"ok": True, "stats": stories_service.stats()}


# --- иконки и манифест: браузеры запрашивают их из корня сайта --------------- #

@app.get("/site.webmanifest")
def web_manifest():
    return FileResponse(ICONS_DIR / "site.webmanifest", media_type="application/manifest+json")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(ICONS_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    return FileResponse(ICONS_DIR / "apple-touch-icon.png", media_type="image/png")


__all__ = ["app"]
