"""База знаний (Notion-lite): страницы и связанные справочники зерна и напитков.

Модуль изолирован: всё живёт в `APIRouter(prefix="/api/kb")`, схема создаётся
идемпотентно при старте (`initialize_schema`), поэтому `app.py` только
подключает роутер — как это сделано для чата (`bot/web/chat.py`).

Модель данных:

* `kb_pages` — дерево страниц (Notion-lite): `parent_id` строит иерархию,
  `category` делит базу на разделы (`beans`, `drinks`, `wiki`, `standards`),
  `content_json` хранит тело страницы (Markdown или JSON-строку).
* `coffee_beans` — справочник зерна, привязанный к странице (`page_id`);
  `roast_type` хранит один или несколько типов обжарки через запятую (`espresso`,
  `filter`, `omni`).
* `menu_drinks` — справочник напитков, привязанный к странице (`page_id`); подача
  хранится двумя полями (`serve_here`, `serve_takeaway`) с фото в `data/kb/photos`.

Для страниц категорий `beans`/`drinks` запись справочника создаётся и
досинхронизируется автоматически из полей страницы (см. `_upsert_bean` /
`_upsert_drink`; поля можно прислать вложенно в `bean`/`drink` или плоско).

Доступ: чтение — любой активный сотрудник (`require_user`), запись — старшие
роли (`require_manager_csrf`: senior/mentor + CSRF) — тот же уровень, что и у
публикации анонсов.
"""

from __future__ import annotations

import io
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.concurrency import run_in_threadpool

from bot.database.models import engine
from bot.web.deps import require_manager_csrf, require_user

REPO_ROOT = Path(__file__).resolve().parents[2]

kb_router = APIRouter(prefix="/api/kb", tags=["knowledge-base"])
# Псевдоним, чтобы имя совпадало с ТЗ.
router = kb_router

# Разделы базы знаний (совпадает с kb_pages.category). Порядок задаёт оглавление
# в интерфейсе: справка (вики/стандарты) идёт выше справочников напитков и зерна.
CATEGORY_KEYS = ("wiki", "standards", "beans", "drinks")
# Тип обжарки зерна (coffee_beans.roast_type).
BEAN_ROAST_TYPES = ("espresso", "filter", "omni")
# Категории меню напитков (menu_drinks.category).
DRINK_CATEGORIES = ("черный кофе", "кофе с молоком", "чай", "не кофе", "авторский")

MAX_TITLE = 200
MAX_SLUG = 120
MAX_TEXT = 20000

# Каталог фото подач напитков (не в git) — как аватары, чат и фото оценок.
_KB_PHOTOS_DIR: Path | None = None
KB_PHOTO_MAX_BYTES = 8 * 1024 * 1024
KB_PHOTO_MAX_SIDE = 1600
KB_PHOTO_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.webp$")
KB_PHOTO_URL_RE = re.compile(r"^/api/kb/photos/[0-9a-f]{32}\.webp$")

# Транслитерация кириллицы для автослага — тот же набор, что в announcements.py.
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def _add_column_if_missing(connection: Any, table: str, column: str, definition: str) -> None:
    """`ALTER TABLE ... ADD COLUMN`, если колонки ещё нет.

    SQLite не поддерживает `ADD COLUMN IF NOT EXISTS`, поэтому ретрофит (подачи
    напитка и их фото) добавляем по факту наличия — идемпотентно при каждом старте.
    """
    existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def initialize_schema(engine: Engine) -> None:
    """Создаёт таблицы базы знаний (идемпотентно; вызывается на старте сервиса)."""
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS kb_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER REFERENCES kb_pages(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                slug TEXT UNIQUE,
                category TEXT NOT NULL,
                content_json TEXT,
                author_id INTEGER REFERENCES users(id),
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS coffee_beans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id INTEGER UNIQUE REFERENCES kb_pages(id) ON DELETE SET NULL,
                name TEXT NOT NULL,
                roast_type TEXT NOT NULL,
                pantone_color TEXT,
                description TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS menu_drinks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id INTEGER UNIQUE REFERENCES kb_pages(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                ttk TEXT,
                ingredients TEXT,
                description TEXT,
                serve_rules TEXT,
                serve_here TEXT,
                serve_takeaway TEXT,
                serve_here_photo TEXT,
                serve_takeaway_photo TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_kb_pages_category
            ON kb_pages (category, title)
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_kb_pages_parent
            ON kb_pages (parent_id)
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_coffee_beans_name
            ON coffee_beans (name)
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_menu_drinks_category
            ON menu_drinks (category, title)
        """))
        # Ретрофит существующих баз: колонки подач и их фото добавляем идемпотентно,
        # а прежние «Правила подачи» переносим в «Подача здесь», чтобы не потерять
        # уже заполненные данные.
        for column in ("serve_here", "serve_takeaway", "serve_here_photo", "serve_takeaway_photo"):
            _add_column_if_missing(connection, "menu_drinks", column, "TEXT")
        connection.execute(text("""
            UPDATE menu_drinks SET serve_here = serve_rules
            WHERE serve_here IS NULL AND serve_rules IS NOT NULL
        """))


def _translit(value: Any) -> str:
    return "".join(TRANSLIT.get(character, character) for character in str(value or "").lower())


def _clean_slug(value: Any) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")
    return base[:MAX_SLUG]


def _slugify(value: Any) -> str:
    return _clean_slug(_translit(value)) or "page"


def _unique_slug(engine: Engine, base: str, exclude_id: int | None = None) -> str:
    """Подбирает свободный slug: base, base-2, base-3 … (уникальность в kb_pages)."""
    base = _clean_slug(base) or "page"
    with engine.connect() as connection:
        slug, index = base, 2
        while True:
            row = connection.execute(
                text("SELECT id FROM kb_pages WHERE slug = :slug"), {"slug": slug}
            ).first()
            if row is None or (exclude_id is not None and int(row[0]) == exclude_id):
                return slug
            slug = f"{base[: MAX_SLUG - 6]}-{index}"
            index += 1


def _clean(value: Any, limit: int) -> str:
    return str(value).strip()[:limit] if value is not None else ""


def _clean_text(value: Any, limit: int = MAX_TEXT) -> str | None:
    text_value = _clean(value, limit)
    return text_value or None


def _normalize_roast_types(value: Any) -> str:
    """Типы обжарки зерна можно прислать списком или строкой через запятую.

    Наружу отдаём CSV (совместимо с прежними одиночными значениями), а сам
    список валидируем и чистим от дублей, сохраняя порядок.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        raw = [str(item).strip() for item in value]
    else:
        raw = [part.strip() for part in str(value).split(",")]
    values = [item for item in raw if item]
    for item in values:
        if item not in BEAN_ROAST_TYPES:
            raise ValueError("Тип обжарки: " + ", ".join(BEAN_ROAST_TYPES))
    return ",".join(dict.fromkeys(values))


def _clean_photo(value: Any) -> str | None:
    """Ссылка на фото подачи: принимаем только собственный URL, иначе — ошибка."""
    if value in (None, ""):
        return None
    if isinstance(value, str) and KB_PHOTO_URL_RE.match(value.strip()):
        return value.strip()
    raise ValueError("Некорректная ссылка на фото подачи")


def kb_photos_dir() -> Path:
    """Каталог фото подач напитков. Создаётся один раз за процесс."""
    global _KB_PHOTOS_DIR
    if _KB_PHOTOS_DIR is None:
        path = Path(os.getenv("NEFT_KB_PHOTOS_DIR") or (REPO_ROOT / "data" / "kb" / "photos"))
        path.mkdir(parents=True, exist_ok=True)
        _KB_PHOTOS_DIR = path
    return _KB_PHOTOS_DIR


def store_kb_photo(data: bytes) -> str | None:
    """Сжимает фото подачи в WebP (Pillow). Возвращает URL или None, если не изображение."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            if max(image.size) > KB_PHOTO_MAX_SIDE:
                image.thumbnail((KB_PHOTO_MAX_SIDE, KB_PHOTO_MAX_SIDE), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="WEBP", quality=82, method=4)
    except Exception:  # noqa: BLE001 — битый файл/не изображение → None
        return None
    name = f"{uuid.uuid4().hex}.webp"
    (kb_photos_dir() / name).write_bytes(buffer.getvalue())
    return f"/api/kb/photos/{name}"


def _clean_int(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Некорректный идентификатор") from error


def _dump_content(value: Any) -> str | None:
    """content_json принимаем как готовую строку или как JSON-объект."""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:MAX_TEXT] or None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ValueError("content_json должен быть строкой или JSON-объектом") from error


def _flag(value: Any, default: int = 1) -> int:
    if value is None:
        return default
    return 1 if bool(value) else 0


def _ref_source(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """Поля справочника можно прислать вложенно (bean/drink) или плоско."""
    nested = payload.get(key)
    return nested if isinstance(nested, dict) else payload


def _page_exists(connection: Any, page_id: int) -> bool:
    return connection.execute(
        text("SELECT 1 FROM kb_pages WHERE id = :id"), {"id": page_id}
    ).first() is not None


def _upsert_bean(connection: Any, page_id: int, default_name: str, payload: dict[str, Any]) -> None:
    """Создаёт или обновляет запись зерна, привязанную к странице."""
    source = _ref_source(payload, "bean")
    current = connection.execute(
        text("SELECT * FROM coffee_beans WHERE page_id = :page_id"), {"page_id": page_id}
    ).mappings().first()

    name = _clean(source.get("name"), MAX_TITLE) or (current["name"] if current else default_name)
    if not name:
        raise ValueError("Название зерна обязательно")
    roast_type = _normalize_roast_types(source.get("roast_type")) \
        or (current["roast_type"] if current else "espresso")

    if source.get("pantone_color") is not None or current is None:
        pantone_color = _clean_text(source.get("pantone_color"), 60)
    else:
        pantone_color = current["pantone_color"]
    if source.get("description") is not None or current is None:
        description = _clean_text(source.get("description"))
    else:
        description = current["description"]
    is_active = _flag(source.get("is_active"), current["is_active"] if current else 1)

    values = {
        "page_id": page_id, "name": name, "roast_type": roast_type,
        "pantone_color": pantone_color, "description": description, "is_active": is_active,
    }
    if current:
        connection.execute(text("""
            UPDATE coffee_beans
            SET name = :name, roast_type = :roast_type, pantone_color = :pantone_color,
                description = :description, is_active = :is_active
            WHERE page_id = :page_id
        """), values)
    else:
        connection.execute(text("""
            INSERT INTO coffee_beans (page_id, name, roast_type, pantone_color, description, is_active)
            VALUES (:page_id, :name, :roast_type, :pantone_color, :description, :is_active)
        """), values)


def _upsert_drink(connection: Any, page_id: int, default_title: str, payload: dict[str, Any]) -> None:
    """Создаёт или обновляет запись напитка, привязанную к странице."""
    source = _ref_source(payload, "drink")
    current = connection.execute(
        text("SELECT * FROM menu_drinks WHERE page_id = :page_id"), {"page_id": page_id}
    ).mappings().first()

    title = _clean(source.get("title"), MAX_TITLE) or (current["title"] if current else default_title)
    if not title:
        raise ValueError("Название напитка обязательно")
    category = _clean(source.get("category"), 60) or (current["category"] if current else "")
    if category not in DRINK_CATEGORIES:
        raise ValueError("Категория напитка: " + ", ".join(DRINK_CATEGORIES))

    def pick(field: str) -> str | None:
        if source.get(field) is not None or current is None:
            return _clean_text(source.get(field))
        return current[field]

    # Фото подач: присланный URL валидируем, отсутствие поля — сохраняем как было,
    # явные null/"" очищают фото.
    photos: dict[str, str | None] = {}
    for key in ("serve_here_photo", "serve_takeaway_photo"):
        photos[key] = _clean_photo(source.get(key)) if key in source else (current[key] if current else None)

    values = {
        "page_id": page_id, "title": title, "category": category,
        "ttk": pick("ttk"), "ingredients": pick("ingredients"),
        "description": pick("description"),
        "serve_here": pick("serve_here"), "serve_takeaway": pick("serve_takeaway"),
        "serve_here_photo": photos["serve_here_photo"],
        "serve_takeaway_photo": photos["serve_takeaway_photo"],
        "is_active": _flag(source.get("is_active"), current["is_active"] if current else 1),
    }
    if current:
        connection.execute(text("""
            UPDATE menu_drinks
            SET title = :title, category = :category, ttk = :ttk, ingredients = :ingredients,
                description = :description, serve_here = :serve_here, serve_takeaway = :serve_takeaway,
                serve_here_photo = :serve_here_photo, serve_takeaway_photo = :serve_takeaway_photo,
                is_active = :is_active
            WHERE page_id = :page_id
        """), values)
    else:
        connection.execute(text("""
            INSERT INTO menu_drinks
                (page_id, title, category, ttk, ingredients, description, serve_here,
                 serve_takeaway, serve_here_photo, serve_takeaway_photo, is_active)
            VALUES (:page_id, :title, :category, :ttk, :ingredients, :description, :serve_here,
                    :serve_takeaway, :serve_here_photo, :serve_takeaway_photo, :is_active)
        """), values)


def _page_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Лёгкое представление страницы для списка (без тела content_json)."""
    reference: dict[str, Any] | None = None
    if row["category"] == "beans" and row.get("bean_name") is not None:
        reference = {
            "type": "beans", "name": row["bean_name"], "roast_type": row["roast_type"],
            "pantone_color": row["pantone_color"], "is_active": row["bean_active"],
        }
    elif row["category"] == "drinks" and row.get("drink_category") is not None:
        reference = {
            "type": "drinks", "category": row["drink_category"], "is_active": row["drink_active"],
        }
    return {
        "id": row["id"], "parent_id": row["parent_id"], "title": row["title"],
        "slug": row["slug"], "category": row["category"], "author_id": row["author_id"],
        "author_name": row["author_name"], "updated_at": row["updated_at"], "reference": reference,
    }


def list_pages(engine: Engine, category: str | None = None) -> list[dict[str, Any]]:
    """Список страниц с фильтрацией по разделу и краткой сводкой справочника."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT p.id, p.parent_id, p.title, p.slug, p.category, p.author_id, p.updated_at,
                   u.display_name AS author_name,
                   b.name AS bean_name, b.roast_type, b.pantone_color, b.is_active AS bean_active,
                   d.category AS drink_category, d.is_active AS drink_active
            FROM kb_pages p
            LEFT JOIN users u ON u.id = p.author_id
            LEFT JOIN coffee_beans b ON b.page_id = p.id
            LEFT JOIN menu_drinks d ON d.page_id = p.id
            WHERE (:category IS NULL OR p.category = :category)
            ORDER BY p.category ASC, p.title COLLATE NOCASE ASC
        """), {"category": category}).mappings().all()
    return [_page_summary(dict(row)) for row in rows]


def get_page(engine: Engine, page_id: int) -> dict[str, Any] | None:
    """Детальная страница вместе со связанной записью справочника и счётчиком детей."""
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT p.*, u.display_name AS author_name, u.name AS author_login
            FROM kb_pages p
            LEFT JOIN users u ON u.id = p.author_id
            WHERE p.id = :id
        """), {"id": page_id}).mappings().first()
        if row is None:
            return None
        page = dict(row)
        if page["category"] == "beans":
            bean = connection.execute(
                text("SELECT * FROM coffee_beans WHERE page_id = :page_id"), {"page_id": page_id}
            ).mappings().first()
            page["bean"] = dict(bean) if bean else None
            page["drink"] = None
        elif page["category"] == "drinks":
            drink = connection.execute(
                text("SELECT * FROM menu_drinks WHERE page_id = :page_id"), {"page_id": page_id}
            ).mappings().first()
            page["drink"] = dict(drink) if drink else None
            page["bean"] = None
        else:
            page["bean"] = None
            page["drink"] = None
        page["children"] = int(connection.execute(
            text("SELECT COUNT(*) FROM kb_pages WHERE parent_id = :id"), {"id": page_id}
        ).scalar_one())
    return page


def create_page(engine: Engine, author_id: int, payload: dict[str, Any]) -> int:
    """Создаёт страницу; для beans/drinks сразу заводит запись справочника."""
    if not isinstance(payload, dict):
        raise ValueError("Ожидался объект страницы")
    category = _clean(payload.get("category"), 32)
    if category not in CATEGORY_KEYS:
        raise ValueError("Раздел: " + ", ".join(CATEGORY_KEYS))
    title = _clean(payload.get("title"), MAX_TITLE)
    if not title:
        raise ValueError("Заголовок обязателен")
    content = _dump_content(payload.get("content_json"))
    parent_id = _clean_int(payload.get("parent_id"))
    slug = _unique_slug(engine, _clean_slug(payload.get("slug")) or _slugify(title))

    with engine.begin() as connection:
        if parent_id is not None and not _page_exists(connection, parent_id):
            raise ValueError("Родительская страница не найдена")
        result = connection.execute(text("""
            INSERT INTO kb_pages (parent_id, title, slug, category, content_json, author_id)
            VALUES (:parent_id, :title, :slug, :category, :content_json, :author_id)
        """), {
            "parent_id": parent_id, "title": title, "slug": slug,
            "category": category, "content_json": content, "author_id": author_id,
        })
        page_id = int(result.lastrowid or 0)
        if category == "beans":
            _upsert_bean(connection, page_id, title, payload)
        elif category == "drinks":
            _upsert_drink(connection, page_id, title, payload)
    return page_id


def update_page(engine: Engine, page_id: int, payload: dict[str, Any]) -> bool:
    """Правка страницы: поля, дерево и синхронизация связанного справочника."""
    if not isinstance(payload, dict):
        raise ValueError("Ожидался объект страницы")
    with engine.connect() as connection:
        current = connection.execute(
            text("SELECT * FROM kb_pages WHERE id = :id"), {"id": page_id}
        ).mappings().first()
    if current is None:
        return False

    category = current["category"]
    if payload.get("category") is not None:
        category = _clean(payload.get("category"), 32)
        if category not in CATEGORY_KEYS:
            raise ValueError("Раздел: " + ", ".join(CATEGORY_KEYS))
    title = _clean(payload.get("title"), MAX_TITLE) or current["title"]

    if payload.get("slug"):
        slug = _unique_slug(engine, _clean_slug(payload["slug"]) or _slugify(title), exclude_id=page_id)
    else:
        slug = current["slug"] or _unique_slug(engine, _slugify(title), exclude_id=page_id)

    content = _dump_content(payload["content_json"]) if "content_json" in payload else current["content_json"]

    parent_id = current["parent_id"]
    if "parent_id" in payload:
        parent_id = _clean_int(payload.get("parent_id"))

    with engine.begin() as connection:
        if parent_id is not None and (parent_id == page_id or not _page_exists(connection, parent_id)):
            raise ValueError("Некорректная родительская страница")

        connection.execute(text("""
            UPDATE kb_pages
            SET parent_id = :parent_id, title = :title, slug = :slug, category = :category,
                content_json = :content_json, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id
        """), {
            "parent_id": parent_id, "title": title, "slug": slug,
            "category": category, "content_json": content, "id": page_id,
        })

        # Синхронизация справочника под (возможно, новую) категорию.
        if category == "beans":
            _upsert_bean(connection, page_id, title, payload)
            connection.execute(text("DELETE FROM menu_drinks WHERE page_id = :page_id"), {"page_id": page_id})
        elif category == "drinks":
            _upsert_drink(connection, page_id, title, payload)
            connection.execute(text("DELETE FROM coffee_beans WHERE page_id = :page_id"), {"page_id": page_id})
        else:
            connection.execute(text("DELETE FROM coffee_beans WHERE page_id = :page_id"), {"page_id": page_id})
            connection.execute(text("DELETE FROM menu_drinks WHERE page_id = :page_id"), {"page_id": page_id})
    return True


def delete_page(engine: Engine, page_id: int) -> bool:
    """Удаляет страницу, связанные записи справочника; детей поднимает на уровень выше."""
    with engine.begin() as connection:
        current = connection.execute(
            text("SELECT parent_id FROM kb_pages WHERE id = :id"), {"id": page_id}
        ).mappings().first()
        if current is None:
            return False
        connection.execute(text("DELETE FROM coffee_beans WHERE page_id = :page_id"), {"page_id": page_id})
        connection.execute(text("DELETE FROM menu_drinks WHERE page_id = :page_id"), {"page_id": page_id})
        connection.execute(
            text("UPDATE kb_pages SET parent_id = :parent WHERE parent_id = :id"),
            {"parent": current["parent_id"], "id": page_id},
        )
        connection.execute(text("DELETE FROM kb_pages WHERE id = :id"), {"id": page_id})
    return True


# --- HTTP ------------------------------------------------------------------- #


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Некорректный JSON") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Ожидался JSON-объект")
    return payload


@kb_router.get("/pages")
def kb_list_pages(category: str | None = None, _: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Список страниц. Необязательный фильтр `?category=beans|drinks|wiki|standards`."""
    if category is not None and category not in CATEGORY_KEYS:
        raise HTTPException(status_code=422, detail="Неизвестный раздел")
    return {"items": list_pages(engine, category), "categories": list(CATEGORY_KEYS)}


@kb_router.get("/pages/{page_id}")
def kb_page_detail(page_id: int, _: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Детальная страница со связанной записью справочника (bean или drink)."""
    page = get_page(engine, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="Страница не найдена")
    return page


@kb_router.post("/pages")
async def kb_create_page(request: Request, user: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    """Создаёт страницу. Для beans/drinks автоматически заводит запись справочника."""
    payload = await _json_object(request)
    try:
        page_id = create_page(engine, int(user["id"]), payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, "id": page_id}


@kb_router.put("/pages/{page_id}")
async def kb_update_page(page_id: int, request: Request,
                         _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    """Правка страницы и синхронизация полей связанного справочника."""
    payload = await _json_object(request)
    try:
        updated = update_page(engine, page_id, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if not updated:
        raise HTTPException(status_code=404, detail="Страница не найдена")
    return {"ok": True}


@kb_router.delete("/pages/{page_id}")
def kb_delete_page(page_id: int, _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    """Удаляет страницу и связанные записи справочника."""
    if not delete_page(engine, page_id):
        raise HTTPException(status_code=404, detail="Страница не найдена")
    return {"ok": True}


@kb_router.get("/photos/{filename}")
def kb_photo(filename: str, _: dict[str, Any] = Depends(require_user)):
    """Отдаёт фото подачи напитка. Имя из URL пропускаем только собственного формата."""
    if not KB_PHOTO_FILENAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="Фото не найдено")
    path = kb_photos_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Фото не найдено")
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=31536000"},
    )


@kb_router.post("/photos")
async def kb_upload_photo(request: Request,
                          _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    """Тело запроса — сам файл изображения (без multipart), как у фото оценок."""
    data = await request.body()
    if not data or len(data) > KB_PHOTO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Файл пустой или больше 8 МБ")
    url = await run_in_threadpool(store_kb_photo, data)
    if not url:
        raise HTTPException(status_code=422, detail="Не удалось прочитать изображение")
    return {"ok": True, "url": url}

