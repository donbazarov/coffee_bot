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
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger("bot.web.announcements")

CATEGORIES = {
    "schedule": "Расписание",
    "swap": "Замены",
    "event": "Событие",
    "general": "Общая информация",
}
# Что включено в колокольчике по умолчанию: важное для работы, без «болталки».
DEFAULT_NOTIFY = ("schedule", "swap")

MAX_TITLE = 120
MAX_BODY = 2000
MAX_LIST = 100


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


def create(
    engine: Engine,
    category: str,
    title: str,
    body: str = "",
    actor_id: int | None = None,
    actor_name: str = "",
    payload: dict[str, Any] | None = None,
) -> int:
    if category not in CATEGORIES:
        raise ValueError(f"Неизвестная категория анонса: {category}")
    with engine.begin() as connection:
        result = connection.execute(text("""
            INSERT INTO web_announcements (category, title, body, actor_user_id, actor_name, payload)
            VALUES (:category, :title, :body, :actor_id, :actor_name, :payload)
        """), {
            "category": category,
            "title": title.strip()[:MAX_TITLE] or CATEGORIES[category],
            "body": (body or "").strip()[:MAX_BODY],
            "actor_id": actor_id,
            "actor_name": (actor_name or "").strip() or None,
            "payload": json.dumps(payload, ensure_ascii=False) if payload else None,
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
            SELECT a.id, a.category, a.title, a.body, a.actor_name, a.payload, a.created_at,
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
            "is_read": bool(row["is_read"]),
            "payload": _payload(row["payload"]),
        }
        for row in rows
    ]


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
