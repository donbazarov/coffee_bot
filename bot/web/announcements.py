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
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Engine

from bot.web.calendar_service import _ensure_columns

logger = logging.getLogger("bot.web.announcements")

# Категории по умолчанию: code, название, важное, цвет, системная, порядок.
# `system` создаёт сам сервер (публикация графика и замены) — такие категории
# нельзя переименовать, перекрасить или удалить. `important` решает, попадает ли
# категория в раздел «Важное» на главной.
DEFAULT_CATEGORIES = (
    ("schedule", "Расписание", 1, "#f47369", 1, 10),
    ("swap", "Замены", 0, "#59a07a", 1, 20),
    ("event", "Событие", 1, "#c9a227", 0, 30),
    ("general", "Общая информация", 1, "#7b8794", 0, 40),
)
# Что включено в уведомлениях по умолчанию: важное для работы, без «болталки».
DEFAULT_NOTIFY = ("schedule", "swap")
FALLBACK_COLOR = "#7b8794"
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}

MAX_TITLE = 120
MAX_BODY = 2000
MAX_LABEL = 60
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
            # Дата и закрепление анонса появились позже самой ленты.
            _ensure_columns(connection, "web_announcements", {
                "start_at": "TEXT",
                "pinned": "INTEGER NOT NULL DEFAULT 0",
            })
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_web_announcements_category
            ON web_announcements (category, created_at)
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_announcement_categories (
                code TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                important INTEGER NOT NULL DEFAULT 1,
                color TEXT NOT NULL DEFAULT '#7b8794',
                system INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 100,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        # Категории по умолчанию — один раз, дальше их ведёт панель управления.
        for code, label, important, color, system, position in DEFAULT_CATEGORIES:
            connection.execute(text("""
                INSERT OR IGNORE INTO web_announcement_categories
                    (code, label, important, color, system, position)
                VALUES (:code, :label, :important, :color, :system, :position)
            """), {"code": code, "label": label, "important": important, "color": color,
                   "system": system, "position": position})
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


def _clean_color(value: Any) -> str:
    color = str(value or "").strip()
    return color if COLOR_RE.match(color) else FALLBACK_COLOR


def _translit(value: str) -> str:
    return "".join(TRANSLIT.get(character, character) for character in value.lower())


def all_categories(engine: Engine) -> list[dict[str, Any]]:
    """Категории для интерфейса: название, цвет, важность и признак системной."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT code, label, important, color, system, position
            FROM web_announcement_categories ORDER BY position, label
        """)).mappings().all()
    return [
        {
            "code": row["code"],
            "label": row["label"],
            "important": bool(row["important"]),
            "color": _clean_color(row["color"]),
            "system": bool(row["system"]),
            "position": int(row["position"]),
        }
        for row in rows
    ]


def category_map(engine: Engine) -> dict[str, dict[str, Any]]:
    return {item["code"]: item for item in all_categories(engine)}


def category_keys(engine: Engine) -> set[str]:
    return set(category_map(engine))


def _make_code(engine: Engine, label: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", _translit(label).lower()).strip("-") or "category"
    base = base[:40]
    existing = category_keys(engine)
    code, index = base, 2
    while code in existing:
        code = f"{base}-{index}"
        index += 1
    return code


def create_category(engine: Engine, label: str, color: Any = None, important: bool = True) -> str:
    clean_label = str(label or "").strip()[:MAX_LABEL]
    if not clean_label:
        raise ValueError("Название категории обязательно")
    code = _make_code(engine, clean_label)
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO web_announcement_categories (code, label, important, color, system, position)
            VALUES (:code, :label, :important, :color, 0,
                    COALESCE((SELECT MAX(position) FROM web_announcement_categories), 0) + 10)
        """), {"code": code, "label": clean_label, "important": 1 if important else 0,
               "color": _clean_color(color)})
    return code


def update_category(
    engine: Engine,
    code: str,
    label: Any = None,
    color: Any = None,
    important: bool | None = None,
) -> dict[str, Any]:
    current = category_map(engine).get(code)
    if current is None:
        raise LookupError("Категория не найдена")
    if current["system"]:
        raise PermissionError("Системную категорию менять нельзя")
    values = {
        "code": code,
        "label": str(label).strip()[:MAX_LABEL] if str(label or "").strip() else current["label"],
        "color": _clean_color(color if color is not None else current["color"]),
        "important": (1 if important else 0) if important is not None else (1 if current["important"] else 0),
    }
    with engine.begin() as connection:
        connection.execute(text("""
            UPDATE web_announcement_categories
            SET label = :label, color = :color, important = :important
            WHERE code = :code
        """), values)
    return category_map(engine)[code]


def delete_category(engine: Engine, code: str) -> None:
    """Удаляет категорию, если в ней нет анонсов и она не системная."""
    current = category_map(engine).get(code)
    if current is None:
        raise LookupError("Категория не найдена")
    if current["system"]:
        raise PermissionError("Системную категорию удалить нельзя")
    with engine.connect() as connection:
        used = int(connection.execute(text(
            "SELECT COUNT(*) FROM web_announcements WHERE category = :code"
        ), {"code": code}).scalar_one())
    if used:
        raise ValueError(f"В категории {used} анонсов — сначала перенесите или удалите их")
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM web_announcement_categories WHERE code = :code"), {"code": code}
        )


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
    pinned: bool = False,
) -> int:
    categories = category_map(engine)
    if category not in categories:
        raise ValueError(f"Неизвестная категория анонса: {category}")
    with engine.begin() as connection:
        result = connection.execute(text("""
            INSERT INTO web_announcements
                (category, title, body, actor_user_id, actor_name, payload, start_at, pinned)
            VALUES (:category, :title, :body, :actor_id, :actor_name, :payload, :start_at, :pinned)
        """), {
            "category": category,
            "title": title.strip()[:MAX_TITLE] or categories[category]["label"],
            "body": (body or "").strip()[:MAX_BODY],
            "actor_id": actor_id,
            "actor_name": (actor_name or "").strip() or None,
            "payload": json.dumps(payload, ensure_ascii=False) if payload else None,
            "start_at": normalize_start_at(start_at),
            "pinned": 1 if pinned else 0,
        })
        return int(result.lastrowid or 0)


def update(
    engine: Engine,
    announcement_id: int,
    title: str | None = None,
    body: str | None = None,
    category: str | None = None,
    start_at: Any = None,
    pinned: bool | None = None,
) -> bool:
    """Правка анонса. Системный анонс нельзя перевести в другую категорию."""
    categories = category_map(engine)
    with engine.begin() as connection:
        current = connection.execute(text("""
            SELECT category, title, body, start_at, pinned FROM web_announcements WHERE id = :id
        """), {"id": announcement_id}).mappings().first()
        if current is None:
            return False
        system_codes = {code for code, item in categories.items() if item["system"]}
        target = category if category in categories else current["category"]
        if current["category"] in system_codes and target != current["category"]:
            raise PermissionError("Системный анонс нельзя перевести в другую категорию")
        connection.execute(text("""
            UPDATE web_announcements
            SET title = :title, body = :body, category = :category, start_at = :start_at, pinned = :pinned
            WHERE id = :id
        """), {
            "id": announcement_id,
            "title": (str(title).strip()[:MAX_TITLE] if str(title or "").strip() else current["title"]),
            "body": (str(body).strip()[:MAX_BODY] if body is not None else (current["body"] or "")),
            "category": target,
            "start_at": normalize_start_at(start_at) if start_at is not None else current["start_at"],
            "pinned": (1 if pinned else 0) if pinned is not None else int(current["pinned"] or 0),
        })
    return True


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
    """Лента. `category="important"` — только важные категории (без «Замен»)."""
    limit = max(1, min(int(limit or 30), MAX_LIST))
    categories = category_map(engine)
    params: dict[str, Any] = {"user_id": user_id, "limit": limit}
    where = ""
    if category == "important":
        keys = [code for code, item in categories.items() if item["important"]]
        if not keys:
            return []
        placeholders = ", ".join(f":cat{index}" for index in range(len(keys)))
        params.update({f"cat{index}": code for index, code in enumerate(keys)})
        where = f"WHERE a.category IN ({placeholders})"
    elif category in categories:
        where = "WHERE a.category = :category"
        params["category"] = category

    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT a.id, a.category, a.title, a.body, a.actor_name, a.payload, a.created_at,
                   a.start_at, a.pinned,
                   CASE WHEN r.user_id IS NULL THEN 0 ELSE 1 END AS is_read
            FROM web_announcements AS a
            LEFT JOIN web_announcement_reads AS r
                   ON r.announcement_id = a.id AND r.user_id = :user_id
            {where}
            ORDER BY a.pinned DESC, a.created_at DESC, a.id DESC
            LIMIT :limit
        """), params).mappings().all()

    return [
        {
            "id": row["id"],
            "category": row["category"],
            "category_label": (categories.get(row["category"]) or {}).get("label", row["category"]),
            "color": (categories.get(row["category"]) or {}).get("color", FALLBACK_COLOR),
            "important": bool((categories.get(row["category"]) or {}).get("important")),
            "system": bool((categories.get(row["category"]) or {}).get("system")),
            "title": row["title"],
            "body": row["body"] or "",
            "actor": row["actor_name"] or "",
            "created_at": _iso(row["created_at"]),
            "start_at": _local_iso(row["start_at"]),
            "pinned": bool(row["pinned"]),
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
    labels = {code: item["label"] for code, item in category_map(engine).items()}
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
            f"CATEGORIES:{_ics_escape(labels.get(row['category'], row['category']))}",
            f"DESCRIPTION:{_ics_escape(row['body'])}",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def unread_counts(engine: Engine, user_id: int) -> dict[str, Any]:
    """Непрочитанное по категориям. Считаем только категории из уведомлений."""
    enabled = [name for name in get_prefs(engine, user_id) if name in category_keys(engine)]
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
        if category and category in category_keys(engine):
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
        return [name for name in DEFAULT_NOTIFY if name in category_keys(engine)]
    try:
        parsed = json.loads(stored)
    except (TypeError, ValueError):
        return [name for name in DEFAULT_NOTIFY if name in category_keys(engine)]
    if not isinstance(parsed, list):
        return [name for name in DEFAULT_NOTIFY if name in category_keys(engine)]
    keys = category_keys(engine)
    return [name for name in parsed if name in keys]


def set_prefs(engine: Engine, user_id: int, categories: Iterable[str]) -> list[str]:
    keys = category_keys(engine)
    valid = [name for name in dict.fromkeys(categories) if name in keys]
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
