"""Точки присутствия и общие настройки бренда.

Единственный источник истины для торговых точек: код (латиница), короткий
alias для бейджей, полное название и цвет. Раньше эти значения были
захардкожены в модулях графика, часов, отчётов и опросников — теперь они
живут в `work_points`, а настройки проекта — в `web_app_settings`.

Каноническое значение в таблицах графика/оценок — `code` (`uya`, `de`).
Хелперы `resolve_point`/`normalize_point` принимают и код, и alias, поэтому
старые записи и офлайн-кеш клиента продолжают работать. Инициализация
идемпотентна: схема, seed, ключи настроек и разовая конвертация legacy
значений выполняются при каждом старте.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from bot.database.models import engine
from bot.web.deps import require_manager_csrf, require_user

# Ключи общих настроек в web_app_settings. Таблицу создаёт
# initialize_calendar_schema, поэтому наш initialize_schema вызывается после.
PROJECT_NAME_KEY = "app.project_name"
DEFAULT_ACCENT_KEY = "app.default_accent"
DEFAULT_PROJECT_NAME = "НЕФТЬ"
DEFAULT_ACCENT = "#f47369"

# Дефолтные точки: (код, короткое имя, полное имя, цвет, порядок).
DEFAULT_POINTS = (
    ("uya", "УЯ", "Университетская", "#2a9d8f", 10),
    ("de", "ДЕ", "Декабристов", "#e76f51", 20),
)

CODE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_ALIAS = 20
MAX_TITLE = 120
MAX_PROJECT_NAME = 60
# Спец-значение описаний напитков: «смена не найдена». Точкой не является.
OTHER_POINT = "другое"

_DDL = """
CREATE TABLE IF NOT EXISTS work_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    title_alias TEXT NOT NULL,
    title TEXT NOT NULL,
    color TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0
)
"""

# Где ещё лежат значения точек. При смене кода обновляем и их, чтобы история
# не начала ссылаться на несуществующую точку.
_POINT_COLUMNS = (
    ("shift_types", "point"),
    ("web_shift_templates", "point"),
    ("evaluations", "shift_point"),
    ("drink_descriptions", "point"),
    ("web_shift_change_log", "old_point"),
    ("web_shift_change_log", "new_point"),
)


def _existing_tables(connection: Any) -> set[str]:
    rows = connection.execute(text(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )).scalars().all()
    return {str(row) for row in rows}


def _table_columns(connection: Any, table: str) -> set[str]:
    """Колонки таблицы: старые базы могли не иметь новых полей."""
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table})")
    return {str(row[1]) for row in rows}


def _migrate_legacy_values(connection: Any, tables: set[str]) -> None:
    """Конвертирует старые кириллические alias в коды (идемпотентно)."""
    rows = connection.execute(text(
        "SELECT code, title_alias FROM work_points"
    )).mappings().all()
    for point in rows:
        for table, column in _POINT_COLUMNS:
            if table not in tables or column not in _table_columns(connection, table):
                continue
            connection.execute(text(
                f"UPDATE {table} SET {column} = :code WHERE {column} = :alias"
            ), {"code": point["code"], "alias": point["title_alias"]})


def initialize_schema(engine: Engine) -> None:
    """Создаёт таблицу, досеивает точки/настройки и конвертирует legacy."""
    with engine.begin() as connection:
        connection.execute(text(_DDL))
        # Alias должен быть уникален: по нему разрешаются старые значения.
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_work_points_alias "
            "ON work_points (title_alias COLLATE NOCASE)"
        ))
        seeded = connection.execute(text("SELECT COUNT(*) FROM work_points")).scalar_one()
        if not seeded:
            for code, alias, title, color, order in DEFAULT_POINTS:
                connection.execute(text("""
                    INSERT INTO work_points (code, title_alias, title, color, is_active, sort_order)
                    VALUES (:code, :alias, :title, :color, 1, :order)
                """), {"code": code, "alias": alias, "title": title,
                       "color": color, "order": order})
        # Настройки бренда: значения по умолчанию не перетирают уже сохранённые.
        connection.execute(text(
            "INSERT OR IGNORE INTO web_app_settings (key, value) VALUES (:key, :value)"
        ), {"key": PROJECT_NAME_KEY, "value": DEFAULT_PROJECT_NAME})
        connection.execute(text(
            "INSERT OR IGNORE INTO web_app_settings (key, value) VALUES (:key, :value)"
        ), {"key": DEFAULT_ACCENT_KEY, "value": DEFAULT_ACCENT})
        _migrate_legacy_values(connection, _existing_tables(connection))


# --------------------------------------------------------------------------- #
# Чтение справочника
# --------------------------------------------------------------------------- #


def _point_dict(row: Any) -> dict[str, Any]:
    color = str(row["color"] or "")
    return {
        "id": int(row["id"]),
        "code": row["code"],
        "title_alias": row["title_alias"],
        "title": row["title"],
        "color": color if COLOR_RE.match(color) else DEFAULT_ACCENT,
        "is_active": int(row["is_active"] or 0),
        "sort_order": int(row["sort_order"] or 0),
    }


def all_points(engine: Engine, active_only: bool = False) -> list[dict[str, Any]]:
    """Все точки в порядке сортировки; `active_only` — только включённые."""
    where = " WHERE is_active = 1" if active_only else ""
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT id, code, title_alias, title, color, is_active, sort_order
            FROM work_points{where}
            ORDER BY sort_order, id
        """)).mappings().all()
    return [_point_dict(row) for row in rows]


def active_points(engine: Engine) -> list[dict[str, Any]]:
    return all_points(engine, active_only=True)


def active_codes(engine: Engine) -> tuple[str, ...]:
    return tuple(point["code"] for point in active_points(engine))


def point_index(engine: Engine) -> dict[str, dict[str, Any]]:
    """Индекс по коду и alias (в нижнем регистре) — для старых значений."""
    index: dict[str, dict[str, Any]] = {}
    for point in all_points(engine):
        for key in (point["code"], point["title_alias"]):
            index[str(key).strip().lower()] = point
    return index


def resolve_point(engine: Engine, value: Any) -> dict[str, Any] | None:
    """Точка по коду или alias; `None`, если значение неизвестно."""
    if value is None:
        return None
    key = str(value).strip().lower()
    if not key:
        return None
    return point_index(engine).get(key)


def normalize_point(engine: Engine, value: Any, allow_none: bool = False,
                    require_active: bool = True) -> str | None:
    """Возвращает канонический код точки или выбрасывает ValueError."""
    if value is None or not str(value).strip():
        if allow_none:
            return None
        raise ValueError("Укажите торговую точку")
    point = resolve_point(engine, value)
    if point is None or (require_active and not point["is_active"]):
        raise ValueError("Неизвестная торговая точка")
    return point["code"]


def point_label(engine: Engine, value: Any) -> str:
    """Короткое название точки для пользовательского текста."""
    point = resolve_point(engine, value)
    if point:
        return point["title_alias"]
    return "" if value is None else str(value)


def label_with_index(index: dict[str, dict[str, Any]], value: Any) -> str:
    point = index.get(str(value or "").strip().lower())
    return point["title_alias"] if point else ("" if value is None else str(value))


# --------------------------------------------------------------------------- #
# Общие настройки бренда
# --------------------------------------------------------------------------- #


def _settings_values(connection: Any) -> dict[str, str]:
    rows = connection.execute(text("""
        SELECT key, value FROM web_app_settings
        WHERE key IN (:project, :accent)
    """), {"project": PROJECT_NAME_KEY, "accent": DEFAULT_ACCENT_KEY}).mappings().all()
    return {row["key"]: row["value"] for row in rows}


def get_main_settings(engine: Engine) -> dict[str, Any]:
    """Настройки раздела «Основное» + активные точки для клиента."""
    with engine.connect() as connection:
        values = _settings_values(connection)
    accent = str(values.get(DEFAULT_ACCENT_KEY) or "")
    return {
        "project_name": values.get(PROJECT_NAME_KEY) or DEFAULT_PROJECT_NAME,
        "default_accent": accent if COLOR_RE.match(accent) else DEFAULT_ACCENT,
        "points": active_points(engine),
    }


def project_name(engine: Engine) -> str:
    with engine.connect() as connection:
        values = _settings_values(connection)
    return values.get(PROJECT_NAME_KEY) or DEFAULT_PROJECT_NAME


def default_accent(engine: Engine) -> str:
    with engine.connect() as connection:
        values = _settings_values(connection)
    accent = str(values.get(DEFAULT_ACCENT_KEY) or "")
    return accent if COLOR_RE.match(accent) else DEFAULT_ACCENT


def save_main_settings(engine: Engine, values: Any) -> dict[str, Any]:
    """Сохраняет название проекта и дефолтный акцент (manager + CSRF)."""
    allowed = {"project_name", "default_accent"}
    if not isinstance(values, dict) or not values:
        raise ValueError("Ожидались настройки для сохранения")
    if set(values) - allowed:
        raise ValueError("Неизвестные настройки")
    normalized: dict[str, str] = {}
    if "project_name" in values:
        name = str(values.get("project_name") or "").strip()
        if not name or len(name) > MAX_PROJECT_NAME:
            raise ValueError(f"Название проекта должно содержать от 1 до {MAX_PROJECT_NAME} символов")
        normalized[PROJECT_NAME_KEY] = name
    if "default_accent" in values:
        accent = str(values.get("default_accent") or "").strip()
        if not COLOR_RE.match(accent):
            raise ValueError("Акцент должен быть цветом вида #rrggbb")
        normalized[DEFAULT_ACCENT_KEY] = accent.lower()
    with engine.begin() as connection:
        for key, value in normalized.items():
            connection.execute(text("""
                INSERT INTO web_app_settings (key, value, updated_at)
                VALUES (:key, :value, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = CURRENT_TIMESTAMP
            """), {"key": key, "value": value})
    return get_main_settings(engine)


# --------------------------------------------------------------------------- #
# CRUD справочника
# --------------------------------------------------------------------------- #


def _clean(value: Any, limit: int) -> str:
    return str(value).strip()[:limit] if value is not None else ""


def _normalize_code(value: Any) -> str:
    code = str(value or "").strip().lower()
    if not CODE_RE.match(code):
        raise ValueError("Код точки: латиница в нижнем регистре, цифры, дефис или подчёркивание")
    return code


def _normalize_color(value: Any) -> str:
    color = str(value or "").strip()
    if not COLOR_RE.match(color):
        raise ValueError("Цвет точки должен быть вида #rrggbb")
    return color.lower()


def _coerce_order(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError("Порядок сортировки должен быть числом")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Порядок сортировки должен быть числом") from error


def _get_point(engine: Engine, point_id: int) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT id, code, title_alias, title, color, is_active, sort_order
            FROM work_points WHERE id = :id
        """), {"id": point_id}).mappings().first()
    if row is None:
        raise LookupError("Точка не найдена")
    return _point_dict(row)


def _check_unique(connection: Any, code: str, alias: str, exclude_id: int | None = None) -> None:
    """Код и alias уникальны. Alias сравниваем в Python: SQLite NOCASE
    сворачивает только ASCII, а короткие имена у нас кириллицей."""
    query = "SELECT id, code, title_alias FROM work_points"
    params: dict[str, Any] = {}
    if exclude_id is not None:
        query += " WHERE id <> :id"
        params["id"] = exclude_id
    rows = connection.execute(text(query), params).mappings().all()
    for row in rows:
        if row["code"] == code or str(row["title_alias"]).strip().lower() == alias.strip().lower():
            raise ValueError("Точка с таким кодом или коротким названием уже есть")


def _active_count(connection: Any, exclude_id: int | None = None) -> int:
    if exclude_id is None:
        return int(connection.execute(text(
            "SELECT COUNT(*) FROM work_points WHERE is_active = 1"
        )).scalar_one())
    return int(connection.execute(text(
        "SELECT COUNT(*) FROM work_points WHERE is_active = 1 AND id <> :id"
    ), {"id": exclude_id}).scalar_one())


def _rename_point_code(connection: Any, old: str, new: str, tables: set[str]) -> None:
    """Каскадно обновляет код точки во всех связанных таблицах."""
    for table, column in _POINT_COLUMNS:
        if table not in tables or old == new:
            continue
        if column not in _table_columns(connection, table):
            continue
        connection.execute(text(
            f"UPDATE {table} SET {column} = :new WHERE {column} = :old"
        ), {"old": old, "new": new})


def create_point(engine: Engine, payload: Any) -> dict[str, Any]:
    """Новая точка присутствия (manager + CSRF)."""
    allowed = {"code", "title_alias", "title", "color", "is_active", "sort_order"}
    if not isinstance(payload, dict):
        raise ValueError("Ожидался объект точки")
    if set(payload) - allowed:
        raise ValueError("Неизвестные поля точки")
    code = _normalize_code(payload.get("code"))
    alias = _clean(payload.get("title_alias"), MAX_ALIAS)
    title = _clean(payload.get("title"), MAX_TITLE)
    if not alias:
        raise ValueError("Короткое название точки обязательно")
    if not title:
        raise ValueError("Полное название точки обязательно")
    color = _normalize_color(payload.get("color"))
    is_active = 1 if payload.get("is_active", True) else 0
    with engine.begin() as connection:
        order = _coerce_order(payload.get("sort_order"),
                              int(connection.execute(text(
                                  "SELECT COALESCE(MAX(sort_order), 0) + 10 FROM work_points"
                              )).scalar_one()))
        _check_unique(connection, code, alias)
        point_id = connection.execute(text("""
            INSERT INTO work_points (code, title_alias, title, color, is_active, sort_order)
            VALUES (:code, :alias, :title, :color, :active, :order)
            RETURNING id
        """), {"code": code, "alias": alias, "title": title,
               "color": color, "active": is_active, "order": order}).scalar_one()
    return _get_point(engine, int(point_id))


def update_point(engine: Engine, point_id: int, payload: Any) -> dict[str, Any]:
    """Правка точки; смена кода каскадно обновляет график, оценки и описания."""
    allowed = {"code", "title_alias", "title", "color", "is_active", "sort_order"}
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Ожидались поля точки")
    if set(payload) - allowed:
        raise ValueError("Неизвестные поля точки")
    with engine.begin() as connection:
        row = connection.execute(text("""
            SELECT id, code, title_alias, title, color, is_active, sort_order
            FROM work_points WHERE id = :id
        """), {"id": point_id}).mappings().first()
        if row is None:
            raise LookupError("Точка не найдена")
        current = _point_dict(row)
        values: dict[str, Any] = {}
        code = current["code"]
        alias = current["title_alias"]
        if "code" in payload:
            code = _normalize_code(payload.get("code"))
            values["code"] = code
        if "title_alias" in payload:
            alias = _clean(payload.get("title_alias"), MAX_ALIAS)
            if not alias:
                raise ValueError("Короткое название точки обязательно")
            values["title_alias"] = alias
        if "title" in payload:
            title = _clean(payload.get("title"), MAX_TITLE)
            if not title:
                raise ValueError("Полное название точки обязательно")
            values["title"] = title
        if "color" in payload:
            values["color"] = _normalize_color(payload.get("color"))
        if "is_active" in payload:
            active = 1 if payload.get("is_active") else 0
            if not active and current["is_active"] and not _active_count(connection, point_id):
                raise ValueError("Должна остаться хотя бы одна активная точка")
            values["is_active"] = active
        if "sort_order" in payload:
            values["sort_order"] = _coerce_order(payload.get("sort_order"), current["sort_order"])
        if not values:
            return current
        if code != current["code"] or alias != current["title_alias"]:
            _check_unique(connection, code, alias, exclude_id=point_id)
        assignments = ", ".join(f"{key} = :{key}" for key in values)
        connection.execute(text(
            f"UPDATE work_points SET {assignments} WHERE id = :id"
        ), {**values, "id": point_id})
        if code != current["code"]:
            _rename_point_code(connection, current["code"], code, _existing_tables(connection))
    return _get_point(engine, point_id)


def delete_point(engine: Engine, point_id: int) -> None:
    """Удаляет неиспользуемую точку. Используемую можно только отключить."""
    with engine.begin() as connection:
        row = connection.execute(text("""
            SELECT id, code, is_active FROM work_points WHERE id = :id
        """), {"id": point_id}).mappings().first()
        if row is None:
            raise LookupError("Точка не найдена")
        if row["is_active"] and _active_count(connection) <= 1:
            raise ValueError("Должна остаться хотя бы одна активная точка")
        tables = _existing_tables(connection)
        for table, column in _POINT_COLUMNS:
            if table not in tables or column not in _table_columns(connection, table):
                continue
            used = connection.execute(text(
                f"SELECT 1 FROM {table} WHERE {column} = :code LIMIT 1"
            ), {"code": row["code"]}).first()
            if used:
                raise ValueError("Точка используется в графике или оценках — отключите её вместо удаления")
        connection.execute(text("DELETE FROM work_points WHERE id = :id"), {"id": point_id})


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


points_router = APIRouter(prefix="/api/points", tags=["points"])
settings_router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Некорректный JSON") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Ожидался JSON-объект")
    return payload


@settings_router.get("/main")
def main_settings(_: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Бренд, дефолтный акцент и активные точки — для всего интерфейса."""
    return get_main_settings(engine)


@settings_router.put("/main")
async def update_main_settings(request: Request,
                               _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    payload = await _json_object(request)
    try:
        settings = save_main_settings(engine, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"ok": True, **settings}


@points_router.get("")
def list_points(_: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Все точки, включая отключённые: нужны панели управления."""
    return {"points": all_points(engine)}


@points_router.post("", status_code=201)
async def add_point(request: Request,
                    _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    payload = await _json_object(request)
    try:
        point = create_point(engine, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except IntegrityError as error:
        raise HTTPException(status_code=409, detail="Точка с таким кодом уже есть") from error
    return {"ok": True, "point": point, "points": all_points(engine)}


@points_router.put("/{point_id}")
async def edit_point(point_id: int, request: Request,
                     _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    payload = await _json_object(request)
    try:
        point = update_point(engine, point_id, payload)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except IntegrityError as error:
        raise HTTPException(status_code=409, detail="Точка с таким кодом уже есть") from error
    return {"ok": True, "point": point, "points": all_points(engine)}


@points_router.delete("/{point_id}")
def remove_point(point_id: int,
                 _: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    try:
        delete_point(engine, point_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"ok": True, "points": all_points(engine)}