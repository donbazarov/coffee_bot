"""Лента анонсов: расписание, замены, события и общая информация.

Публикации в Telegram-каналы заменены лентой внутри приложения: с этого сервера
api.telegram.org недоступен, а лента работает всегда и не требует внешних сервисов.

Категории:

* `schedule` — публикация графика смен (со снимками) — создаётся автоматически;
* `swap` — замены смен — создаётся автоматически;
* `event` — событие команды — создаёт наставник вручную;
* `general` — общая информация — создаёт наставник вручную.

У каждого сотрудника свой «колокольчик»: набор категорий
(`web_announcement_prefs`), которые учитываются в счётчике непрочитанного.
Сама лента показывает всё — колокольчик влияет только на бейдж.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

from bot.web.calendar_service import _ensure_columns

logger = logging.getLogger("bot.web.announcements")

CATEGORIES = {
    "schedule": "Расписание",
    "swap": "Замены",
    "event": "Событие",
    "general": "Общая информация",
}
# Системные категории создаёт сам сервер (при сохранении графика), их нельзя
# переименовать или убрать из настроек.
SYSTEM_CATEGORIES = ("schedule", "swap")
# Что включено в уведомлениях по умолчанию: важное для работы, без «болталки».
DEFAULT_NOTIFY = ("schedule", "swap")

MAX_TITLE = 120
MAX_BODY = 2000
MAX_LIST = 100
# Формат хранения даты анонса (локальное время точки); совпадает с SQLite-строкой.
STAMP = "%Y-%m-%d %H:%M"


def initialize_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                actor_user_id INTEGER REFERENCES users(id),
                actor_name TEXT,
                payload TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        if engine.dialect.name == "sqlite":
            # Дата анонса появилась позже самой ленты — докидываем в живые базы.
            _ensure_columns(connection, "web_announcements", {"start_at": "TEXT"})
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_web_announcements_category
            ON web_announcements (category, created_at)
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_announcement_reads (
                user_id INTEGER NOT NULL REFERENCES users(id),
                announcement_id INTEGER NOT NULL REFERENCES web_announcements(id),
                read_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, announcement_id)
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_announcement_prefs (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                categories TEXT NOT NULL
            )
        """))


def _iso(value: Any) -> str:
    """SQLite хранит UTC как «YYYY-MM-DD HH:MM:SS» — отдаём ISO с Z для браузера."""
    stamp = str(value or "").strip()
    if not stamp:
        return ""
    return stamp.replace(" ", "T") + "Z"


def _payload(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _local_iso(value: Any) -> str:
    """«2026-10-12 15:00» → «2026-10-12T15:00:00»: браузеры читают это как локальное время."""
    stamp = str(value or "").strip()
    if not stamp:
        return ""
    normalized = stamp.replace(" ", "T")
    return normalized if "T" in normalized and len(normalized) > 16 else normalized + ":00"


def normalize_start_at(value: Any) -> str | None:
    """Принимает «2026-10-12T15:00» из <input type="datetime-local"> либо пустое."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ValueError("Дата и время должны быть в формате ГГГГ-ММ-ДД ЧЧ:ММ") from error
    return moment.strftime(STAMP)


def categories_payload() -> list[dict[str, Any]]:
    """Категории для интерфейса: системные создаёт код, их нельзя настраивать."""
    return [
        {"key": key, "label": label, "system": key in SYSTEM_CATEGORIES}
        for key, label in CATEGORIES.items()
    ]


def _ics_escape(value: Any) -> str:
    """Экранирование по RFC 5545: запятые, точки с запятой и переводы строк."""
    text_value = str(value or "")
    text_value = text_value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text_value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def create(
    engine: Engine,
    category: str,
    title: str,
    body: str = "",
    actor_id: int | None = None,
    actor_name: str = "",
    payload: dict[str, Any] | None = None,
    start_at: str | None = None,
) -> int:
    if category not in CATEGORIES:
        raise ValueError(f"Неизвестная категория анонса: {category}")
    with engine.begin() as connection:
        result = connection.execute(text("""
            INSERT INTO web_announcements (category, title, body, actor_user_id, actor_name, payload, start_at)
            VALUES (:category, :title, :body, :actor_id, :actor_name, :payload, :start_at)
        """), {
            "category": category,
            "title": title.strip()[:MAX_TITLE] or CATEGORIES[category],
            "body": (body or "").strip()[:MAX_BODY],
            "actor_id": actor_id,
            "actor_name": (actor_name or "").strip() or None,
            "payload": json.dumps(payload, ensure_ascii=False) if payload else None,
            "start_at": normalize_start_at(start_at),
        })
        return int(result.lastrowid or 0)


def publish_schedule_events(engine: Engine, result: dict[str, Any], actor: str, actor_id: int | None = None) -> None:
    """Создаёт записи ленты по итогам сохранения графика.

    Вызывается из запроса сохранения, поэтому любые ошибки только логируются:
    лента не имеет права сломать публикацию графика.
    """
    try:
        if result.get("change_type") == "swap":
            lines = result.get("swap_lines") or []
            if lines:
                create(engine, "swap", f"Замены · {actor}", "\n".join(lines), actor_id, actor)
            return

        if not result.get("published"):
            return

        snapshots = result.get("snapshots") or []
        period_start, period_end = result.get("period_start"), result.get("period_end")
        if period_start and period_end:
            title = f"График смен опубликован · {period_start} — {period_end}"
        else:
            title = "График смен опубликован"
        captions = [shot.get("caption") for shot in snapshots if shot.get("caption")]
        body = "\n".join(captions) if captions else f"Снимков: {len(snapshots)}"
        create(
            engine,
            "schedule",
            title,
            body,
            actor_id,
            actor,
            {"snapshots": [shot.get("url") for shot in snapshots if shot.get("url")]},
        )
    except Exception as error:  # noqa: BLE001 — лента не должна ломать сохранение графика
        logger.warning("Не удалось создать анонс: %s", error)


def list_for_user(
    engine: Engine,
    user_id: int,
    category: str | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 30), MAX_LIST))
    filter_sql = "WHERE a.category = :category" if category in CATEGORIES else ""
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT a.id, a.category, a.title, a.body, a.actor_name, a.payload, a.created_at, a.start_at,
                   CASE WHEN r.user_id IS NULL THEN 0 ELSE 1 END AS is_read
            FROM web_announcements AS a
            LEFT JOIN web_announcement_reads AS r
                   ON r.announcement_id = a.id AND r.user_id = :user_id
            {filter_sql}
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT :limit
        """), {"user_id": user_id, "category": category, "limit": limit}).mappings().all()

    return [
        {
            "id": row["id"],
            "category": row["category"],
            "category_label": CATEGORIES.get(row["category"], row["category"]),
            "title": row["title"],
            "body": row["body"] or "",
            "actor": row["actor_name"] or "",
            "created_at": _iso(row["created_at"]),
            "start_at": _local_iso(row["start_at"]),
            "is_read": bool(row["is_read"]),
            "payload": _payload(row["payload"]),
        }
        for row in rows
    ]


def build_feed(engine: Engine, user_id: int, user_timezone: ZoneInfo) -> str:
    """ICS-фид анонсов с датой.

    В фид попадают только категории, включённые в уведомлениях: уведомления —
    единая настройка для счётчика, push и календаря.
    """
    enabled = set(get_prefs(engine, user_id))
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT id, category, title, body, start_at
            FROM web_announcements
            WHERE start_at IS NOT NULL AND start_at != ''
            ORDER BY start_at
        """)).mappings().all()

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Coffee Quality//Announcements//RU",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
        "X-WR-CALNAME:Анонсы — НЕФТЬ",
    ]
    for row in rows:
        if row["category"] not in enabled:
            continue
        try:
            start_local = datetime.strptime(str(row["start_at"]).strip(), STAMP).replace(tzinfo=user_timezone)
        except ValueError:
            continue
        start_utc = start_local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        end_utc = (start_local + timedelta(hours=1)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:coffee-announcement-{row['id']}@coffee-quality.local",
            f"DTSTAMP:{now}",
            f"DTSTART:{start_utc}",
            f"DTEND:{end_utc}",
            f"SUMMARY:{_ics_escape(row['title'])}",
            f"CATEGORIES:{_ics_escape(CATEGORIES.get(row['category'], row['category']))}",
            f"DESCRIPTION:{_ics_escape(row['body'])}",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def unread_counts(engine: Engine, user_id: int) -> dict[str, Any]:
    """Непрочитанное по категориям. Считаем только категории из «колокольчика»."""
    enabled = [name for name in get_prefs(engine, user_id) if name in CATEGORIES]
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT a.category, COUNT(*) AS total
            FROM web_announcements AS a
            LEFT JOIN web_announcement_reads AS r
                   ON r.announcement_id = a.id AND r.user_id = :user_id
            WHERE r.user_id IS NULL
            GROUP BY a.category
        """), {"user_id": user_id}).mappings().all()
    per_category = {row["category"]: int(row["total"]) for row in rows}
    return {
        "total": sum(count for name, count in per_category.items() if name in enabled),
        "by_category": per_category,
        "enabled": enabled,
    }


def mark_read(engine: Engine, user_id: int, ids: Iterable[int] | None = None, category: str | None = None) -> int:
    with engine.begin() as connection:
        if ids:
            values = [int(value) for value in ids]
            placeholders = ", ".join(f":id{index}" for index in range(len(values)))
            params = {"user_id": user_id}
            params.update({f"id{index}": value for index, value in enumerate(values)})
            connection.execute(text(f"""
                INSERT OR IGNORE INTO web_announcement_reads (user_id, announcement_id)
                SELECT :user_id, id FROM web_announcements WHERE id IN ({placeholders})
            """), params)
            return len(values)
        if category in CATEGORIES:
            result = connection.execute(text("""
                INSERT OR IGNORE INTO web_announcement_reads (user_id, announcement_id)
                SELECT :user_id, id FROM web_announcements WHERE category = :category
            """), {"user_id": user_id, "category": category})
        else:
            result = connection.execute(text("""
                INSERT OR IGNORE INTO web_announcement_reads (user_id, announcement_id)
                SELECT :user_id, id FROM web_announcements
            """), {"user_id": user_id})
        return int(result.rowcount or 0)


def get_prefs(engine: Engine, user_id: int) -> list[str]:
    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT categories FROM web_announcement_prefs WHERE user_id = :user_id"),
            {"user_id": user_id},
        ).scalar_one_or_none()
    if stored is None:
        return list(DEFAULT_NOTIFY)
    try:
        parsed = json.loads(stored)
    except (TypeError, ValueError):
        return list(DEFAULT_NOTIFY)
    if not isinstance(parsed, list):
        return list(DEFAULT_NOTIFY)
    return [name for name in parsed if name in CATEGORIES]


def set_prefs(engine: Engine, user_id: int, categories: Iterable[str]) -> list[str]:
    valid = [name for name in dict.fromkeys(categories) if name in CATEGORIES]
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO web_announcement_prefs (user_id, categories) VALUES (:user_id, :categories)
            ON CONFLICT(user_id) DO UPDATE SET categories = :categories
        """), {"user_id": user_id, "categories": json.dumps(valid, ensure_ascii=False)})
    return valid


def delete(engine: Engine, announcement_id: int) -> bool:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM web_announcement_reads WHERE announcement_id = :id"),
            {"id": announcement_id},
        )
        result = connection.execute(
            text("DELETE FROM web_announcements WHERE id = :id"), {"id": announcement_id}
        )
    return bool(result.rowcount)
