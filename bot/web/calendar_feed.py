"""Единый личный ICS-фид: смены + анонсы с датой + задачи с дедлайном.

Замена двух старых фидов («Смены» и «Анонсы») одним календарём на пользователя:
подписка одна, а в календарь попадают смены сотрудника, анонсы с `start_at`
и задачи, у которых есть `due_date` и адресат совпадает с пользователем
(`assignee_type='user'` — лично, `'role'` — его должность, `'all'` — все).

Формат ссылки завязан на токен из `auth.create_user_feed_token`: внутрь зашита
эпоха `users.calendar_reset_at`, поэтому после «Сбросить ссылку» старые ссылки
перестают работать, не дожидаясь ротации секрета WEB_SESSION_SECRET.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import text
from sqlalchemy.engine import Engine

from bot.database.models import engine
from bot.web import page_links, work_points
from bot.web.auth import (
    create_announcements_token,
    create_user_feed_token,
    read_announcements_token,
    read_calendar_token,
    read_user_feed_token,
)
from bot.web.calendar_service import _ensure_columns, _shift_bounds, app_timezone
from bot.web.deps import require_user

logger = logging.getLogger("bot.web.calendar_feed")

feed_router = APIRouter()

# Варианты напоминаний VALARM, доступные в личном кабинете: минуты до события.
REMINDER_OPTIONS = (60, 180, 1440, 10080)  # час, 3 часа, день, неделя
DEFAULT_REMINDERS = (60,)

_ICS_DTSTAMP = "%Y%m%dT%H%M%SZ"


def initialize_feed_schema(engine: Engine) -> None:
    """Докатывает поля календаря пользователя (вызывает также `models.init_db`)."""
    with engine.begin() as connection:
        _ensure_columns(connection, "users", {
            "calendar_reset_at": "TEXT",
            "calendar_reminders": "TEXT",
        })


def _ics_escape(value: Any) -> str:
    """Экранирование по RFC 5545: запятые, точки с запятой и переводы строк."""
    text_value = str(value or "")
    text_value = text_value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text_value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def parse_reminders(raw: Any) -> tuple[int, ...]:
    """Минуты напоминаний из `users.calendar_reminders` (список через запятую)."""
    if raw is None:
        return DEFAULT_REMINDERS
    values: list[int] = []
    for item in str(raw).split(","):
        item = item.strip()
        if not item:
            continue
        try:
            minutes = int(float(item))
        except (TypeError, ValueError):
            continue
        if minutes > 0 and minutes not in values:
            values.append(minutes)
    return tuple(sorted(values)) if values else DEFAULT_REMINDERS


def user_reminders(engine: Engine, user_id: int) -> tuple[int, ...]:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT calendar_reminders FROM users WHERE id = :user_id"),
            {"user_id": user_id},
        ).mappings().first()
    return parse_reminders(None if row is None else row["calendar_reminders"])


def _valarm_lines(minutes: tuple[int, ...]) -> list[str]:
    """По одному VALARM на точку напоминания: `TRIGGER:-PT…M` до начала."""
    lines: list[str] = []
    for minutes_before in minutes:
        lines.extend([
            "BEGIN:VALARM",
            f"TRIGGER:-PT{minutes_before}M",
            "ACTION:DISPLAY",
            "END:VALARM",
        ])
    return lines


def _feed_user(connection: Any, user_id: int) -> dict[str, Any] | None:
    return connection.execute(text("""
        SELECT id, name, display_name, iiko_id
        FROM users WHERE id = :user_id
    """), {"user_id": user_id}).mappings().first()


def build_user_feed(engine: Engine, user_id: int, user_timezone: ZoneInfo) -> str:
    """Собирает ICS-календарь сотрудника: смены, анонсы, задачи."""
    with engine.connect() as connection:
        user = _feed_user(connection, user_id)
        if user is None or user["iiko_id"] is None:
            return _empty_feed()
        display = user["display_name"] or user["name"]

        shifts = connection.execute(text("""
            SELECT s.shift_id, s.shift_date, st.start_time, st.end_time, st.point
            FROM schedule s JOIN shift_types st ON st.id = s.shift_type_id
            WHERE s.iiko_id = :iiko_id AND COALESCE(s.is_active, 1) = 1
            ORDER BY s.shift_date, st.start_time
        """), {"iiko_id": str(user["iiko_id"])}).mappings().all()

        announcements = connection.execute(text("""
            SELECT id, category, title, body, start_at
            FROM web_announcements
            WHERE start_at IS NOT NULL AND start_at != ''
            ORDER BY start_at
        """)).mappings().all()
        labels = {row["code"]: row["label"] for row in connection.execute(text(
            "SELECT code, label FROM web_announcement_categories"
        )).mappings().all()}

        # Задачи: только с указанной датой, не удалённые и не ушедшие в архив.
        role_rows = connection.execute(text("""
            SELECT ur.role_key FROM user_roles ur
            JOIN role_access_config r ON r.role_key = ur.role_key
            WHERE ur.user_id = :user_id AND r.is_active = 1
        """), {"user_id": user_id}).scalars().all()
        role_params = {f"r{index}": value for index, value in enumerate(role_rows)}
        role_sql = f"t.assignee_role IN ({', '.join(f':{name}' for name in role_params)})" if role_rows else "1 = 0"
        tasks = connection.execute(text(f"""
            SELECT t.id, t.title, t.description, t.due_date, t.due_time,
                   t.status, t.assignee_type, t.assignee_role, t.assignee_user_id
            FROM tasks t
            WHERE t.due_date IS NOT NULL AND t.due_date != ''
              AND t.is_deleted = 0
              AND t.status NOT IN ('archived_done', 'archived_trash')
              AND (
                    t.assignee_type = 'all'
                    OR t.assignee_user_id = :user_id
                    OR (t.assignee_type = 'role' AND ({role_sql}))
                  )
            ORDER BY t.due_date, t.due_time
        """), {"user_id": user_id, **role_params}).mappings().all()

        reminders = parse_reminders(connection.execute(
            text("SELECT calendar_reminders FROM users WHERE id = :user_id"),
            {"user_id": user_id},
        ).scalar_one_or_none())

    project = work_points.project_name(engine)
    index = work_points.point_index(engine)
    now = datetime.now(user_timezone).astimezone(timezone.utc).strftime(_ICS_DTSTAMP)

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Coffee Quality//Team Calendar//RU",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        # Подсказка клиенту, как часто перечитывать фид (RFC 7986 + X-PUBLISHED-TTL).
        # Фид собирается из БД на каждый запрос — больший интервал не нужен.
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
        f"X-WR-CALNAME:{_ics_escape(f'Календарь — {display} · {project}')}",
    ]

    # --- Смены --------------------------------------------------------------
    for shift in shifts:
        start, end = _shift_bounds(shift["shift_date"], shift["start_time"], shift["end_time"], user_timezone)
        point = index.get(str(shift["point"] or "").strip().lower())
        alias = point["title_alias"] if point else (shift["point"] or "")
        location = point["title"] if point else alias
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:coffee-shift-{shift['shift_id']}@coffee-quality.local",
            f"DTSTAMP:{now}",
            f"DTSTART:{start.astimezone(timezone.utc).strftime(_ICS_DTSTAMP)}",
            f"DTEND:{end.astimezone(timezone.utc).strftime(_ICS_DTSTAMP)}",
            f"SUMMARY:{_ics_escape(f'Смена {alias} — {project}')}",
            f"LOCATION:{_ics_escape(location)}",
        ])
        lines.extend(_valarm_lines(reminders))
        lines.append("END:VEVENT")

    # --- Анонсы с датой -----------------------------------------------------
    stamp = "%Y-%m-%d %H:%M"
    for row in announcements:
        try:
            start_local = datetime.strptime(str(row["start_at"]).strip(), stamp).replace(tzinfo=user_timezone)
        except ValueError:
            continue
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:coffee-announcement-{row['id']}@coffee-quality.local",
            f"DTSTAMP:{now}",
            f"DTSTART:{start_local.astimezone(timezone.utc).strftime(_ICS_DTSTAMP)}",
            f"DTEND:{(start_local + timedelta(hours=1)).astimezone(timezone.utc).strftime(_ICS_DTSTAMP)}",
            f"SUMMARY:{_ics_escape(row['title'])}",
            f"CATEGORIES:{_ics_escape(labels.get(row['category'], row['category']))}",
            f"DESCRIPTION:{_ics_escape(page_links.plain_text(row['body']))}",
        ])
        lines.extend(_valarm_lines(reminders))
        lines.append("END:VEVENT")

    # --- Задачи с дедлайном -------------------------------------------------
    for task in tasks:
        try:
            due = datetime.strptime(str(task["due_date"]).strip(), "%Y-%m-%d")
        except ValueError:
            continue
        done = task["status"] == "done"
        summary = f"Задача: {task['title']}" + (" (выполнено)" if done else "")
        description_parts = ["Задача из доски задач"]
        if task["description"]:
            description_parts.append(page_links.plain_text(task["description"]))
        event = [
            "BEGIN:VEVENT",
            f"UID:coffee-task-{task['id']}@coffee-quality.local",
            f"DTSTAMP:{now}",
        ]
        # Событие на весь день, либо полноценное на время из due_time.
        if task["due_time"]:
            try:
                start_local = datetime.combine(
                    due, datetime.strptime(str(task["due_time"]).strip(), "%H:%M").time(),
                    tzinfo=user_timezone,
                )
            except ValueError:
                start_local = None
            if start_local is not None:
                event.append(f"DTSTART:{start_local.astimezone(timezone.utc).strftime(_ICS_DTSTAMP)}")
                event.append(f"DTEND:{(start_local + timedelta(hours=1)).astimezone(timezone.utc).strftime(_ICS_DTSTAMP)}")
            else:
                event.append(f"DTSTART;VALUE=DATE:{task['due_date']}")
        else:
            event.append(f"DTSTART;VALUE=DATE:{task['due_date']}")
        event.append(f"SUMMARY:{_ics_escape(summary)}")
        event.append(f"DESCRIPTION:{_ics_escape(chr(10).join(description_parts))}")
        if done:
            event.append("STATUS:COMPLETED")
        event.extend(_valarm_lines(reminders))
        event.append("END:VEVENT")
        lines.extend(event)

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _empty_feed() -> str:
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Coffee Quality//Team Calendar//RU",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
        "END:VCALENDAR",
    ]) + "\r\n"

def _empty_feed() -> str:
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Coffee Quality//Team Calendar//RU",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
        "END:VCALENDAR",
    ]) + "\r\n"


# --- HTTP ------------------------------------------------------------------ #

@feed_router.get("/api/calendar/feed-link")
def calendar_feed_link(request: Request, user: dict[str, Any] = Depends(require_user)):
    """Единая личная ссылка: смены, анонсы с датой и задачи с дедлайном."""
    reset_epoch = current_reset_epoch(engine, user["id"])
    token = create_user_feed_token(user["id"], reset_epoch)
    https_url = str(request.url_for("calendar_feed", token=token))
    return {**_link_pair(https_url), "reminders": list(user_reminders(engine, user["id"]))}


@feed_router.get("/calendar/feed/{token}.ics", name="calendar_feed")
def calendar_feed(token: str):
    """Единый фид. Принимает и новые токены, и старые (смены/анонсы)."""
    user_id = read_user_feed_token(token) or read_calendar_token(token)
    if user_id is None:
        raise HTTPException(status_code=404, detail="Календарь не найден")
    content = build_user_feed(engine, user_id, app_timezone())
    return _ics_response(content)


@feed_router.get("/api/announcements/feed-link")
def announcements_feed_link(request: Request, user: dict[str, Any] = Depends(require_user)):
    """Legacy-точка: старый клиент просил «календарь анонсов».

    Отдаём ссылку на старый формат, но фид — единый календарь.
    """
    token = create_announcements_token(user["id"])
    https_url = str(request.url_for("announcements_feed", token=token))
    return _link_pair(https_url)


@feed_router.get("/announcements/feed/{token}.ics", name="announcements_feed")
def announcements_feed(token: str):
    """Legacy-фид: старые подписки продолжают работать и отдают единый календарь."""
    user_id = read_announcements_token(token)
    if user_id is None:
        raise HTTPException(status_code=404, detail="Календарь не найден")
    content = build_user_feed(engine, user_id, app_timezone())
    return _ics_response(content)


@feed_router.post("/api/calendar/feed/reset")
def calendar_feed_reset(user: dict[str, Any] = Depends(require_user)):
    """Отзывает все ранее выданные ссылки на фид (эпоха в БД растёт)."""
    with engine.begin() as connection:
        epoch = str(int(time.time()))
        connection.execute(
            text("UPDATE users SET calendar_reset_at = :epoch WHERE id = :user_id"),
            {"epoch": epoch, "user_id": user["id"]},
        )
    return {"reset_at": epoch}


@feed_router.patch("/api/calendar/reminders")
async def calendar_reminders(request: Request, user: dict[str, Any] = Depends(require_user)):
    """Напоминания VALARM: список минут до события (мультивыбор)."""
    payload = await request.json()
    raw = payload.get("reminders")
    if raw is None:
        raise HTTPException(status_code=422, detail="reminders обязателен")
    allowed = set(REMINDER_OPTIONS)
    values: list[int] = []
    for item in raw:
        try:
            minutes = int(item)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Некорректное значение напоминания")
        if minutes not in allowed:
            raise HTTPException(status_code=422, detail=f"Напоминание {minutes} недоступно")
        if minutes not in values:
            values.append(minutes)
    stored = ",".join(str(value) for value in sorted(values))
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE users SET calendar_reminders = :reminders WHERE id = :user_id"),
            {"reminders": stored, "user_id": user["id"]},
        )
    return {"reminders": [int(value) for value in sorted(values)]}


def current_reset_epoch(engine: Engine, user_id: int) -> int:
    """Актуальная эпоха сброса для выдачи новой ссылки."""
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT calendar_reset_at FROM users WHERE id = :user_id"),
            {"user_id": user_id},
        ).mappings().first()
    if not row or not row["calendar_reset_at"]:
        return 0
    try:
        return int(float(row["calendar_reset_at"]))
    except (TypeError, ValueError):
        return 0


def _link_pair(https_url: str) -> dict[str, str]:
    return {
        "https_url": https_url,
        "webcal_url": https_url.replace("https://", "webcal://", 1).replace("http://", "webcal://", 1),
    }


def _ics_response(content: str) -> Response:
    return Response(
        content,
        media_type="text/calendar; charset=utf-8",
        headers={"Cache-Control": "private, no-cache"},
    )
