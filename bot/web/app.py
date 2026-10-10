import os
import hmac
import hashlib
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from bot.config import BotConfig
from bot.database.models import engine, init_db
from bot.web import access_code, announcements, avatars, schedule_snapshot, stories_service, telegram_login, telegram_publish
from bot.web.auth import COOKIE_NAME, SESSION_MAX_AGE, SESSION_RENEW_AFTER, create_calendar_token, create_session, read_calendar_token, read_session, register_or_find_telegram_user, verify_telegram_login
from bot.web.calendar_service import (
    app_timezone,
    apply_schedule_changes,
    build_calendar_feed,
    create_shift_template,
    delete_shift_template,
    get_app_settings,
    get_month_calendar,
    get_shift_history,
    get_user_preferences,
    initialize_calendar_schema,
    list_shift_templates,
    reorder_shift_templates,
    save_app_settings,
    save_user_preferences,
    THEME_VALUES,
    update_shift_template,
)
from bot.web.migrations import migrate_legacy_telegram_ids

BASE_DIR = Path(__file__).resolve().parent
ROLE_VALUES = {"barista", "senior", "mentor"}
ROLE_NAMES = {"barista": "Бариста", "senior": "Старший", "mentor": "Наставник"}

# Тексты ошибок входа. Формулировки для «нет такого сотрудника» и «неверный код»
# намеренно разные только в одном: без кода нельзя понять, существует ли запись,
# но подсказка «уточните у наставника» нужна в любом случае.
LOGIN_ERRORS = {
    "empty": "Введите Iiko ID и код доступа.",
    "unknown": "Не нашли такой Iiko ID или username — уточните у наставника.",
    "code": "Неверный код доступа.",
    "locked": "Слишком много попыток. Попробуйте позже или попросите наставника обновить код.",
    "inactive": "Доступ к аккаунту отключён. Обратитесь к наставнику.",
    "nocode": "Для вашего аккаунта код ещё не выдан. Обратитесь к наставнику.",
}
ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_AVATAR_BYTES = 4 * 1024 * 1024
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


def _static_version() -> str:
    """Отпечаток статики: меняется при правке css/js и сбрасывает кеш браузера.

    Без этого статика без Cache-Control кешируется браузером эвристически,
    и после деплоя новый HTML работает со старыми app.css/app.js.
    """
    digest = hashlib.sha1()
    for name in ("fonts.css", "theme-boot.js", "app.css", "app.js", "sw.js", "login-wait.js", "stories/app.js", "stories/styles.css"):
        try:
            stat = (BASE_DIR / "static" / name).stat()
        except OSError:
            continue
        digest.update(f"{name}:{stat.st_mtime_ns}:{stat.st_size}".encode())
    return digest.hexdigest()[:12]


# Пересчитывается при рестарте сервиса — деплой меняет mtime файлов.
STATIC_VERSION = _static_version()
templates.env.globals["static_version"] = STATIC_VERSION


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
        "style-src 'self' 'unsafe-inline'; font-src 'self'; "
        "img-src 'self' data: https://t.me https://*.telegram.org; connect-src 'self'; "
        "frame-src https://oauth.telegram.org; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    content_type = response.headers.get("content-type", "")
    if request.url.path.startswith("/static/"):
        # Версионированные ссылки (?v=<hash>) кешируем надолго: при рестарте
        # контейнера (деплой) страница не остаётся без стилей — браузер берёт
        # CSS/JS из кеша. Без версии — сутки (это logo.svg и шрифты из CSS).
        if request.query_params.get("v"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "public, max-age=86400"
    elif content_type.startswith("text/html"):
        # HTML перепроверяем всегда — иначе после деплоя браузер держит старую разметку.
        response.headers["Cache-Control"] = "no-cache"
    return response


def _session_user(request: Request) -> tuple[dict[str, Any] | None, str | None]:
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
    # Сначала создаём недостающие таблицы. Приложение обязано подниматься на
    # пустой базе: без этого шага старт падал с "no such table: users", потому
    # что миграции читают users раньше, чем схема вообще создана.
    init_db()
    migrate_legacy_telegram_ids()
    initialize_calendar_schema(engine)
    access_code.initialize_access_schema(engine)
    announcements.initialize_schema(engine)
    # Коды активным сотрудникам выдаются сразу: раскатка входа по коду не требует
    # ручной работы наставника по каждому человеку.
    access_code.ensure_active_codes(engine)
    telegram_login.initialize_login_schema(engine)
    # Раз в месяц (т.е. при ближайшем рестарте после рубежа) чистим старые снимки.
    schedule_snapshot.prune_snapshots(engine)
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
    allowed = {"quality_enabled", "calendar_enabled", "theme", "accent"}
    if not payload or set(payload) - allowed:
        raise HTTPException(status_code=422, detail="Неизвестные настройки")
    for key, value in payload.items():
        if key in {"quality_enabled", "calendar_enabled"} and not isinstance(value, bool):
            raise HTTPException(status_code=422, detail="Настройки модулей должны быть boolean")
        if key == "theme" and value not in THEME_VALUES:
            raise HTTPException(status_code=422, detail="Неизвестная тема оформления")
        if key == "accent" and not (isinstance(value, str) and ACCENT_RE.match(value)):
            raise HTTPException(status_code=422, detail="Акцент должен быть цветом вида #rrggbb")
    return save_user_preferences(engine, user["id"], payload)


@app.patch("/api/profile")
async def update_profile(request: Request, user: dict[str, Any] = Depends(require_csrf)):
    """Отображаемое имя — видно всем (график, интерфейс, ICS)."""
    payload = await _read_json_object(request)
    if set(payload) - {"display_name"}:
        raise HTTPException(status_code=422, detail="Неизвестные поля профиля")
    display_name = str(payload.get("display_name") or "").strip()
    if len(display_name) > 100:
        raise HTTPException(status_code=422, detail="Имя должно быть не длиннее 100 символов")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE users SET display_name = :name, updated_at = CURRENT_TIMESTAMP WHERE id = :id"),
            {"name": display_name or None, "id": user["id"]},
        )
    return {"ok": True, "display_name": display_name}


@app.get("/avatars/{user_id}.jpg")
def user_avatar(user_id: int, _: dict[str, Any] = Depends(require_user)):
    try:
        path = avatars.avatar_path(user_id)
    except OSError as error:
        raise HTTPException(status_code=404, detail="Аватара нет") from error
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Аватара нет")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/profile/avatar")
async def upload_avatar(request: Request, user: dict[str, Any] = Depends(require_csrf)):
    """Тело запроса — сам файл изображения (без multipart, чтобы не тянуть зависимость)."""
    data = await request.body()
    if not data or len(data) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="Файл пустой или больше 4 МБ")
    if not avatars.store_avatar(user["id"], data):
        raise HTTPException(status_code=422, detail="Не удалось прочитать изображение")
    with engine.begin() as connection:
        connection.execute(text("UPDATE users SET avatar_rev = avatar_rev + 1 WHERE id = :id"), {"id": user["id"]})
    return {"ok": True}


@app.delete("/api/profile/avatar")
def reset_avatar(user: dict[str, Any] = Depends(require_csrf)):
    avatars.delete_avatar(user["id"])
    with engine.begin() as connection:
        connection.execute(text("UPDATE users SET avatar_rev = 0 WHERE id = :id"), {"id": user["id"]})
    return {"ok": True}


@app.post("/api/profile/code")
def rotate_own_code(user: dict[str, Any] = Depends(require_csrf)) -> dict[str, Any]:
    """Сотрудник меняет свой код. Устройства не выкидываем — это его же техника."""
    return {"ok": True, "code": access_code.set_access_code(engine, user["id"])}


@app.post("/api/session/logout-all")
def logout_all(request: Request, user: dict[str, Any] = Depends(require_csrf)) -> JSONResponse:
    """«Выйти на всех устройствах»: поднимаем session_epoch, текущему выдаём новую куку."""
    _, csrf_token = _session_user(request)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE users SET session_epoch = session_epoch + 1 WHERE id = :id"),
            {"id": user["id"]},
        )
        epoch = connection.execute(
            text("SELECT session_epoch FROM users WHERE id = :id"), {"id": user["id"]}
        ).scalar_one()
    cookie_value, _ = create_session(int(user["id"]), int(epoch), csrf_token=csrf_token)
    response = JSONResponse({"ok": True})
    _set_session_cookie(response, cookie_value)
    return response


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
    tg_pending = False

    # Сотрудник вернулся на сайт после подтверждения в Telegram — входим сразу,
    # без повторного нажатия кнопки. При выключенном Telegram-входе заявки не
    # разбираем вообще: подтверждать их некому, бот не работает.
    if user is None and BotConfig.telegram_login_enabled:
        entry = telegram_login.get_login_request(engine, request.cookies.get(TG_LOGIN_COOKIE))
        if entry and entry["status"] == telegram_login.STATUS_APPROVED:
            done = _complete_telegram_login(entry)
            if done is not None:
                cookie_value, next_path = done
                response = RedirectResponse(next_path, status_code=303)
                _set_session_cookie(response, cookie_value)
                response.delete_cookie(TG_LOGIN_COOKIE, path="/")
                return response
        # Заявка ещё не подтверждена: страница входа сама опросит статус и
        # обновит себя, чтобы не приходилось жать «Обновить» вручную.
        tg_pending = bool(entry and entry["status"] == telegram_login.STATUS_PENDING)

    if user and BotConfig.telegram_outbound_enabled and user.get("telegram_id") and not user.get("avatar_rev"):
        # Фото из Telegram скачиваем в фоне — страница не ждёт сеть. Ошибки
        # внутри потока гасятся там же: запрос из-за этого падать не должен.
        try:
            threading.Thread(
                target=avatars.ensure_telegram_avatar,
                args=(engine, user["id"], user["telegram_id"]),
                daemon=True,
            ).start()
        except RuntimeError:
            pass

    bot_username = _bot_username()
    telegram_callback_url = os.getenv("TELEGRAM_AUTH_CALLBACK_URL") or str(request.url_for("telegram_callback"))
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "tg_pending": tg_pending,
            "login_error": LOGIN_ERRORS.get(request.query_params.get("login_error"), ""),
            "csrf_token": csrf_token or "",
            "bot_username": bot_username,
            "telegram_callback_url": telegram_callback_url,
            # Оба способа входа через Telegram гасит один флаг AUTH_TELEGRAM_ENABLED=0:
            # виджет грузится с telegram.org, а бот не может дотянуться до api.telegram.org.
            "telegram_login_enabled": (
                BotConfig.telegram_login_enabled
                and bool(bot_username and telegram_callback_url.startswith("https://"))
            ),
            "bot_login_enabled": BotConfig.telegram_login_enabled and bool(bot_username),
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
    # Скользящее продление: устройство, которым пользуются, не выходит никогда.
    # CSRF берём прежний — отрисованная страница уже содержит его.
    if user and csrf_token:
        session = read_session(request.cookies.get(COOKIE_NAME))
        if session and session["expires_at"] - int(time.time()) < SESSION_MAX_AGE - SESSION_RENEW_AFTER:
            cookie_value, _ = create_session(
                int(user["id"]), int(user.get("session_epoch") or 0), csrf_token=csrf_token
            )
            _set_session_cookie(response, cookie_value)
    return response


@app.post("/auth/login")
async def login(request: Request):
    """Вход по «iiko_id (или @username) + код доступа». Обычная форма, без JS."""
    # Тело разбираем сами: `request.form()` требует python-multipart, а проект
    # держит зависимости минимальными (тот же приём, что в загрузке аватара).
    body = (await request.body()).decode("utf-8", "replace")
    fields = dict(parse_qsl(body, keep_blank_values=True))
    raw_login = str(fields.get("login") or "")
    raw_code = str(fields.get("code") or "")
    client_ip = request.client.host if request.client else ""
    known_login = access_code.normalize_login(raw_login)

    def deny(reason: str):
        return RedirectResponse(f"/?login_error={reason}", status_code=303)

    if not known_login or not raw_code.strip():
        return deny("empty")

    user = access_code.find_user_by_login(engine, raw_login)
    if user is None:
        access_code.record_attempt(engine, None, known_login, client_ip, False, "unknown")
        return deny("unknown")

    if access_code.throttle_reason(engine, user, client_ip):
        access_code.record_attempt(engine, int(user["id"]), known_login, client_ip, False, "locked")
        return deny("locked")

    if not access_code.is_staff(user) or not user.get("access_code"):
        reason = "nocode" if access_code.is_staff(user) else "inactive"
        access_code.record_attempt(engine, int(user["id"]), known_login, client_ip, False, reason)
        return deny(reason)

    if not access_code.code_matches(user, raw_code):
        access_code.register_failure(engine, user)
        access_code.record_attempt(engine, int(user["id"]), known_login, client_ip, False, "code")
        return deny("code")

    access_code.register_success(engine, int(user["id"]))
    access_code.record_attempt(engine, int(user["id"]), known_login, client_ip, True, None)
    cookie_value, _ = create_session(int(user["id"]), int(user.get("session_epoch") or 0))
    response = RedirectResponse("/", status_code=303)
    _set_session_cookie(response, cookie_value)
    return response


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
    if not BotConfig.telegram_login_enabled:
        return RedirectResponse("/", status_code=303)

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


# --------------------------------------------------------------------------- #
# Снимки графика («Расписание») и личные шаблоны смен
# --------------------------------------------------------------------------- #

@app.get("/calendar/snapshots/{filename}")
def calendar_snapshot(filename: str, _: dict[str, Any] = Depends(require_user)):
    """Снимок графика доступен любому авторизованному сотруднику (для истории правок)."""
    found = schedule_snapshot.read_snapshot(filename)
    if found is None:
        raise HTTPException(status_code=404, detail="Снимок не найден")
    return FileResponse(found, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/shift-templates")
def shift_templates(_: dict[str, Any] = Depends(require_user)):
    return list_shift_templates(engine)


@app.post("/api/shift-templates")
async def create_shift_template_route(request: Request, user: dict[str, Any] = Depends(require_manager_csrf)):
    payload = await _read_json_object(request)
    try:
        return create_shift_template(engine, user["id"], payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.patch("/api/shift-templates/{template_id}")
async def update_shift_template_route(template_id: int, request: Request, _: dict[str, Any] = Depends(require_manager_csrf)):
    payload = await _read_json_object(request)
    try:
        return update_shift_template(engine, template_id, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.delete("/api/shift-templates/{template_id}")
def delete_shift_template_route(template_id: int, _: dict[str, Any] = Depends(require_manager_csrf)):
    if not delete_shift_template(engine, template_id):
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    return {"ok": True}


@app.post("/api/shift-templates/reorder")
async def reorder_shift_templates_route(request: Request, _: dict[str, Any] = Depends(require_manager_csrf)):
    payload = await _read_json_object(request)
    try:
        return reorder_shift_templates(engine, payload.get("order"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/app-settings")
def app_settings(_: dict[str, Any] = Depends(require_manager)):
    """Каналы Telegram для публикаций — видит и меняет только наставник/старший."""
    return get_app_settings(engine)


@app.patch("/api/app-settings")
async def update_app_settings(request: Request, _: dict[str, Any] = Depends(require_manager_csrf)):
    payload = await _read_json_object(request)
    try:
        return save_app_settings(engine, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/shifts/save")
async def save_calendar_changes(request: Request, user: dict[str, Any] = Depends(require_csrf)):
    payload = await _read_json_object(request)
    operations = payload.get("operations")
    if not isinstance(operations, list) or any(not isinstance(item, dict) for item in operations):
        raise HTTPException(status_code=422, detail="Ожидался список ячеек")
    change_type = payload.get("change_type", "swap")
    if change_type not in {"swap", "schedule"}:
        raise HTTPException(status_code=422, detail="Неизвестный тип правки")
    # Тип «Расписание» — только наставник. Проверка на сервере, не только в UI.
    if change_type == "schedule" and user["role"] != "mentor":
        raise HTTPException(status_code=403, detail="Тип «Расписание» доступен только наставникам")
    # publish=False — тихое сохранение расписания: без лога, без снимка, без рассылки.
    publish = payload.get("publish", True)
    if not isinstance(publish, bool):
        raise HTTPException(status_code=422, detail="Флаг публикации должен быть булевым")
    # Диапазон скриншотов «Расписания»: без него снимки строятся по изменённым дням.
    publish_start = payload.get("publish_start")
    publish_end = payload.get("publish_end")
    for label, value in (("начала", publish_start), ("окончания", publish_end)):
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"Дата {label} диапазона должна быть строкой")
    try:
        # apply_schedule_changes синхронная и с генерацией картинки — уводим из event loop.
        result = await run_in_threadpool(apply_schedule_changes, engine, user["id"], operations,
                                         change_type, publish, publish_start, publish_end)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    # Уведомления — после успешного сохранения, отдельно от транзакции.
    # Telegram (выключен флагом и недоступен с этого сервера) плюс лента анонсов,
    # которая заменяет каналы: снимки графика и строки замен видны в приложении.
    if result.get("swap_lines") or result.get("published"):
        await run_in_threadpool(telegram_publish.publish_schedule_events, engine, result, user["name"])
        await run_in_threadpool(
            announcements.publish_schedule_events, engine, result, user["name"], user["id"]
        )
    return result


@app.post("/api/logout")
def logout(response: Response, _: dict[str, Any] = Depends(require_csrf)):
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Лента анонсов: расписание, замены, события и общая информация
# --------------------------------------------------------------------------- #

@app.get("/api/announcements")
def list_announcements(category: str | None = None, limit: int = 30, user: dict[str, Any] = Depends(require_user)):
    if category not in (None, "all") and category not in announcements.CATEGORIES:
        raise HTTPException(status_code=422, detail="Неизвестная категория")
    return {
        "items": announcements.list_for_user(
            engine, user["id"], None if category in (None, "all") else category, limit
        ),
        "unread": announcements.unread_counts(engine, user["id"]),
        "categories": [{"key": key, "label": label} for key, label in announcements.CATEGORIES.items()],
        "can_publish": user["role"] in {"senior", "mentor"},
    }


@app.post("/api/announcements")
async def create_announcement(request: Request, user: dict[str, Any] = Depends(require_manager_csrf)):
    payload = await _read_json_object(request)
    if set(payload) - {"category", "title", "body"}:
        raise HTTPException(status_code=422, detail="Неизвестные поля анонса")
    category = str(payload.get("category") or "")
    if category not in announcements.CATEGORIES:
        raise HTTPException(status_code=422, detail="Неизвестная категория")
    title = str(payload.get("title") or "").strip()
    if not title or len(title) > announcements.MAX_TITLE:
        raise HTTPException(status_code=422, detail="Заголовок должен быть от 1 до 120 символов")
    body = str(payload.get("body") or "").strip()
    if len(body) > announcements.MAX_BODY:
        raise HTTPException(status_code=422, detail="Текст слишком длинный")
    return {"ok": True, "id": announcements.create(engine, category, title, body, user["id"], user["name"])}


@app.delete("/api/announcements/{announcement_id}")
def delete_announcement(announcement_id: int, _: dict[str, Any] = Depends(require_manager_csrf)):
    if not announcements.delete(engine, announcement_id):
        raise HTTPException(status_code=404, detail="Анонс не найден")
    return {"ok": True}


@app.post("/api/announcements/read")
async def read_announcements(request: Request, user: dict[str, Any] = Depends(require_csrf)):
    payload = await _read_json_object(request)
    if set(payload) - {"ids", "category"}:
        raise HTTPException(status_code=422, detail="Неизвестные поля")
    ids = payload.get("ids")
    if ids is not None and (not isinstance(ids, list) or any(not isinstance(value, int) for value in ids)):
        raise HTTPException(status_code=422, detail="ids должен быть списком чисел")
    category = payload.get("category")
    if category is not None and category not in announcements.CATEGORIES:
        raise HTTPException(status_code=422, detail="Неизвестная категория")
    announcements.mark_read(engine, user["id"], ids, category)
    return {"ok": True, "unread": announcements.unread_counts(engine, user["id"])}


@app.get("/api/announcements/settings")
def announcement_settings(user: dict[str, Any] = Depends(require_user)):
    return {"enabled": announcements.get_prefs(engine, user["id"])}


@app.patch("/api/announcements/settings")
async def update_announcement_settings(request: Request, user: dict[str, Any] = Depends(require_csrf)):
    payload = await _read_json_object(request)
    if set(payload) != {"enabled"} or not isinstance(payload.get("enabled"), list):
        raise HTTPException(status_code=422, detail="Ожидался список категорий")
    for value in payload["enabled"]:
        if value not in announcements.CATEGORIES:
            raise HTTPException(status_code=422, detail="Неизвестная категория")
    enabled = announcements.set_prefs(engine, user["id"], payload["enabled"])
    return {"ok": True, "enabled": enabled, "unread": announcements.unread_counts(engine, user["id"])}


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
            SELECT id, name, display_name, avatar_rev, iiko_id, telegram_username, role, is_active, telegram_id, access_code
            FROM users ORDER BY is_active DESC, COALESCE(display_name, name) COLLATE NOCASE
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
    payload = await request.json()
    # Выдача нового кода — отдельное действие: наставник видит код и отдаёт его лично.
    regenerate = bool(isinstance(payload, dict) and payload.pop("regenerate_code", False))
    values = _validate_user_payload(payload, partial=True)
    if not values and not regenerate:
        raise HTTPException(status_code=422, detail="Нет полей для обновления")
    assignments = ", ".join(f"{key} = :{key}" for key in values)
    has_fields = bool(assignments)
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
            if has_fields:
                result = connection.execute(text(f"UPDATE users SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = :user_id"), values)
                if result.rowcount == 0:
                    raise HTTPException(status_code=404, detail="Пользователь не найден")
        except IntegrityError as error:
            raise HTTPException(status_code=409, detail="Iiko ID или Telegram username уже используется") from error
    if regenerate:
        return {"ok": True, "code": access_code.set_access_code(engine, user_id)}
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Истории гостей
#
# Публичная страница живёт по прямой ссылке /stories. В интерфейсе сотрудников
# на неё нет ни одной ссылки: страница для гостей, а не для команды.
# Модерация перенесена из отдельного прототипа в панель управления наставника и
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

@app.get("/sw.js")
def service_worker():
    """Service Worker отдаём именно из корня: от пути зависит его область.

    Из /static/sw.js область была бы только /static/ и страницы он бы не
    перехватывал. Кешировать его нельзя — браузер должен проверять новую
    версию при каждом запуске, иначе обновление приложения «залипнет».
    """
    return FileResponse(
        BASE_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


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
