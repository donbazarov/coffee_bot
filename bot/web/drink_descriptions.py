"""Описание напитков: лента смен, рецепты и викторины «где чей латте-арт / мем».

Модуль изолирован по образцу `chat.py` и `knowledge_base.py`: всё живёт в
`APIRouter(prefix="/api/drink-descriptions")`, схема создаётся идемпотентно при
старте (`initialize_schema`), поэтому `app.py` только подключает роутер.

Что внутри:
* **Лента** — таблица `drink_descriptions` (пост смены: точка, автор, напарник,
  заметка, фото латте-арта и мема) и `drink_description_items` (напитки поста,
  поддержка нескольких через «+»). Первый напиток обязательно «черный кофе»,
  молочный идёт только следующим — чай/авторский/не-кофе запрещены на уровне
  логики (белый список категорий).
* **Смена** — точка и напарник определяются автоматически по графику
  (`calendar_service.active_staff`), с фоллбэком на ручной выбор «вне смены».
* **Викторины** — `drink_description_quizzes` + `drink_quiz_options` +
  `drink_quiz_votes`: одна викторина на тип (латте-арт / мем), варианты —
  перестановки авторóв по загруженным фото («1. Настя · 2. Польза»), голоса
  видны всей команде после ответа.
* **Изображения** — Pillow «на лету» в WebP, файлы кладутся в
  `data/drink_descriptions/photos` (env `NEFT_DRINK_PHOTOS_DIR`), в базу пишется
  только путь. Раздаются авторизованным пользователям роутом.
* **Опыт** — публикация даёт XP в ветки «Команда» и «Сенсорика»; если среди
  напитков есть «кофе с молоком» и приложено фото латте-арта — ещё «Молоко».
  Верный ответ в викторине даёт XP в «Команду» (один раз на участника).
"""

from __future__ import annotations

import io
import itertools
import logging
import os
import random
import re
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine
from starlette.concurrency import run_in_threadpool

from bot.database.models import engine
from bot.web import gamification
from bot.web.calendar_service import active_staff, app_timezone
from bot.web.deps import require_csrf, require_user

logger = logging.getLogger("bot.web.drink_descriptions")

router = APIRouter(prefix="/api/drink-descriptions", tags=["drink-descriptions"])

REPO_ROOT = Path(__file__).resolve().parents[2]

# Торговые точки (совпадает с shift_types.point и app.SHIFT_POINTS) + фоллбэк.
POINT_VALUES = ("ДЕ", "УЯ", "другое")
# Категории напитков описания (совпадает с menu_drinks.category / DRINK_PARAMS).
BLACK_COFFEE = "черный кофе"
MILK_COFFEE = "кофе с молоком"
ITEM_CATEGORIES = (BLACK_COFFEE, MILK_COFFEE)

# Шкалы интерфейса (дискретные значения; валидируем белым списком, как DRINK_CATEGORIES).
BALANCE_LEVELS = ("низкая", "ниже среднего", "средняя", "выше среднего", "высокая")
BODY_LEVELS = ("легкое", "среднее", "плотное")
AFTERTASTE_LEVELS = ("короткое", "среднее", "долгое")
MILK_DROP_LEVELS = ("отсутствует", "небольшая", "большая")
BALANCE_SCORES = ("в молоко", "нейтральный", "в кофе")

# Типы викторин, привязанные к фото поста.
QUIZ_KINDS = ("latte_art", "meme")
QUIZ_QUESTIONS = {"latte_art": "Где чей латте-арт?", "meme": "Где чей мем?"}

# Опыт за действия модуля (ветки skill_branches.code).
XP_PUBLISH = {"team": 10, "sensory": 15}
XP_PUBLISH_MILK = {"milk": 20}
XP_QUIZ_CORRECT = {"team": 5}

MAX_NOTE = 2000
MAX_TEXT = 400
MIN_QUIZ_OPTIONS = 2
MAX_QUIZ_OPTIONS = 6
MAX_PHOTOS_PER_KIND = 6
DEFAULT_PAGE = 20
MAX_PAGE = 50

# Каталог фото постов (не в git) — как аватары, чат и фото оценок.
_PHOTOS_DIR: Path | None = None
MAX_PHOTO_BYTES = 8 * 1024 * 1024
PHOTO_MAX_SIDE = 1600
PHOTO_QUALITY = 82
PHOTO_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.webp$")
PHOTO_URL_PREFIX = "/api/drink-descriptions/photos/"
PHOTO_URL_RE = re.compile(r"^/api/drink-descriptions/photos/[0-9a-f]{32}\.webp$")


def _add_column_if_missing(connection: Connection, table: str, column: str, definition: str) -> None:
    """`ALTER TABLE ... ADD COLUMN`, если колонки ещё нет.

    SQLite не поддерживает `ADD COLUMN IF NOT EXISTS`, поэтому ретрофит новых
    полей (напиток из базы знаний, характер баланса) докатываем по факту
    наличия — идемпотентно при каждом старте.
    """
    existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def photos_dir() -> Path:
    """Каталог фото постов. Создаётся один раз за процесс."""
    global _PHOTOS_DIR
    if _PHOTOS_DIR is None:
        path = Path(os.getenv("NEFT_DRINK_PHOTOS_DIR") or (REPO_ROOT / "data" / "drink_descriptions" / "photos"))
        path.mkdir(parents=True, exist_ok=True)
        _PHOTOS_DIR = path
    return _PHOTOS_DIR


def store_photo(data: bytes) -> str | None:
    """Сжимает фото поста в WebP (Pillow). Возвращает URL или None, если не картинка."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            if max(image.size) > PHOTO_MAX_SIDE:
                image.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="WEBP", quality=PHOTO_QUALITY, method=4)
    except Exception:  # noqa: BLE001 — битый файл/не изображение → None
        return None
    name = f"{uuid.uuid4().hex}.webp"
    (photos_dir() / name).write_bytes(buffer.getvalue())
    return f"{PHOTO_URL_PREFIX}{name}"


def initialize_schema(db: Engine) -> None:
    """Идемпотентно создаёт таблицы модуля (вызывается на старте сервиса)."""
    with db.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS drink_descriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id INTEGER NOT NULL REFERENCES users(id),
                partner_id INTEGER REFERENCES users(id),
                point TEXT NOT NULL,
                shift_date DATE NOT NULL,
                note TEXT,
                latte_art_photo TEXT,
                meme_photo TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS drink_description_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description_id INTEGER NOT NULL REFERENCES drink_descriptions(id) ON DELETE CASCADE,
                position INTEGER NOT NULL DEFAULT 0,
                category TEXT NOT NULL,
                bean_id INTEGER REFERENCES coffee_beans(id),
                recipe_dose REAL,
                recipe_yield REAL,
                recipe_water REAL,
                recipe_time REAL,
                recipe_temp REAL,
                is_espresso_recipe INTEGER NOT NULL DEFAULT 0,
                balance_acidity TEXT,
                balance_sweetness TEXT,
                balance_bitterness TEXT,
                balance_score TEXT,
                body_intensity TEXT,
                body_texture TEXT,
                bouquet TEXT,
                aftertaste_duration TEXT,
                aftertaste_quality TEXT,
                milk_drop TEXT
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS drink_description_quizzes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description_id INTEGER NOT NULL REFERENCES drink_descriptions(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                question TEXT NOT NULL,
                photo_url TEXT NOT NULL,
                correct_option_id INTEGER,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS drink_quiz_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_id INTEGER NOT NULL REFERENCES drink_description_quizzes(id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                is_correct INTEGER NOT NULL DEFAULT 0
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS drink_quiz_votes (
                quiz_id INTEGER NOT NULL REFERENCES drink_description_quizzes(id) ON DELETE CASCADE,
                option_id INTEGER NOT NULL REFERENCES drink_quiz_options(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (quiz_id, user_id)
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS drink_description_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                description_id INTEGER NOT NULL REFERENCES drink_descriptions(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                url TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0
            )
        """))
        # Ретрофит: автор фото (кто из пары смены сделал снимок).
        _add_column_if_missing(connection, "drink_description_photos", "author_id",
                               "INTEGER REFERENCES users(id)")
        # Ретрофит: напиток из базы знаний и текстовый характер баланса.
        _add_column_if_missing(connection, "drink_description_items", "drink_id",
                               "INTEGER REFERENCES menu_drinks(id)")
        _add_column_if_missing(connection, "drink_description_items", "balance_acidity_text", "TEXT")
        _add_column_if_missing(connection, "drink_description_items", "balance_sweetness_text", "TEXT")
        _add_column_if_missing(connection, "drink_description_items", "balance_bitterness_text", "TEXT")
        for ddl in (
            "CREATE INDEX IF NOT EXISTS idx_drink_desc_point ON drink_descriptions (point, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_drink_desc_author ON drink_descriptions (author_id)",
            "CREATE INDEX IF NOT EXISTS idx_drink_items_desc ON drink_description_items (description_id, position)",
            "CREATE INDEX IF NOT EXISTS idx_drink_quiz_desc ON drink_description_quizzes (description_id)",
            "CREATE INDEX IF NOT EXISTS idx_drink_quiz_options ON drink_quiz_options (quiz_id, position)",
            "CREATE INDEX IF NOT EXISTS idx_drink_photos_desc ON drink_description_photos (description_id, kind, position)",
        ):
            connection.execute(text(ddl))


# --- Хелперы валидации ------------------------------------------------------- #


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Некорректный JSON") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Ожидался JSON-объект")
    return payload


def _choice(value: Any, allowed: tuple[str, ...], label: str) -> str | None:
    """Необязательное значение из белого списка."""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or value not in allowed:
        raise HTTPException(status_code=422, detail=f"Некорректное значение поля «{label}»")
    return value


def _optional_number(value: Any, label: str) -> float | None:
    """Необязательное числовое поле рецепта (граммы, секунды, °C)."""
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(status_code=422, detail=f"Поле «{label}» должно быть числом")
    number = float(value)
    if number < 0 or number > 100000:
        raise HTTPException(status_code=422, detail=f"Поле «{label}» вне допустимого диапазона")
    return round(number, 2)


def _optional_text(payload: dict[str, Any], key: str, label: str, limit: int = MAX_TEXT) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"Поле «{label}» должно быть текстом")
    cleaned = value.strip()
    return cleaned[:limit] or None


def _optional_reference_id(payload: dict[str, Any], key: str, label: str) -> int | None:
    value = payload.get(key)
    if value in (None, "", 0):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HTTPException(status_code=422, detail=f"Некорректная ссылка на «{label}»")
    return value


def _photo_url(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str) and PHOTO_URL_RE.match(value.strip()):
        return value.strip()
    raise HTTPException(status_code=422, detail="Некорректная ссылка на фото")


def _photo_entries(value: Any, label: str, default_author: int) -> list[dict[str, Any]]:
    """Нормализует фото: элемент — URL или объект `{url, author_id}`.

    Автор по умолчанию — автор поста. Привязка к бариста нужна для викторины
    «где чей латте-арт / мем»: правильный ответ — порядок авторóв по фото.
    """
    if value in (None, "", []):
        return []
    items = value if isinstance(value, list) else [value]
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            url = _photo_url(item.get("url"))
            author_raw = item.get("author_id")
        else:
            url = _photo_url(item)
            author_raw = None
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            author = int(author_raw) if author_raw not in (None, "", 0) else int(default_author)
        except (TypeError, ValueError):
            author = int(default_author)
        entries.append({"url": url, "author_id": author})
    if len(entries) > MAX_PHOTOS_PER_KIND:
        raise HTTPException(status_code=422, detail=f"Слишком много фото: {label}")
    return entries


def _today() -> str:
    return datetime.now(app_timezone()).date().isoformat()


def _clean_shift_date(value: Any) -> str:
    if value in (None, ""):
        return _today()
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="Некорректная дата смены")
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Некорректная дата смены") from error


# --- Смена и сотрудники ------------------------------------------------------ #


def _active_staff_list(connection: Connection) -> list[dict[str, Any]]:
    """Активные сотрудники команды — для ручного выбора напарника вне смены."""
    rows = connection.execute(text("""
        SELECT id, COALESCE(display_name, name) AS name, role FROM users
        WHERE is_active = 1 AND role IN ('newbie', 'barista', 'senior', 'mentor')
        ORDER BY name
    """)).mappings().all()
    return [{"id": int(row["id"]), "name": row["name"], "role": row["role"]} for row in rows]


def current_shift(user_id: int) -> dict[str, Any]:
    """Точка и напарники текущего сотрудника по графику (с фоллбэком «вне смены»)."""
    now = datetime.now(app_timezone())
    shift_date = now.date().isoformat()
    point: str | None = None
    partners: list[dict[str, Any]] = []
    for candidate in ("ДЕ", "УЯ"):
        staff = active_staff(engine, candidate, now)
        if any(int(person["id"]) == int(user_id) for person in staff):
            point = candidate
            partners = [
                {"id": int(person["id"]), "name": person["name"], "role": person["role"], "on_shift": True}
                for person in staff if int(person["id"]) != int(user_id)
            ]
            break
    with engine.connect() as connection:
        staff_list = _active_staff_list(connection)
    partner_ids = {person["id"] for person in partners}
    candidates = list(partners) + [
        {**person, "on_shift": False} for person in staff_list if person["id"] not in partner_ids
    ]
    return {
        "point": point or "другое",
        "on_shift": point is not None,
        "shift_date": shift_date,
        "partners": partners,
        "candidates": candidates,
    }


# --- Валидация и запись ------------------------------------------------------ #


def _normalize_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Разбирает напитки поста. Первый обязан быть «черный кофе»; чай/авторский — запрещены."""
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(status_code=422, detail="Добавьте хотя бы один напиток")
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="Некорректный напиток")
        category = raw.get("category")
        if category not in ITEM_CATEGORIES:
            if isinstance(category, str) and category in ("чай", "авторский", "не кофе"):
                raise HTTPException(status_code=422, detail=f"Категория «{category}» не поддерживается")
            raise HTTPException(status_code=422, detail="Некорректная категория напитка")
        if index == 0 and category != BLACK_COFFEE:
            raise HTTPException(status_code=422, detail="Первый напиток должен быть «черный кофе»")
        items.append({
            "category": category,
            "drink_id": _optional_reference_id(raw, "drink_id", "Напиток"),
            "bean_id": _optional_reference_id(raw, "bean_id", "Зерно"),
            "recipe_dose": _optional_number(raw.get("recipe_dose"), "кофе на входе"),
            "recipe_yield": _optional_number(raw.get("recipe_yield"), "кофе на выходе"),
            "recipe_water": _optional_number(raw.get("recipe_water"), "вода"),
            "recipe_time": _optional_number(raw.get("recipe_time"), "время"),
            "recipe_temp": _optional_number(raw.get("recipe_temp"), "температура"),
            "is_espresso_recipe": bool(raw.get("is_espresso_recipe")),
            "balance_acidity": _choice(raw.get("balance_acidity"), BALANCE_LEVELS, "кислотность"),
            "balance_acidity_text": _optional_text(raw, "balance_acidity_text", "характер кислотности"),
            "balance_sweetness": _choice(raw.get("balance_sweetness"), BALANCE_LEVELS, "сладость"),
            "balance_sweetness_text": _optional_text(raw, "balance_sweetness_text", "характер сладости"),
            "balance_bitterness": _choice(raw.get("balance_bitterness"), BALANCE_LEVELS, "горечь"),
            "balance_bitterness_text": _optional_text(raw, "balance_bitterness_text", "характер горечи"),
            "balance_score": _choice(raw.get("balance_score"), BALANCE_SCORES, "баланс"),
            "body_intensity": _choice(raw.get("body_intensity"), BODY_LEVELS, "тело"),
            "body_texture": _optional_text(raw, "body_texture", "нюанс тела"),
            "bouquet": _optional_text(raw, "bouquet", "букет"),
            "aftertaste_duration": _choice(raw.get("aftertaste_duration"), AFTERTASTE_LEVELS, "послевкусие"),
            "aftertaste_quality": _optional_text(raw, "aftertaste_quality", "качество послевкусия"),
            "milk_drop": _choice(raw.get("milk_drop"), MILK_DROP_LEVELS, "молочная капля"),
        })
    # Молочный напиток с галочкой «рецепт как на эспрессо» копирует числа из эспрессо.
    espresso = next((item for item in items if item["category"] == BLACK_COFFEE), None)
    for item in items:
        if item["category"] == MILK_COFFEE and item["is_espresso_recipe"] and espresso:
            for field in ("recipe_dose", "recipe_yield", "recipe_time", "recipe_temp"):
                item[field] = espresso[field]
    return items


ITEM_COLUMNS = (
    "description_id", "position", "category", "drink_id", "bean_id", "recipe_dose", "recipe_yield",
    "recipe_water", "recipe_time", "recipe_temp", "is_espresso_recipe", "balance_acidity",
    "balance_acidity_text", "balance_sweetness", "balance_sweetness_text", "balance_bitterness",
    "balance_bitterness_text", "balance_score", "body_intensity",
    "body_texture", "bouquet", "aftertaste_duration", "aftertaste_quality", "milk_drop",
)


def _insert_items(connection: Connection, description_id: int, items: list[dict[str, Any]]) -> None:
    for index, item in enumerate(items):
        values = {"description_id": description_id, "position": index,
                  "is_espresso_recipe": 1 if item["is_espresso_recipe"] else 0}
        values.update({key: item[key] for key in ITEM_COLUMNS if key in item})
        placeholders = ", ".join(f":{column}" for column in ITEM_COLUMNS)
        connection.execute(text(
            f"INSERT INTO drink_description_items ({', '.join(ITEM_COLUMNS)})\n"
            f"VALUES ({placeholders})"
        ), values)


def _distinct_permutations(names: list[str]) -> list[tuple[str, ...]]:
    """Различные перестановки последовательности (мультимножество учитывается)."""
    return list(dict.fromkeys(itertools.permutations(names)))


def _quiz_options(name_by_id: dict[int, str], author_ids: list[int]) -> list[dict[str, Any]]:
    """Варианты викторины: перестановки авторóв по слотам фото.

    Текст — «1. Настя · 2. Польза», где номер — порядок изображения. Верный
    вариант — фактический порядок. Если перестановок слишком много, оставляем
    верную плюс случайную выборку остальных (верный присутствует всегда).
    """
    names = [name_by_id.get(int(author_id), "Бариста") for author_id in author_ids]
    permutations = _distinct_permutations(names)
    if len(permutations) < MIN_QUIZ_OPTIONS:
        return []
    correct = tuple(names)
    if len(permutations) > MAX_QUIZ_OPTIONS:
        others = [option for option in permutations if option != correct]
        random.shuffle(others)
        permutations = [correct] + others[:MAX_QUIZ_OPTIONS - 1]
    options = []
    for option in permutations:
        text = " · ".join(f"{index + 1}. {name}" for index, name in enumerate(option))
        options.append({"text": text[:MAX_TEXT], "is_correct": option == correct})
    return options


def _create_quizzes(connection: Connection, description_id: int, name_by_id: dict[int, str],
                    photos: dict[str, list[dict[str, Any]]]) -> list[int]:
    """Одна викторина на тип: варианты — перестановки авторóв по слотам фото."""
    created: list[int] = []
    for kind in QUIZ_KINDS:
        entries = photos.get(kind) or []
        if len(entries) < MIN_QUIZ_OPTIONS:
            continue
        author_ids = [int(entry["author_id"]) for entry in entries]
        options = _quiz_options(name_by_id, author_ids)
        if not options:
            continue
        random.shuffle(options)
        result = connection.execute(text("""
            INSERT INTO drink_description_quizzes (description_id, kind, question, photo_url)
            VALUES (:description_id, :kind, :question, :photo_url)
        """), {"description_id": description_id, "kind": kind,
               "question": QUIZ_QUESTIONS[kind], "photo_url": entries[0]["url"]})
        quiz_id = int(result.lastrowid)
        correct_option_id: int | None = None
        for index, option in enumerate(options):
            opt_result = connection.execute(text("""
                INSERT INTO drink_quiz_options (quiz_id, text, position, is_correct)
                VALUES (:quiz_id, :text, :position, :is_correct)
            """), {"quiz_id": quiz_id, "text": option["text"], "position": index,
                   "is_correct": 1 if option["is_correct"] else 0})
            if option["is_correct"]:
                correct_option_id = int(opt_result.lastrowid)
        connection.execute(
            text("UPDATE drink_description_quizzes SET correct_option_id = :id WHERE id = :quiz"),
            {"id": correct_option_id, "quiz": quiz_id},
        )
        created.append(quiz_id)
    return created


def _insert_photos(connection: Connection, description_id: int, kind: str,
                   entries: list[dict[str, Any]]) -> None:
    for index, entry in enumerate(entries):
        connection.execute(text(
            "INSERT INTO drink_description_photos (description_id, kind, url, author_id, position) "
            "VALUES (:description_id, :kind, :url, :author_id, :position)"
        ), {"description_id": description_id, "kind": kind, "url": entry["url"],
            "author_id": entry["author_id"], "position": index})


def _award_publish_xp(connection: Connection, author_id: int, items: list[dict[str, Any]],
                      latte_photos: list[dict[str, Any]], description_id: int) -> dict[str, int]:
    """Опыт за публикацию: «Команда» + «Сенсорика» (+ «Молоко» за латте-арт с молоком)."""
    amounts = dict(XP_PUBLISH)
    if latte_photos and any(item["category"] == MILK_COFFEE for item in items):
        for code, value in XP_PUBLISH_MILK.items():
            amounts[code] = amounts.get(code, 0) + value
    applied = gamification.award_xp(
        connection, user_id=author_id, amounts=amounts,
        source_type="description", source_id=description_id, created_by=author_id,
    )
    gamification.check_user_achievements(connection, author_id, "description")
    return applied


# --- Сериализация ------------------------------------------------------------ #


def _serialize_item(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "position": int(row["position"]),
        "category": row["category"],
        "drink": (
            {"id": int(row["drink_id"]), "title": row["drink_title"]}
            if row["drink_id"] is not None else None
        ),
        "bean": (
            {"id": int(row["bean_id"]), "name": row["bean_name"], "roast_type": row["bean_roast"]}
            if row["bean_id"] is not None else None
        ),
        "recipe": {
            "dose": row["recipe_dose"],
            "yield": row["recipe_yield"],
            "water": row["recipe_water"],
            "time": row["recipe_time"],
            "temp": row["recipe_temp"],
        },
        "is_espresso_recipe": bool(row["is_espresso_recipe"]),
        "balance": {
            "acidity": row["balance_acidity"],
            "sweetness": row["balance_sweetness"],
            "bitterness": row["balance_bitterness"],
        },
        "balance_texts": {
            "acidity": row["balance_acidity_text"],
            "sweetness": row["balance_sweetness_text"],
            "bitterness": row["balance_bitterness_text"],
        },
        "balance_score": row["balance_score"],
        "body": {"intensity": row["body_intensity"], "texture": row["body_texture"]},
        "bouquet": row["bouquet"],
        "aftertaste": {"duration": row["aftertaste_duration"], "quality": row["aftertaste_quality"]},
        "milk_drop": row["milk_drop"],
    }


def _items_for(connection: Connection, description_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not description_ids:
        return {}
    rows = connection.execute(
        text("""
            SELECT i.*, b.name AS bean_name, b.roast_type AS bean_roast, d.title AS drink_title
            FROM drink_description_items i
            LEFT JOIN coffee_beans b ON b.id = i.bean_id
            LEFT JOIN menu_drinks d ON d.id = i.drink_id
            WHERE i.description_id IN :ids
            ORDER BY i.description_id, i.position, i.id
        """).bindparams(bindparam("ids", expanding=True)),
        {"ids": description_ids},
    ).mappings().all()
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["description_id"]), []).append(_serialize_item(row))
    return grouped


def _photos_for(connection: Connection, description_ids: list[int]) -> dict[int, dict[str, list[dict[str, Any]]]]:
    if not description_ids:
        return {}
    rows = connection.execute(
        text("SELECT description_id, kind, url, author_id FROM drink_description_photos "
             "WHERE description_id IN :ids ORDER BY description_id, kind, position, id")
        .bindparams(bindparam("ids", expanding=True)),
        {"ids": description_ids},
    ).mappings().all()
    grouped: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for row in rows:
        bucket = grouped.setdefault(int(row["description_id"]), {"latte_art": [], "meme": []})
        bucket.setdefault(row["kind"], []).append({
            "url": row["url"],
            "author_id": int(row["author_id"]) if row["author_id"] is not None else None,
        })
    return grouped


DESCRIPTION_SELECT = """
    SELECT d.id, d.author_id, d.partner_id, d.point, d.shift_date, d.note,
           d.latte_art_photo, d.meme_photo, d.created_at,
           COALESCE(au.display_name, au.name) AS author_name,
           COALESCE(pu.display_name, pu.name) AS partner_name
    FROM drink_descriptions d
    JOIN users au ON au.id = d.author_id
    LEFT JOIN users pu ON pu.id = d.partner_id
"""


def _quizzes_for(connection: Connection, description_ids: list[int], viewer_id: int,
                 author_map: dict[int, int]) -> dict[int, list[dict[str, Any]]]:
    if not description_ids:
        return {}
    quizzes = connection.execute(
        text("""
            SELECT id, description_id, kind, question, photo_url, correct_option_id
            FROM drink_description_quizzes
            WHERE description_id IN :ids
            ORDER BY id
        """).bindparams(bindparam("ids", expanding=True)),
        {"ids": description_ids},
    ).mappings().all()
    if not quizzes:
        return {}
    quiz_ids = [int(quiz["id"]) for quiz in quizzes]
    option_rows = connection.execute(
        text("SELECT id, quiz_id, text, position, is_correct FROM drink_quiz_options "
             "WHERE quiz_id IN :ids ORDER BY quiz_id, position, id").bindparams(bindparam("ids", expanding=True)),
        {"ids": quiz_ids},
    ).mappings().all()
    voter_rows = connection.execute(
        text("""
            SELECT v.quiz_id, v.option_id, COALESCE(u.display_name, u.name) AS name
            FROM drink_quiz_votes v JOIN users u ON u.id = v.user_id
            WHERE v.quiz_id IN :ids
            ORDER BY v.created_at, v.user_id
        """).bindparams(bindparam("ids", expanding=True)),
        {"ids": quiz_ids},
    ).mappings().all()
    my_rows = connection.execute(
        text("SELECT quiz_id, option_id FROM drink_quiz_votes "
             "WHERE quiz_id IN :ids AND user_id = :me").bindparams(bindparam("ids", expanding=True)),
        {"ids": quiz_ids, "me": int(viewer_id)},
    ).mappings().all()
    voters: dict[int, dict[int, list[str]]] = {}
    for row in voter_rows:
        voters.setdefault(int(row["quiz_id"]), {}).setdefault(int(row["option_id"]), []).append(row["name"])
    counts: dict[int, dict[int, int]] = {
        quiz_id: {option_id: len(names) for option_id, names in quiz_options.items()}
        for quiz_id, quiz_options in voters.items()
    }
    my_votes = {int(row["quiz_id"]): int(row["option_id"]) for row in my_rows}
    options_by_quiz: dict[int, list[Any]] = {}
    for row in option_rows:
        options_by_quiz.setdefault(int(row["quiz_id"]), []).append(row)

    grouped: dict[int, list[dict[str, Any]]] = {}
    for quiz in quizzes:
        quiz_id = int(quiz["id"])
        description_id = int(quiz["description_id"])
        my_option = my_votes.get(quiz_id)
        reveal = my_option is not None or author_map.get(description_id) == int(viewer_id)
        options = []
        for row in options_by_quiz.get(quiz_id, []):
            option_id = int(row["id"])
            options.append({
                "id": option_id,
                "text": row["text"],
                "votes": counts.get(quiz_id, {}).get(option_id, 0),
                "voters": (voters.get(quiz_id, {}).get(option_id, []) if reveal else []),
                "is_correct": bool(row["is_correct"]) if reveal else None,
            })
        grouped.setdefault(description_id, []).append({
            "id": quiz_id,
            "kind": quiz["kind"],
            "question": quiz["question"],
            "photo_url": quiz["photo_url"],
            "voted": my_option is not None,
            "my_option_id": my_option,
            "correct_option_id": int(quiz["correct_option_id"]) if reveal and quiz["correct_option_id"] else None,
            "votes": sum(counts.get(quiz_id, {}).values()),
            "options": options,
        })
    return grouped


def _serialize_description(row: Any, items: list[dict[str, Any]], quizzes: list[dict[str, Any]],
                           photos: dict[str, list[dict[str, Any]]], viewer_id: int) -> dict[str, Any]:
    latte = photos.get("latte_art") or []
    meme = photos.get("meme") or []
    return {
        "id": int(row["id"]),
        "point": row["point"],
        "shift_date": row["shift_date"],
        "note": row["note"],
        "photos": {"latte_art": latte, "meme": meme},
        "latte_art_photo": latte[0]["url"] if latte else None,
        "meme_photo": meme[0]["url"] if meme else None,
        "created_at": row["created_at"],
        "author": {"id": int(row["author_id"]), "name": row["author_name"]},
        "partner": (
            {"id": int(row["partner_id"]), "name": row["partner_name"]}
            if row["partner_id"] is not None else None
        ),
        "is_mine": int(row["author_id"]) == int(viewer_id),
        "items": items,
        "quizzes": quizzes,
    }


# --- HTTP ------------------------------------------------------------------- #


@router.get("")
def list_descriptions(point: str | None = None, author_id: int | None = None,
                      limit: int = DEFAULT_PAGE, offset: int = 0,
                      user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Лента описаний с фильтрами по точке и бариста и постраничной загрузкой."""
    if point not in (None, "all") and point not in POINT_VALUES:
        raise HTTPException(status_code=422, detail="Неизвестная торговая точка")
    limit = max(1, min(int(limit or DEFAULT_PAGE), MAX_PAGE))
    offset = max(0, int(offset or 0))
    conditions: list[str] = []
    params: dict[str, Any] = {"limit": limit + 1, "offset": offset}
    if point not in (None, "all"):
        conditions.append("d.point = :point")
        params["point"] = point
    if author_id:
        conditions.append("(d.author_id = :author OR d.partner_id = :author)")
        params["author"] = int(author_id)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    with engine.connect() as connection:
        rows = connection.execute(text(
            f"{DESCRIPTION_SELECT}{where} ORDER BY d.created_at DESC, d.id DESC LIMIT :limit OFFSET :offset"
        ), params).mappings().all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        ids = [int(row["id"]) for row in rows]
        items = _items_for(connection, ids)
        author_map = {int(row["id"]): int(row["author_id"]) for row in rows}
        quizzes = _quizzes_for(connection, ids, int(user["id"]), author_map)
        photos = _photos_for(connection, ids)
        authors = connection.execute(text("""
            SELECT DISTINCT u.id, COALESCE(u.display_name, u.name) AS name
            FROM drink_descriptions d JOIN users u ON u.id = d.author_id
            ORDER BY name
        """)).mappings().all()
    return {
        "items": [
            _serialize_description(row, items.get(int(row["id"]), []), quizzes.get(int(row["id"]), []),
                                   photos.get(int(row["id"]), {}), int(user["id"]))
            for row in rows
        ],
        "points": list(POINT_VALUES),
        "authors": [{"id": int(row["id"]), "name": row["name"]} for row in authors],
        "has_more": has_more,
        "limit": limit,
        "offset": offset,
    }


@router.get("/shift")
def my_shift(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Точка и напарники текущего сотрудника по графику (фоллбэк «вне смены»)."""
    return current_shift(int(user["id"]))


@router.get("/options")
def description_options(_: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Справочники формы: зерно и напитки из базы знаний, категории, шкалы и точки."""
    with engine.connect() as connection:
        beans = connection.execute(text(
            "SELECT id, name, roast_type FROM coffee_beans WHERE is_active = 1 ORDER BY name"
        )).mappings().all()
        drinks = connection.execute(text(
            "SELECT id, title, category FROM menu_drinks WHERE is_active = 1 ORDER BY title"
        )).mappings().all()
    return {
        "beans": [{"id": int(row["id"]), "name": row["name"], "roast_type": row["roast_type"]} for row in beans],
        "drinks": [{"id": int(row["id"]), "title": row["title"], "category": row["category"]} for row in drinks],
        "categories": list(ITEM_CATEGORIES),
        "scales": {
            "balance": list(BALANCE_LEVELS),
            "body": list(BODY_LEVELS),
            "aftertaste": list(AFTERTASTE_LEVELS),
            "milk_drop": list(MILK_DROP_LEVELS),
            "balance_score": list(BALANCE_SCORES),
        },
        "points": list(POINT_VALUES),
    }


@router.get("/memes")
def list_memes(author_id: int | None = None, limit: int = 30, offset: int = 0,
               _: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Галерея «Мемы смен»: фото мемов с фильтром по автору."""
    limit = max(1, min(int(limit or 30), 60))
    offset = max(0, int(offset or 0))
    params: dict[str, Any] = {"limit": limit + 1, "offset": offset}
    condition = ""
    if author_id:
        condition = "AND d.author_id = :author"
        params["author"] = int(author_id)
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT p.id, p.url, p.description_id, p.position, d.point, d.shift_date, d.created_at,
                   d.author_id, COALESCE(au.display_name, au.name) AS author_name,
                   COALESCE(pu.display_name, pu.name) AS partner_name
            FROM drink_description_photos p
            JOIN drink_descriptions d ON d.id = p.description_id
            JOIN users au ON au.id = d.author_id
            LEFT JOIN users pu ON pu.id = d.partner_id
            WHERE p.kind = 'meme' {condition}
            ORDER BY d.created_at DESC, d.id DESC, p.position, p.id
            LIMIT :limit OFFSET :offset
        """), params).mappings().all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        authors = connection.execute(text("""
            SELECT DISTINCT u.id, COALESCE(u.display_name, u.name) AS name
            FROM drink_description_photos p
            JOIN drink_descriptions d ON d.id = p.description_id
            JOIN users u ON u.id = d.author_id
            WHERE p.kind = 'meme'
            ORDER BY name
        """)).mappings().all()
    return {
        "items": [{
            "id": int(row["id"]),
            "url": row["url"],
            "description_id": int(row["description_id"]),
            "point": row["point"],
            "shift_date": row["shift_date"],
            "created_at": row["created_at"],
            "author": {"id": int(row["author_id"]), "name": row["author_name"]},
            "partner": {"name": row["partner_name"]} if row["partner_name"] else None,
        } for row in rows],
        "authors": [{"id": int(row["id"]), "name": row["name"]} for row in authors],
        "has_more": has_more,
        "limit": limit,
        "offset": offset,
    }


@router.post("/photos")
async def upload_photo(request: Request, _: dict[str, Any] = Depends(require_csrf)) -> dict[str, Any]:
    """Тело запроса — сам файл изображения (без multipart), как у фото оценок."""
    data = await request.body()
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="Файл пустой или больше 8 МБ")
    url = await run_in_threadpool(store_photo, data)
    if not url:
        raise HTTPException(status_code=422, detail="Не удалось прочитать изображение")
    return {"ok": True, "url": url}


@router.get("/photos/{filename}")
def description_photo(filename: str, _: dict[str, Any] = Depends(require_user)):
    """Отдаёт фото поста. Имя из URL пропускаем только собственного формата."""
    if not PHOTO_FILENAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="Фото не найдено")
    path = photos_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Фото не найдено")
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=31536000"},
    )


@router.post("")
async def create_description(request: Request,
                             user: dict[str, Any] = Depends(require_csrf)) -> dict[str, Any]:
    """Публикует описание смены: валидация напитков, фото, викторины и начисление XP."""
    payload = await _json_object(request)
    items = _normalize_items(payload)
    point = payload.get("point")
    if point not in POINT_VALUES:
        raise HTTPException(status_code=422, detail="Укажите торговую точку")
    partner_id = _optional_reference_id(payload, "partner_id", "Напарник")
    if partner_id == int(user["id"]):
        partner_id = None
    shift_date = _clean_shift_date(payload.get("shift_date"))
    note = _optional_text(payload, "note", "заметка", MAX_NOTE)
    photos = {
        "latte_art": _photo_entries(
            payload.get("latte_art_photos") or payload.get("latte_art_photo"), "латте-арт", int(user["id"])),
        "meme": _photo_entries(
            payload.get("meme_photos") or payload.get("meme_photo"), "мем", int(user["id"])),
    }

    with engine.begin() as connection:
        partner: dict[str, Any] | None = None
        if partner_id is not None:
            row = connection.execute(text(
                "SELECT id, COALESCE(display_name, name) AS name, role, is_active FROM users WHERE id = :id"
            ), {"id": partner_id}).mappings().first()
            if not row or not row["is_active"] or row["role"] not in ("newbie", "barista", "senior", "mentor"):
                raise HTTPException(status_code=422, detail="Напарник не найден или недоступен")
            partner = {"id": int(row["id"]), "name": row["name"]}
        for item in items:
            if item["bean_id"] is not None and not connection.execute(text(
                "SELECT 1 FROM coffee_beans WHERE id = :id AND is_active = 1"
            ), {"id": item["bean_id"]}).first():
                raise HTTPException(status_code=422, detail="Зерно не найдено или отключено")
            if item["drink_id"] is not None:
                drink_row = connection.execute(text(
                    "SELECT category FROM menu_drinks WHERE id = :id AND is_active = 1"
                ), {"id": item["drink_id"]}).mappings().first()
                if not drink_row:
                    raise HTTPException(status_code=422, detail="Напиток не найден или отключён")
                if drink_row["category"] != item["category"]:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Напиток «{drink_row['category']}» не соответствует категории «{item['category']}»",
                    )
        author_row = connection.execute(text(
            "SELECT COALESCE(display_name, name) AS name FROM users WHERE id = :id"
        ), {"id": user["id"]}).mappings().first()
        author_name = author_row["name"] if author_row else "Сотрудник"

        # Фото привязываются к бариста смены: автор поста или его напарник.
        name_by_id = {int(user["id"]): author_name}
        if partner is not None:
            name_by_id[int(partner["id"])] = partner["name"]
        for kind in QUIZ_KINDS:
            for entry in photos.get(kind) or []:
                if int(entry["author_id"]) not in name_by_id:
                    raise HTTPException(status_code=422, detail="Фото может принадлежать только бариста смены")

        result = connection.execute(text("""
            INSERT INTO drink_descriptions
                (author_id, partner_id, point, shift_date, note, latte_art_photo, meme_photo)
            VALUES (:author, :partner, :point, :date, :note, :latte, :meme)
        """), {
            "author": int(user["id"]), "partner": partner_id, "point": point,
            "date": shift_date, "note": note,
            "latte": photos["latte_art"][0]["url"] if photos["latte_art"] else None,
            "meme": photos["meme"][0]["url"] if photos["meme"] else None,
        })
        description_id = int(result.lastrowid)
        _insert_items(connection, description_id, items)
        _insert_photos(connection, description_id, "latte_art", photos["latte_art"])
        _insert_photos(connection, description_id, "meme", photos["meme"])
        quiz_ids = _create_quizzes(connection, description_id, name_by_id, photos)
        xp = _award_publish_xp(connection, int(user["id"]), items, photos["latte_art"], description_id)
    return {"ok": True, "id": description_id, "quizzes": quiz_ids, "xp": xp}


@router.get("/{description_id}")
def description_detail(description_id: int,
                       user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Деталь одного поста: напитки и викторины."""
    with engine.connect() as connection:
        row = connection.execute(
            text(f"{DESCRIPTION_SELECT} WHERE d.id = :id"), {"id": int(description_id)}
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Описание не найдено")
        ids = [int(row["id"])]
        items = _items_for(connection, ids)
        quizzes = _quizzes_for(connection, ids, int(user["id"]),
                               {int(row["id"]): int(row["author_id"])})
        photos = _photos_for(connection, ids)
    return _serialize_description(row, items.get(int(row["id"]), []), quizzes.get(int(row["id"]), []),
                                  photos.get(int(row["id"]), {}), int(user["id"]))


@router.post("/{description_id}/quizzes/{quiz_id}/vote")
async def vote_quiz(description_id: int, quiz_id: int, request: Request,
                    user: dict[str, Any] = Depends(require_csrf)) -> dict[str, Any]:
    """Голос в викторине. Верный ответ даёт XP в «Команду» — один раз на участника."""
    payload = await _json_object(request)
    try:
        option_id = int(payload.get("option_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Выберите вариант ответа") from None

    with engine.begin() as connection:
        quiz = connection.execute(text(
            "SELECT id, description_id, correct_option_id FROM drink_description_quizzes WHERE id = :id"
        ), {"id": int(quiz_id)}).mappings().first()
        if not quiz or int(quiz["description_id"]) != int(description_id):
            raise HTTPException(status_code=404, detail="Викторина не найдена")
        description = connection.execute(text(
            "SELECT author_id FROM drink_descriptions WHERE id = :id"
        ), {"id": int(description_id)}).mappings().first()
        option = connection.execute(text(
            "SELECT id, is_correct FROM drink_quiz_options WHERE id = :id AND quiz_id = :quiz"
        ), {"id": option_id, "quiz": int(quiz_id)}).mappings().first()
        if not option:
            raise HTTPException(status_code=422, detail="Некорректный вариант ответа")
        existing = connection.execute(text(
            "SELECT option_id FROM drink_quiz_votes WHERE quiz_id = :quiz AND user_id = :user"
        ), {"quiz": int(quiz_id), "user": int(user["id"])}).mappings().first()
        if existing:
            raise HTTPException(status_code=409, detail="Вы уже ответили")
        connection.execute(text(
            "INSERT INTO drink_quiz_votes (quiz_id, option_id, user_id) VALUES (:quiz, :option, :user)"
        ), {"quiz": int(quiz_id), "option": option_id, "user": int(user["id"])})

        is_author = description is not None and int(description["author_id"]) == int(user["id"])
        awarded = 0
        if option["is_correct"] and not is_author:
            applied = gamification.award_xp(
                connection, user_id=int(user["id"]), amounts=dict(XP_QUIZ_CORRECT),
                source_type="quiz", source_id=int(quiz_id), created_by=None,
            )
            awarded = sum(applied.values())
            gamification.check_user_achievements(connection, int(user["id"]), "quiz")

    with engine.connect() as connection:
        quizzes = _quizzes_for(connection, [int(description_id)], int(user["id"]),
                               {int(description_id): int(description["author_id"]) if description else 0})
    return {
        "ok": True,
        "correct": bool(option["is_correct"]),
        "xp": awarded,
        "quizzes": quizzes.get(int(description_id), []),
    }
