"""Техническое обслуживание: точки, категории, остатки и журнал замен.

Модуль изолирован по образцу `tasks.py`/`knowledge_base.py`: доменная логика и
роуты живут здесь, `app.py` только подключает роутеры и вызывает
`initialize_schema` на старте.

Никакого хардкода категорий и точек: и те, и другие — справочники в БД.
На старте создаются только таблицы, справочники заполняются в панели управления.

Модель данных:

* `locations`            — точки сети (динамические колонки дашборда);
* `equipment_categories` — категории запчастей (цвет, порядок);
* `equipment_parts`      — каталог запчастей/расходников; карточкой управляет
  База знаний (раздел `equipment`, см. `bot/web/knowledge_base.py`), а здесь —
  чтение и расчёты (`upsert_part` вызывается из КБ, чтобы схема жила в одном
  месте);
* `location_stock`       — остатки на точках (`UNIQUE(point_code, part_id)`);
* `maintenance_logs`     — журнал замен с расчётом `next_due_date`.

Автоматизация (через модуль задач): остаток `<= min_stock` → задача на закупку,
истёкший `next_due_date` → задача на замену. Дедупликация — маркер в
`tasks.meta_info`: открытая задача с тем же маркером повторно не создаётся.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import text
from sqlalchemy.engine import Engine

from bot.database.models import engine
from bot.web import access_roles, modules, page_links, tasks
from bot.web.announcements import TRANSLIT
from bot.web.deps import require_manager_csrf, require_user

FALLBACK_COLOR = "#7b8794"
# Порог «скоро дедлайн», дней (правится в панели управления).
SOON_DAYS = 7
# Статусы обслуживания точки/запчасти: почему именно так — в `_status_for`.
STATUS_ORDER = {"overdue": 0, "soon": 1, "ok": 2, "new": 3, "none": 4}

MAX_CODE = 40
MAX_NAME = 120
MAX_MODEL = 120
MAX_ARTICLE = 60
MAX_URL = 500
MAX_COMMENT = 1000

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

modules.register_defaults("maintenance", {"SOON_DAYS": SOON_DAYS})

# Схема создаётся отдельными операторами: драйвер SQLite не принимает несколько
# `CREATE TABLE` в одном `execute`. Порядок важен — `equipment_parts` ссылается
# на `equipment_categories`, а `location_stock`/`maintenance_logs` — на первые две.
_DDL = (
    """
    CREATE TABLE IF NOT EXISTS locations (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS equipment_categories (
        code TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        color TEXT,
        position INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS equipment_parts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_code TEXT REFERENCES equipment_categories(code) ON DELETE CASCADE,
        name TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        article TEXT,
        replacement_interval_days INTEGER,
        purchase_url TEXT,
        min_stock INTEGER NOT NULL DEFAULT 1,
        page_id INTEGER REFERENCES kb_pages(id) ON DELETE SET NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS location_stock (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        point_code TEXT REFERENCES locations(code) ON DELETE CASCADE,
        part_id INTEGER REFERENCES equipment_parts(id) ON DELETE CASCADE,
        quantity INTEGER NOT NULL DEFAULT 0,
        UNIQUE(point_code, part_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS maintenance_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        point_code TEXT REFERENCES locations(code) ON DELETE CASCADE,
        part_id INTEGER REFERENCES equipment_parts(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id),
        replaced_at DATE NOT NULL,
        next_due_date DATE,
        comment TEXT,
        photo_url TEXT,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_equipment_parts_category ON equipment_parts (category_code)",
    "CREATE INDEX IF NOT EXISTS idx_location_stock_part ON location_stock (part_id)",
    "CREATE INDEX IF NOT EXISTS idx_maintenance_logs_point ON maintenance_logs (point_code, part_id)",
)


def initialize_schema(engine: Engine) -> None:
    """Идемпотентно создаёт таблицы (справочники остаются как есть, без досева)."""
    with engine.begin() as connection:
        for statement in _DDL:
            connection.execute(text(statement))


# --- Мелкие нормализаторы --------------------------------------------------- #


def _clean(value: Any, limit: int) -> str:
    return str(value).strip()[:limit] if value is not None else ""


def _clean_code(value: Any) -> str:
    """Код справочника: латиница в нижнем регистре, цифры, дефис и подчёркивание.

    Подчёркивание сохраняем: коды, заданные в панели управления или пришедшие
    из БЗ (например, `coffee_machines`), должны переживать нормализацию.
    """
    return re.sub(r"[^a-z0-9_-]+", "-", str(value or "").lower()).strip("-_")[:MAX_CODE]


def _translit(value: Any) -> str:
    return "".join(TRANSLIT.get(character, character) for character in str(value or "").lower())


def _slugify(value: Any, fallback: str) -> str:
    return _clean_code(_translit(value)) or fallback


def _clean_color(value: Any) -> str | None:
    """Цвет — только `#rrggbb`; мусор отбрасываем (без падения формы)."""
    if isinstance(value, str) and COLOR_RE.match(value.strip()):
        return value.strip().lower()
    return None


def _clean_int(value: Any, *, default: int | None = None, minimum: int = 0,
               label: str = "Значение") -> int | None:
    if value in (None, ""):
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}: нужно целое число") from error
    if number < minimum:
        raise ValueError(f"{label}: не меньше {minimum}")
    return number


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _today() -> date:
    return date.today()


# --- Локации ---------------------------------------------------------------- #


def all_locations(engine: Engine) -> list[dict[str, Any]]:
    """Все точки сети в порядке добавления (rowid) — порядок колонок дашборда."""
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT code, name, is_active FROM locations ORDER BY rowid"
        )).mappings().all()
    return [{"code": row["code"], "name": row["name"], "is_active": int(row["is_active"] or 0)}
            for row in rows]


def active_locations(engine: Engine) -> list[dict[str, Any]]:
    return [item for item in all_locations(engine) if item["is_active"]]


def _location_exists(engine: Engine, code: str) -> bool:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT 1 FROM locations WHERE code = :code"), {"code": code}
        ).first() is not None


def create_location(engine: Engine, payload: dict[str, Any]) -> str:
    """Новая точка. Код можно задать явно, иначе он собирается из названия."""
    name = _clean(payload.get("name"), MAX_NAME)
    if not name:
        raise ValueError("Название точки обязательно")
    code = _clean_code(payload.get("code")) or _slugify(name, "point")
    base, index = code, 2
    with engine.begin() as connection:
        while connection.execute(
            text("SELECT 1 FROM locations WHERE code = :code"), {"code": code}
        ).first():
            code = f"{base[:MAX_CODE - 3]}-{index}"
            index += 1
        connection.execute(text(
            "INSERT INTO locations (code, name, is_active) VALUES (:code, :name, :active)"
        ), {"code": code, "name": name, "active": 1 if payload.get("is_active", True) else 0})
    return code


def update_location(engine: Engine, code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Правка названия и активности точки."""
    values: dict[str, Any] = {}
    if "name" in payload:
        name = _clean(payload.get("name"), MAX_NAME)
        if not name:
            raise ValueError("Название точки обязательно")
        values["name"] = name
    if "is_active" in payload:
        values["is_active"] = 1 if payload.get("is_active") else 0
    if not values:
        raise ValueError("Нечего сохранять")
    assignments = ", ".join(f"{key} = :{key}" for key in values)
    with engine.begin() as connection:
        result = connection.execute(text(
            f"UPDATE locations SET {assignments} WHERE code = :code"
        ), {**values, "code": code})
        if not result.rowcount:
            raise LookupError("Точка не найдена")
    return next(item for item in all_locations(engine) if item["code"] == code)


def delete_location(engine: Engine, code: str) -> None:
    """Удаляет точку вместе с остатками и записями журнала (каскад вручную)."""
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT 1 FROM locations WHERE code = :code"), {"code": code}
        ).first()
        if row is None:
            raise LookupError("Точка не найдена")
        connection.execute(text("DELETE FROM location_stock WHERE point_code = :code"), {"code": code})
        connection.execute(text("DELETE FROM maintenance_logs WHERE point_code = :code"), {"code": code})
        connection.execute(text("DELETE FROM locations WHERE code = :code"), {"code": code})


# --- Категории запчастей ---------------------------------------------------- #


def _category_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT code, label, color, position FROM equipment_categories
            ORDER BY position, label COLLATE NOCASE
        """)).mappings().all()
    return [{"code": row["code"], "label": row["label"],
             "color": row["color"] or FALLBACK_COLOR, "position": int(row["position"] or 0)}
            for row in rows]


def all_categories(engine: Engine) -> list[dict[str, Any]]:
    """Категории с числом запчастей — нужны и дашборду, и панели управления."""
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for row in connection.execute(text(
            "SELECT category_code, COUNT(*) AS total FROM equipment_parts GROUP BY category_code"
        )).mappings().all():
            counts[row["category_code"]] = int(row["total"])
    return [{**item, "parts_count": counts.get(item["code"], 0)} for item in _category_rows(engine)]


def category_codes(engine: Engine) -> tuple[str, ...]:
    return tuple(item["code"] for item in _category_rows(engine))


def create_category(engine: Engine, label: Any, color: Any = None) -> str:
    """Новая категория. Код собирается из названия, при коллизии добавляется номер."""
    label = _clean(label, MAX_NAME)
    if not label:
        raise ValueError("Название категории обязательно")
    code = _slugify(label, "category")
    base, index = code, 2
    with engine.begin() as connection:
        while connection.execute(
            text("SELECT 1 FROM equipment_categories WHERE code = :code"), {"code": code}
        ).first():
            code = f"{base[:MAX_CODE - 3]}-{index}"
            index += 1
        connection.execute(text("""
            INSERT INTO equipment_categories (code, label, color, position)
            VALUES (:code, :label, :color,
                    (SELECT COALESCE(MAX(position), 0) + 10 FROM equipment_categories))
        """), {"code": code, "label": label, "color": _clean_color(color) or FALLBACK_COLOR})
    return code


def update_category(engine: Engine, code: str, label: Any = None, color: Any = None) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if label is not None:
        label = _clean(label, MAX_NAME)
        if not label:
            raise ValueError("Название категории обязательно")
        values["label"] = label
    if color is not None:
        values["color"] = _clean_color(color) or FALLBACK_COLOR
    if not values:
        raise ValueError("Нечего сохранять")
    assignments = ", ".join(f"{key} = :{key}" for key in values)
    with engine.begin() as connection:
        result = connection.execute(text(
            f"UPDATE equipment_categories SET {assignments} WHERE code = :code"
        ), {**values, "code": code})
        if not result.rowcount:
            raise LookupError("Категория не найдена")
    return next(item for item in _category_rows(engine) if item["code"] == code)


def delete_category(engine: Engine, code: str) -> None:
    """Категорию с запчастями удалить нельзя — сначала перенесите запчасти."""
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT 1 FROM equipment_categories WHERE code = :code"), {"code": code}
        ).first()
        if row is None:
            raise LookupError("Категория не найдена")
        used = connection.execute(
            text("SELECT COUNT(*) FROM equipment_parts WHERE category_code = :code"), {"code": code}
        ).scalar_one()
        if used:
            raise ValueError("В категории есть запчасти — сначала перенесите их")
        connection.execute(text("DELETE FROM equipment_categories WHERE code = :code"), {"code": code})


# --- Каталог запчастей ------------------------------------------------------ #
#
# Записи `equipment_parts` создаёт и правит База знаний (раздел `equipment`):
# карточка страницы и есть карточка запчасти. Здесь — единый источник схемы и
# чтение; `upsert_part` вызывается из `knowledge_base.py`, чтобы поля не
# дублировались в двух местах.


def part_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """Запись запчасти в форме, удобной и API, и карточке БЗ."""
    keys = set(row.keys())
    return {
        "id": int(row["id"]),
        "category_code": row["category_code"],
        "category_label": row["category_label"] if "category_label" in keys else None,
        "category_color": (row["category_color"] if "category_color" in keys else None) or FALLBACK_COLOR,
        "name": row["name"],
        "model": row["model"] or "",
        "article": row["article"] or "",
        "replacement_interval_days": row["replacement_interval_days"],
        "purchase_url": row["purchase_url"] or "",
        "min_stock": int(row["min_stock"] or 0),
        "page_id": row["page_id"] if "page_id" in keys else None,
    }


def list_parts(engine: Engine, category: str | None = None) -> list[dict[str, Any]]:
    """Каталог запчастей; необязательный фильтр по категории."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT p.*, c.label AS category_label, c.color AS category_color
            FROM equipment_parts p
            LEFT JOIN equipment_categories c ON c.code = p.category_code
            WHERE (:category IS NULL OR p.category_code = :category)
            ORDER BY c.position, p.name COLLATE NOCASE
        """), {"category": category}).mappings().all()
    return [part_payload(row) for row in rows]


def part_for_page(connection: Any, page_id: int) -> dict[str, Any] | None:
    """Запчасть, привязанная к странице БЗ (для карточки раздела `equipment`)."""
    row = connection.execute(text("""
        SELECT p.*, c.label AS category_label, c.color AS category_color
        FROM equipment_parts p
        LEFT JOIN equipment_categories c ON c.code = p.category_code
        WHERE p.page_id = :page_id
    """), {"page_id": page_id}).mappings().first()
    return part_payload(row) if row else None


def parts_by_page(connection: Any) -> dict[int, dict[str, Any]]:
    """Все запчасти, сгруппированные по `page_id` (краткая сводка для списка КБ)."""
    rows = connection.execute(text(
        "SELECT page_id, name, model, category_code FROM equipment_parts WHERE page_id IS NOT NULL"
    )).mappings().all()
    return {int(row["page_id"]): {"name": row["name"], "model": row["model"],
                                  "category_code": row["category_code"]} for row in rows}


def delete_parts_for_page(connection: Any, page_id: int) -> None:
    """Удаляет запчасть страницы вместе с остатками и журналом (каскад вручную)."""
    part_ids = [int(row[0]) for row in connection.execute(
        text("SELECT id FROM equipment_parts WHERE page_id = :page_id"), {"page_id": page_id}
    ).all()]
    for part_id in part_ids:
        connection.execute(text("DELETE FROM location_stock WHERE part_id = :id"), {"id": part_id})
        connection.execute(text("DELETE FROM maintenance_logs WHERE part_id = :id"), {"id": part_id})
    connection.execute(text("DELETE FROM equipment_parts WHERE page_id = :page_id"), {"page_id": page_id})


def upsert_part(connection: Any, page_id: int, default_name: str, payload: dict[str, Any]) -> None:
    """Создаёт/обновляет запчасть страницы. Поля можно прислать в `part` или плоско."""
    nested = payload.get("part")
    source = nested if isinstance(nested, dict) else payload
    current = connection.execute(
        text("SELECT * FROM equipment_parts WHERE page_id = :page_id"), {"page_id": page_id}
    ).mappings().first()

    name = _clean(source.get("name"), MAX_NAME) or (current["name"] if current else default_name)
    if not name:
        raise ValueError("Название запчасти обязательно")
    model = _clean(source.get("model"), MAX_MODEL) or (current["model"] if current else "")

    if "category_code" in source or current is None:
        category = _clean_code(source.get("category_code")) or None
        if category is not None:
            known = connection.execute(
                text("SELECT 1 FROM equipment_categories WHERE code = :code"), {"code": category}
            ).first()
            if known is None:
                raise ValueError("Неизвестная категория запчасти")
    else:
        category = current["category_code"]

    if "replacement_interval_days" in source:
        interval = _clean_int(source.get("replacement_interval_days"), default=None, minimum=0,
                              label="Периодичность замены") or None
    else:
        interval = current["replacement_interval_days"] if current else None

    min_stock = _clean_int(source.get("min_stock"), default=None, minimum=0, label="Мин. остаток")
    if min_stock is None:
        min_stock = int(current["min_stock"]) if current else 1

    def text_field(field: str, limit: int) -> str:
        if field in source or current is None:
            return _clean(source.get(field), limit)
        return current[field] or ""

    values = {
        "page_id": page_id,
        "category_code": category,
        "name": name,
        "model": model,
        "article": text_field("article", MAX_ARTICLE),
        "replacement_interval_days": interval,
        "purchase_url": text_field("purchase_url", MAX_URL),
        "min_stock": min_stock,
    }
    if current:
        connection.execute(text("""
            UPDATE equipment_parts
            SET category_code = :category_code, name = :name, model = :model, article = :article,
                replacement_interval_days = :replacement_interval_days,
                purchase_url = :purchase_url, min_stock = :min_stock
            WHERE page_id = :page_id
        """), values)
    else:
        connection.execute(text("""
            INSERT INTO equipment_parts
                (page_id, category_code, name, model, article,
                 replacement_interval_days, purchase_url, min_stock)
            VALUES (:page_id, :category_code, :name, :model, :article,
                    :replacement_interval_days, :purchase_url, :min_stock)
        """), values)


def get_part(engine: Engine, part_id: int) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT p.*, c.label AS category_label, c.color AS category_color
            FROM equipment_parts p
            LEFT JOIN equipment_categories c ON c.code = p.category_code
            WHERE p.id = :id
        """), {"id": part_id}).mappings().first()
    return part_payload(row) if row else None


# --- Остатки на точках ------------------------------------------------------ #


def stock_matrix(engine: Engine) -> dict[str, Any]:
    """Матрица остатков: строки — запчасти, колонки — активные точки.

    Отсутствие строки в `location_stock` означает «остаток не ведётся» (в UI
    показываем прочерк), а не ноль: иначе каждая запчасть на каждой точке
    выглядела бы «ниже минимума» и плодила задачи на закупку.
    """
    locations = active_locations(engine)
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT p.id AS part_id, p.name, p.model, p.min_stock, p.article,
                   p.category_code, p.page_id,
                   c.label AS category_label, c.color AS category_color,
                   s.point_code, s.quantity
            FROM equipment_parts p
            LEFT JOIN equipment_categories c ON c.code = p.category_code
            LEFT JOIN location_stock s ON s.part_id = p.id
            ORDER BY c.position, p.name COLLATE NOCASE
        """)).mappings().all()
    parts: dict[int, dict[str, Any]] = {}
    for row in rows:
        part = parts.setdefault(int(row["part_id"]), {
            "part_id": int(row["part_id"]), "name": row["name"], "model": row["model"] or "",
            "article": row["article"] or "", "min_stock": int(row["min_stock"] or 0),
            "category_code": row["category_code"], "category_label": row["category_label"],
            "category_color": row["category_color"] or FALLBACK_COLOR, "quantities": {},
            "page_id": int(row["page_id"]) if row["page_id"] else None,
        })
        if row["point_code"] is not None:
            part["quantities"][row["point_code"]] = int(row["quantity"] or 0)
    return {"locations": locations, "parts": list(parts.values())}


def set_stock(engine: Engine, point_code: Any, part_id: Any, quantity: Any) -> dict[str, Any]:
    """Ставит остаток запчасти на точке (upsert) и возвращает итоговую запись."""
    code = _clean_code(point_code)
    if not code or not _location_exists(engine, code):
        raise ValueError("Точка не найдена")
    part = _clean_int(part_id, default=None, minimum=1, label="Запчасть")
    if part is None or get_part(engine, part) is None:
        raise ValueError("Запчасть не найдена")
    amount = _clean_int(quantity, default=0, minimum=0, label="Остаток")
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO location_stock (point_code, part_id, quantity)
            VALUES (:code, :part_id, :quantity)
            ON CONFLICT(point_code, part_id) DO UPDATE SET quantity = excluded.quantity
        """), {"code": code, "part_id": part, "quantity": amount})
    return {"point_code": code, "part_id": part, "quantity": amount}


# --- Журнал замен ----------------------------------------------------------- #


def list_logs(engine: Engine, point_code: str | None = None, part_id: int | None = None,
              limit: int = 200) -> list[dict[str, Any]]:
    """Журнал замен (свежие сверху) с названиями точки, запчасти и автора."""
    limit = max(1, min(int(limit or 200), 500))
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT m.*, l.name AS point_name, p.name AS part_name, p.model AS part_model,
                   p.page_id AS part_page_id,
                   c.label AS category_label, u.display_name AS author_name, u.name AS author_login
            FROM maintenance_logs m
            LEFT JOIN locations l ON l.code = m.point_code
            LEFT JOIN equipment_parts p ON p.id = m.part_id
            LEFT JOIN equipment_categories c ON c.code = p.category_code
            LEFT JOIN users u ON u.id = m.user_id
            WHERE (:point IS NULL OR m.point_code = :point)
              AND (:part IS NULL OR m.part_id = :part)
            ORDER BY m.replaced_at DESC, m.id DESC
            LIMIT :limit
        """), {"point": point_code, "part": part_id, "limit": limit}).mappings().all()
    return [{
        "id": int(row["id"]), "point_code": row["point_code"],
        "point_name": row["point_name"] or row["point_code"],
        "part_id": int(row["part_id"]), "part_name": row["part_name"] or f"#{row['part_id']}",
        "part_model": row["part_model"] or "", "category_label": row["category_label"],
        "part_page_id": int(row["part_page_id"]) if row["part_page_id"] else None,
        "user_id": row["user_id"], "author": row["author_name"] or row["author_login"] or "",
        "replaced_at": str(row["replaced_at"] or "")[:10],
        "next_due_date": str(row["next_due_date"])[:10] if row["next_due_date"] else None,
        "comment": row["comment"] or "", "photo_url": row["photo_url"] or "",
        "created_at": row["created_at"],
    } for row in rows]


def logs_stats(engine: Engine) -> dict[str, Any]:
    """Сводка по журналу: всего, за 30/90 дней, топ запчастей и точки."""
    with engine.connect() as connection:
        totals = connection.execute(text("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN replaced_at >= date('now','-30 day') THEN 1 ELSE 0 END) AS last30,
                   SUM(CASE WHEN replaced_at >= date('now','-90 day') THEN 1 ELSE 0 END) AS last90
            FROM maintenance_logs
        """)).mappings().first()
        by_part = connection.execute(text("""
            SELECT COALESCE(p.name, 'Запчасть #' || m.part_id) AS label, COUNT(*) AS total
            FROM maintenance_logs m LEFT JOIN equipment_parts p ON p.id = m.part_id
            GROUP BY m.part_id ORDER BY total DESC, label LIMIT 8
        """)).mappings().all()
        by_point = connection.execute(text("""
            SELECT COALESCE(l.name, m.point_code) AS label, COUNT(*) AS total
            FROM maintenance_logs m LEFT JOIN locations l ON l.code = m.point_code
            GROUP BY m.point_code ORDER BY total DESC
        """)).mappings().all()
    return {
        "total": int(totals["total"] or 0),
        "last_30_days": int(totals["last30"] or 0),
        "last_90_days": int(totals["last90"] or 0),
        "by_part": [{"label": row["label"], "total": int(row["total"])} for row in by_part],
        "by_point": [{"label": row["label"], "total": int(row["total"])} for row in by_point],
    }


def create_log(engine: Engine, user: dict[str, Any], payload: dict[str, Any]) -> int:
    """Запись о замене. `next_due_date` считается от периодичности запчасти."""
    code = _clean_code(payload.get("point_code"))
    if not code or not _location_exists(engine, code):
        raise ValueError("Точка не найдена")
    part_id = _clean_int(payload.get("part_id"), default=None, minimum=1, label="Запчасть")
    part = get_part(engine, part_id) if part_id else None
    if part is None:
        raise ValueError("Запчасть не найдена")

    replaced_at = _parse_date(_clean(payload.get("replaced_at"), 10) or _today().isoformat())
    if replaced_at is None:
        raise ValueError("Дата замены в формате ГГГГ-ММ-ДД")

    due_raw = _clean(payload.get("next_due_date"), 10)
    if due_raw:
        due = _parse_date(due_raw)
        if due is None:
            raise ValueError("Следующая замена в формате ГГГГ-ММ-ДД")
    elif part["replacement_interval_days"]:
        due = replaced_at + timedelta(days=int(part["replacement_interval_days"]))
    else:
        due = None

    with engine.begin() as connection:
        result = connection.execute(text("""
            INSERT INTO maintenance_logs
                (point_code, part_id, user_id, replaced_at, next_due_date, comment, photo_url)
            VALUES (:point_code, :part_id, :user_id, :replaced_at, :next_due_date, :comment, :photo_url)
        """), {
            "point_code": code, "part_id": part_id, "user_id": user["id"],
            "replaced_at": replaced_at.isoformat(),
            "next_due_date": due.isoformat() if due else None,
            "comment": _clean(payload.get("comment"), MAX_COMMENT) or None,
            "photo_url": _clean(payload.get("photo_url"), MAX_URL) or None,
        })
    return int(result.lastrowid or 0)


def delete_log(engine: Engine, log_id: int) -> None:
    with engine.begin() as connection:
        result = connection.execute(text("DELETE FROM maintenance_logs WHERE id = :id"), {"id": log_id})
        if not result.rowcount:
            raise LookupError("Запись не найдена")


# --- Обзор обслуживания по точкам ------------------------------------------- #


def _status_for(interval: int | None, last: date | None, due: date | None, today: date,
                soon_days: int) -> str:
    """Статус обслуживания: `none`/`new`/`overdue`/`soon`/`ok` (см. STATUS_ORDER)."""
    if interval is None and last is None:
        return "none"          # по мере износа, истории замен нет
    if due is None:
        return "new"           # регламент есть, но замен ещё не было
    if due < today:
        return "overdue"
    if (due - today).days <= soon_days:
        return "soon"
    return "ok"


def overview(engine: Engine) -> dict[str, Any]:
    """Сводка «обслуживание по точкам»: статус и ближайший дедлайн по каждому узлу."""
    try:
        soon_days = int(modules.setting("maintenance", "SOON_DAYS", SOON_DAYS))
    except (TypeError, ValueError):
        soon_days = SOON_DAYS
    today = _today()
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT l.code AS point_code, l.name AS point_name,
                   p.id AS part_id, p.name, p.model, p.replacement_interval_days,
                   p.min_stock, p.article, p.category_code, p.page_id,
                   c.label AS category_label, c.color AS category_color,
                   (SELECT MAX(m.replaced_at) FROM maintenance_logs m
                     WHERE m.point_code = l.code AND m.part_id = p.id) AS last_replaced,
                   (SELECT m.next_due_date FROM maintenance_logs m
                     WHERE m.point_code = l.code AND m.part_id = p.id
                     ORDER BY m.replaced_at DESC, m.id DESC LIMIT 1) AS next_due
            FROM locations l
            CROSS JOIN equipment_parts p
            LEFT JOIN equipment_categories c ON c.code = p.category_code
            WHERE l.is_active = 1
            ORDER BY c.position, p.name COLLATE NOCASE, l.rowid
        """)).mappings().all()

    items: list[dict[str, Any]] = []
    counts = {key: 0 for key in STATUS_ORDER}
    for row in rows:
        interval = row["replacement_interval_days"]
        last = _parse_date(row["last_replaced"])
        due = _parse_date(row["next_due"])
        if due is None and interval and last:
            due = last + timedelta(days=int(interval))
        status = _status_for(interval, last, due, today, soon_days)
        counts[status] += 1
        items.append({
            "point_code": row["point_code"], "point_name": row["point_name"] or row["point_code"],
            "part_id": int(row["part_id"]), "name": row["name"], "model": row["model"] or "",
            "article": row["article"] or "", "category_label": row["category_label"],
            "category_color": row["category_color"] or FALLBACK_COLOR,
            "page_id": int(row["page_id"]) if row["page_id"] else None,
            "replacement_interval_days": interval,
            "min_stock": int(row["min_stock"] or 0),
            "last_replaced": last.isoformat() if last else None,
            "next_due_date": due.isoformat() if due else None,
            "days_left": (due - today).days if due else None,
            "status": status,
        })
    items.sort(key=lambda item: (STATUS_ORDER[item["status"]],
                                 item["next_due_date"] or "9999-12-31",
                                 item["name"].lower()))
    return {"today": today.isoformat(), "soon_days": soon_days,
            "counts": counts, "items": items}


# --- Автоматизация задач ---------------------------------------------------- #
#
# Задачи создаёт модуль задач (`tasks.create_task`). Чтобы не плодить дубли,
# каждая авто-задача помечается маркером в `meta_info`: открытая задача с тем же
# маркером повторно не создаётся (закрыли её — проблема вернулась — создастся
# заново, это ожидаемо). В описании — ссылка на карточку запчасти в БЗ
# (`page_links.mention`): задачу читают на точке, и переход к реквизитам
# («где заказать», артикул, модель) экономит поиск по базе знаний.


def _system_author(engine: Engine) -> int | None:
    """Автор авто-задач: активный управляющий, иначе задача без автора."""
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT u.id FROM users u
            JOIN role_access_config r ON r.role_key = u.role AND r.is_active = 1
            WHERE u.is_active = 1
              AND instr(',' || r.capabilities || ',', ',manage,') > 0
            ORDER BY r.access_level DESC, u.id LIMIT 1
        """)).first()
    return int(row[0]) if row else None


def _has_open_task(engine: Engine, token: str) -> bool:
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT 1 FROM tasks
            WHERE meta_info LIKE :token AND is_deleted = 0
              AND status IN ('todo','in_progress','done')
            LIMIT 1
        """), {"token": f"%{token}%"}).first()
    return row is not None


def _create_auto_task(engine: Engine, author_id: int | None, token: str,
                      title: str, description: str, priority: str) -> bool:
    try:
        # Автор — системный: должность берём самую старшую из справочника, чтобы
        # задача не зависела от того, кто из управляющих сейчас в смене.
        tasks.create_task(engine, {"id": author_id, "role": access_roles.highest_role()["role_key"]}, {
            "title": title,
            "description": description,
            "priority": priority,
            "meta_info": f"ТО · авто\n{token}",
            "assignee_type": "all",
        })
    except ValueError:
        return False
    return True


def run_automation(engine: Engine) -> dict[str, int]:
    """Авто-задачи: закупка (остаток ≤ `min_stock`) и замена (срок вышел)."""
    created = {"purchase": 0, "replacement": 0}
    author_id = _system_author(engine)
    with engine.connect() as connection:
        low = connection.execute(text("""
            SELECT s.point_code, l.name AS point_name, p.id AS part_id, p.name,
                   p.page_id, p.min_stock, s.quantity
            FROM location_stock s
            JOIN locations l ON l.code = s.point_code AND l.is_active = 1
            JOIN equipment_parts p ON p.id = s.part_id
            WHERE s.quantity <= p.min_stock
        """)).mappings().all()
    for row in low:
        token = f"auto:stock:{row['point_code']}:{row['part_id']}"
        if _has_open_task(engine, token):
            continue
        if _create_auto_task(engine, author_id, token,
                             f"Закупка: {row['name']} ({row['point_name']})",
                             f"{page_links.mention(row['page_id'], row['name'])} — остаток "
                             f"{row['quantity']} при минимуме {row['min_stock']}. "
                             f"Пополните запас на точке «{row['point_name']}».",
                             "high"):
            created["purchase"] += 1

    today = _today().isoformat()
    with engine.connect() as connection:
        due_rows = connection.execute(text("""
            SELECT l.code AS point_code, l.name AS point_name, p.id AS part_id, p.name,
                   p.page_id,
                   (SELECT m.next_due_date FROM maintenance_logs m
                     WHERE m.point_code = l.code AND m.part_id = p.id
                     ORDER BY m.replaced_at DESC, m.id DESC LIMIT 1) AS next_due
            FROM locations l
            CROSS JOIN equipment_parts p
            WHERE l.is_active = 1 AND p.replacement_interval_days IS NOT NULL
        """)).mappings().all()
    for row in due_rows:
        due = str(row["next_due"] or "")[:10]
        if not due or due >= today:
            continue
        token = f"auto:due:{row['point_code']}:{row['part_id']}"
        if _has_open_task(engine, token):
            continue
        if _create_auto_task(engine, author_id, token,
                             f"Замена: {row['name']} ({row['point_name']})",
                             f"{page_links.mention(row['page_id'], row['name'])} — срок замены "
                             f"истёк {due}. Проведите замену и внесите запись в журнал ТО.",
                             "urgent"):
            created["replacement"] += 1
    return created


# --- QR-код карточки запчасти ----------------------------------------------- #


def part_qr_text(part: dict[str, Any]) -> str:
    """Текст, который кодирует QR: узнаваемые реквизиты запчасти."""
    lines = [str(part.get("name") or "Запчасть")]
    if part.get("model"):
        lines.append(f"Модель: {part['model']}")
    if part.get("article"):
        lines.append(f"Артикул: {part['article']}")
    if part.get("replacement_interval_days"):
        lines.append(f"Замена: каждые {part['replacement_interval_days']} дн.")
    lines.append(f"Мин. остаток: {int(part.get('min_stock') or 0)}")
    return "\n".join(lines)


def part_qr_svg(part: dict[str, Any]) -> bytes:
    """QR-код в SVG. Импорт `qrcode` ленивый: сайт поднимается и без пакета."""
    import io

    import qrcode
    import qrcode.image.svg

    image = qrcode.make(part_qr_text(part), image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue()


# --- HTTP ------------------------------------------------------------------- #


maintenance_router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])
locations_router = APIRouter(prefix="/api/locations", tags=["locations"])


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Некорректный JSON") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Ожидался JSON-объект")
    return payload


# Точки сети: чтение — любой сотрудник, правка — старшие (senior/mentor) + CSRF.


@locations_router.get("")
def list_locations(_: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Справочник точек: из него строятся динамические колонки дашборда."""
    return {"locations": all_locations(engine)}


@locations_router.post("", status_code=201)
async def add_location(request: Request,
                       _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    payload = await _json_object(request)
    if set(payload) - {"code", "name", "is_active"}:
        raise HTTPException(status_code=422, detail="Неизвестные поля точки")
    try:
        code = create_location(engine, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "code": code, "locations": all_locations(engine)}


@locations_router.patch("/{code}")
async def edit_location(code: str, request: Request,
                        _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    payload = await _json_object(request)
    if set(payload) - {"name", "is_active"}:
        raise HTTPException(status_code=422, detail="Неизвестные поля точки")
    try:
        location = update_location(engine, code, payload)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "location": location, "locations": all_locations(engine)}


@locations_router.delete("/{code}")
def remove_location(code: str,
                    _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    try:
        delete_location(engine, code)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"ok": True, "locations": all_locations(engine)}


# Категории ТО: правятся в панели управления, читаются дашбордом.


@maintenance_router.get("/categories")
def list_categories(_: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return {"categories": all_categories(engine)}


@maintenance_router.post("/categories", status_code=201)
async def add_category(request: Request,
                       _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    payload = await _json_object(request)
    if set(payload) - {"label", "color"}:
        raise HTTPException(status_code=422, detail="Неизвестные поля категории")
    try:
        code = create_category(engine, payload.get("label"), payload.get("color"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "code": code, "categories": all_categories(engine)}


@maintenance_router.patch("/categories/{code}")
async def edit_category(code: str, request: Request,
                        _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    payload = await _json_object(request)
    if set(payload) - {"label", "color"}:
        raise HTTPException(status_code=422, detail="Неизвестные поля категории")
    try:
        category = update_category(engine, code, payload.get("label"), payload.get("color"))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "category": category, "categories": all_categories(engine)}


@maintenance_router.delete("/categories/{code}")
def remove_category(code: str,
                    _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    try:
        delete_category(engine, code)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "categories": all_categories(engine)}


# Каталог запчастей: карточками управляет База знаний, здесь — чтение и QR.


@maintenance_router.get("/parts")
def list_equipment_parts(category: str | None = None,
                         _: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return {"parts": list_parts(engine, category)}


@maintenance_router.get("/parts/{part_id}/qr.svg")
def part_qr(part_id: int, _: dict[str, Any] = Depends(require_user)) -> Response:
    """QR-код запчасти (SVG) для карточки в Базе знаний."""
    part = get_part(engine, part_id)
    if part is None:
        raise HTTPException(status_code=404, detail="Запчасть не найдена")
    try:
        svg = part_qr_svg(part)
    except ImportError as error:
        raise HTTPException(status_code=501, detail="QR-код недоступен: не установлен пакет qrcode") from error
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "private, max-age=86400"})


# Остатки на точках: колонки строятся из справочника точек.


@maintenance_router.get("/stock")
def get_stock(_: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return stock_matrix(engine)


@maintenance_router.patch("/stock")
async def patch_stock(request: Request,
                      _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    """Ставит остаток; при падении до минимума сразу создаётся задача на закупку."""
    payload = await _json_object(request)
    if set(payload) - {"point_code", "part_id", "quantity"}:
        raise HTTPException(status_code=422, detail="Неизвестные поля остатка")
    try:
        row = set_stock(engine, payload.get("point_code"), payload.get("part_id"),
                        payload.get("quantity"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    automation = run_automation(engine)
    matrix = stock_matrix(engine)
    return {"ok": True, "stock": row, "matrix": matrix, "automation": automation}


# Журнал замен.


@maintenance_router.get("/logs")
def get_logs(point_code: str | None = None, part_id: int | None = None, limit: int = 200,
             _: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return {"items": list_logs(engine, point_code, part_id, limit), "stats": logs_stats(engine)}


@maintenance_router.post("/logs", status_code=201)
async def add_log(request: Request, user: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    payload = await _json_object(request)
    if set(payload) - {"point_code", "part_id", "replaced_at", "next_due_date", "comment", "photo_url"}:
        raise HTTPException(status_code=422, detail="Неизвестные поля записи")
    try:
        log_id = create_log(engine, user, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "id": log_id, "items": list_logs(engine), "stats": logs_stats(engine),
            "overview": overview(engine)}


@maintenance_router.delete("/logs/{log_id}")
def remove_log(log_id: int,
               _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    try:
        delete_log(engine, log_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"ok": True, "items": list_logs(engine), "stats": logs_stats(engine)}


# Обзор обслуживания по точкам (заодно прогоняет автоматизацию задач).


@maintenance_router.get("/overview")
def get_overview(_: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    run_automation(engine)
    return {"ok": True, **overview(engine)}
