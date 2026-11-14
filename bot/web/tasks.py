"""Канбан-доска задач: этапы смены, архив и категории.

Модуль изолирован по образцу `announcements.py`: здесь только доменная логика
(таблицы, выборки, права видимости), а роуты живут в `bot/web/app.py`.

Этапы (`tasks.status`):

* `todo` — «Этап 01: To Do» — к выполнению;
* `in_progress` — «Этап 02: В работе»;
* `done` — «Этап 03: Готово» (ещё на доске, не в архиве);
* `archived_done` / `archived_cancelled` — архив: выполнено / отменено.

Адресация (`tasks.assignee_type`):

* `all` — общая задача для всей команды;
* `role` — на должность (`newbie`/`barista`/`senior`/`mentor`);
* `user` — конкретному сотруднику.

Видимость строго персональная: сотрудник видит общие задачи, задачи на свою
должность и задачи лично на него. Чужие персональные задачи не отдаются никому —
даже наставнику: фильтр «Все» это персональная выборка, а не «вся база».

Мягкое удаление (`is_deleted`) — основа Undo: карточка уходит с доски, но её
можно вернуть на прежнее место. Окончательно удаляются только архивные задачи.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from bot.web import modules
from bot.web.announcements import COLOR_RE, TRANSLIT
from bot.web.calendar_service import ROLE_NAMES as ROLE_LABELS, _ensure_columns

logger = logging.getLogger("bot.web.tasks")

# Категории по умолчанию: code, название, цвет, порядок. Цвета взяты из макета
# CrewDesk (Stitch «Kanban Board Page Layout»), дальше их ведёт панель управления.
DEFAULT_CATEGORIES = (
    ("bar", "БАР", "#ff6b4a", 10),
    ("tech", "ТЕХНИКА", "#60a5fa", 20),
    ("stock", "СКЛАД", "#5eead4", 30),
    ("prep", "ЗАГОТОВКИ", "#f59e0b", 40),
    ("inventory", "ИНВЕНТАРЬ", "#fcba5f", 50),
    ("checklist", "ЧЕК-ЛИСТ", "#4ade80", 60),
)

STATUSES = ("todo", "in_progress", "done", "archived_done", "archived_cancelled")
BOARD_STATUSES = ("todo", "in_progress", "done")
ARCHIVE_STATUSES = ("archived_done", "archived_cancelled")
PRIORITIES = ("normal", "high", "urgent")
ASSIGNEE_TYPES = ("all", "role", "user")
ASSIGNEE_ROLES = tuple(ROLE_LABELS)
# Режимы выборки: всё доступное, только моё (должность + лично), только общие.
FILTERS = ("all", "mine", "shared")

FALLBACK_COLOR = "#7b8794"

MAX_TITLE = 120
MAX_DESCRIPTION = 2000
MAX_META = 120
MAX_LABEL = 60
MAX_LIST = 300

modules.register_defaults("tasks", {
    "MAX_TITLE": MAX_TITLE, "MAX_DESCRIPTION": MAX_DESCRIPTION,
    "MAX_META": MAX_META, "MAX_LABEL": MAX_LABEL,
})


def initialize_schema(engine: Engine) -> None:
    """Идемпотентно создаёт таблицы модуля (вызывается на старте сервиса)."""
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS task_categories (
                code TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                color TEXT NOT NULL DEFAULT '#7b8794',
                position INTEGER NOT NULL DEFAULT 100,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        # Категории по умолчанию — один раз, дальше их ведёт панель управления.
        for code, label, color, position in DEFAULT_CATEGORIES:
            connection.execute(text("""
                INSERT OR IGNORE INTO task_categories (code, label, color, position)
                VALUES (:code, :label, :color, :position)
            """), {"code": code, "label": label, "color": color, "position": position})
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                category_code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'todo',
                priority TEXT NOT NULL DEFAULT 'normal',
                meta_info TEXT,
                assignee_type TEXT NOT NULL DEFAULT 'all',
                assignee_role TEXT,
                assignee_user_id INTEGER REFERENCES users(id),
                author_id INTEGER REFERENCES users(id),
                completed_by_id INTEGER REFERENCES users(id),
                completed_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                position INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                deleted_at DATETIME
            )
        """))
        for name, ddl in (
            ("idx_tasks_column", "(status, position)"),
            ("idx_tasks_category", "(category_code)"),
            ("idx_tasks_assignee", "(assignee_type, assignee_user_id, assignee_role)"),
        ):
            connection.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON tasks {ddl}"))
        if engine.dialect.name == "sqlite":
            # Поля докатываются и на живой базе — таблица живёт между релизами.
            _ensure_columns(connection, "tasks", {
                "priority": "TEXT NOT NULL DEFAULT 'normal'",
                "meta_info": "TEXT",
                "assignee_role": "TEXT",
                "assignee_user_id": "INTEGER",
                "completed_by_id": "INTEGER",
                "completed_at": "TEXT",
                "position": "INTEGER NOT NULL DEFAULT 0",
                "is_deleted": "INTEGER NOT NULL DEFAULT 0",
                "deleted_at": "TEXT",
            })


def prune_deleted(engine: Engine, days: int = 14) -> int:
    """Убирает давно удалённые (мягко) задачи: окно Undo — сессия, не вечность."""
    with engine.begin() as connection:
        result = connection.execute(text("""
            DELETE FROM tasks
            WHERE is_deleted = 1 AND deleted_at IS NOT NULL
              AND deleted_at < datetime('now', :offset)
        """), {"offset": f"-{int(days)} days"})
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
#  Категории задач — правит панель управления (senior/mentor).
# ---------------------------------------------------------------------------

def _clean_color(value: Any) -> str:
    color = str(value or "").strip()
    return color if COLOR_RE.match(color) else FALLBACK_COLOR


def _limit(code: str, key: str, fallback: int) -> int:
    return modules.setting(code, key, fallback)


def all_categories(engine: Engine) -> list[dict[str, Any]]:
    """Категории для интерфейса: код, название, цвет и порядок."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT code, label, color, position FROM task_categories
            ORDER BY position, label
        """)).mappings().all()
    return [
        {
            "code": row["code"],
            "label": row["label"],
            "color": _clean_color(row["color"]),
            "position": int(row["position"]),
        }
        for row in rows
    ]


def category_map(engine: Engine) -> dict[str, dict[str, Any]]:
    return {item["code"]: item for item in all_categories(engine)}


def category_keys(engine: Engine) -> set[str]:
    return set(category_map(engine))


def _slug(engine: Engine, label: str) -> str:
    translit = "".join(TRANSLIT.get(character, character) for character in label.lower())
    base = re.sub(r"[^a-z0-9]+", "-", translit).strip("-") or "category"
    base = base[:40]
    existing = category_keys(engine)
    code, index = base, 2
    while code in existing:
        code = f"{base}-{index}"
        index += 1
    return code


def create_category(engine: Engine, label: Any, color: Any = None) -> str:
    clean_label = str(label or "").strip()[:_limit("tasks", "MAX_LABEL", MAX_LABEL)]
    if not clean_label:
        raise ValueError("Название категории обязательно")
    code = _slug(engine, clean_label)
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO task_categories (code, label, color, position)
            VALUES (:code, :label, :color,
                    COALESCE((SELECT MAX(position) FROM task_categories), 0) + 10)
        """), {"code": code, "label": clean_label, "color": _clean_color(color)})
    return code


def update_category(engine: Engine, code: str, label: Any = None, color: Any = None) -> dict[str, Any]:
    current = category_map(engine).get(code)
    if current is None:
        raise LookupError("Категория не найдена")
    values = {
        "code": code,
        "label": str(label).strip()[:_limit("tasks", "MAX_LABEL", MAX_LABEL)] if str(label or "").strip() else current["label"],
        "color": _clean_color(color if color is not None else current["color"]),
    }
    with engine.begin() as connection:
        connection.execute(text("""
            UPDATE task_categories SET label = :label, color = :color WHERE code = :code
        """), values)
    return category_map(engine)[code]


def delete_category(engine: Engine, code: str) -> None:
    """Удаляет категорию, если в ней нет задач (активных или архивных)."""
    if code not in category_keys(engine):
        raise LookupError("Категория не найдена")
    with engine.connect() as connection:
        used = int(connection.execute(text(
            "SELECT COUNT(*) FROM tasks WHERE category_code = :code"
        ), {"code": code}).scalar_one())
    if used:
        raise ValueError(f"В категории {used} задач — сначала перенесите или удалите их")
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM task_categories WHERE code = :code"), {"code": code})


# ---------------------------------------------------------------------------
#  Выборка задач: единый предикат видимости и счётчики.
# ---------------------------------------------------------------------------

_TASK_SELECT = """
    SELECT t.id, t.title, t.description, t.category_code, t.status, t.priority,
           t.meta_info, t.assignee_type, t.assignee_role, t.assignee_user_id,
           t.author_id, t.completed_by_id, t.completed_at, t.created_at,
           t.updated_at, t.position, t.is_deleted,
           c.label AS category_label, c.color AS category_color,
           COALESCE(a.display_name, a.name) AS author_name,
           COALESCE(u.display_name, u.name) AS assignee_name,
           COALESCE(d.display_name, d.name) AS completed_name
    FROM tasks t
    LEFT JOIN task_categories c ON c.code = t.category_code
    LEFT JOIN users a ON a.id = t.author_id
    LEFT JOIN users u ON u.id = t.assignee_user_id
    LEFT JOIN users d ON d.id = t.completed_by_id
"""


def _visibility(user: dict[str, Any], assignee: str) -> str:
    """SQL-условие видимости: общие + своя должность + лично мне.

    Чужие персональные задачи не возвращаются ни в одном режиме — «Все» это
    персональная выборка целиком, а не вся база.
    """
    if assignee == "shared":
        return "t.assignee_type = 'all'"
    mine = ("(t.assignee_type = 'role' AND t.assignee_role = :role) "
            "OR (t.assignee_type = 'user' AND t.assignee_user_id = :uid)")
    return f"({mine})" if assignee == "mine" else f"(t.assignee_type = 'all' OR {mine})"


def _iso(value: Any) -> str:
    """SQLite хранит UTC как «YYYY-MM-DD HH:MM:SS» — отдаём ISO с Z для браузера."""
    stamp = str(value or "").strip()
    return stamp.replace(" ", "T") + "Z" if stamp else ""


def _metas(value: Any) -> list[str]:
    """Мета-подвал карточки: «Смена: Утро», «Дедлайн: 14:00» — по строке на бейдж."""
    return [part.strip() for chunk in str(value or "").split("\n")
            for part in chunk.split(";") if part.strip()]


def _task_payload(row: Any) -> dict[str, Any]:
    role = row["assignee_role"] or ""
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "description": row["description"] or "",
        "category": row["category_code"],
        "category_label": row["category_label"] or row["category_code"],
        "category_color": _clean_color(row["category_color"]),
        "status": row["status"],
        "priority": row["priority"] or "normal",
        "meta": _metas(row["meta_info"]),
        "assignee": {
            "type": row["assignee_type"] or "all",
            "role": role,
            "role_label": ROLE_LABELS.get(role, ""),
            "user_id": int(row["assignee_user_id"]) if row["assignee_user_id"] else None,
            "name": row["assignee_name"] or "",
        },
        "author_id": int(row["author_id"]) if row["author_id"] else None,
        "author_name": row["author_name"] or "",
        "completed_at": _iso(row["completed_at"]),
        "completed_name": row["completed_name"] or "",
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "position": int(row["position"] or 0),
        "is_deleted": bool(row["is_deleted"]),
    }


def _params(user: dict[str, Any], limit: int) -> dict[str, Any]:
    return {"role": user["role"], "uid": user["id"], "limit": limit}


def list_active(engine: Engine, user: dict[str, Any], assignee: str = "all",
                category: str | None = None, limit: int = MAX_LIST) -> list[dict[str, Any]]:
    """Актуальные задачи доски (без архива и мягко удалённых)."""
    if assignee not in FILTERS:
        assignee = "all"
    params = _params(user, limit)
    where = [f"t.is_deleted = 0 AND t.status IN {BOARD_STATUSES}", _visibility(user, assignee)]
    if category:
        where.append("t.category_code = :category")
        params["category"] = category
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            {_TASK_SELECT}
            WHERE {" AND ".join(where)}
            ORDER BY CASE t.status WHEN 'todo' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
                     t.position, t.id
            LIMIT :limit
        """), params).mappings().all()
    return [_task_payload(row) for row in rows]


def list_archive(engine: Engine, user: dict[str, Any], assignee: str = "all",
                 category: str | None = None, limit: int = MAX_LIST) -> list[dict[str, Any]]:
    """Архив: выполненные и отменённые задачи, свежие сверху."""
    if assignee not in FILTERS:
        assignee = "all"
    params = _params(user, limit)
    where = [f"t.is_deleted = 0 AND t.status IN {ARCHIVE_STATUSES}", _visibility(user, assignee)]
    if category:
        where.append("t.category_code = :category")
        params["category"] = category
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            {_TASK_SELECT}
            WHERE {" AND ".join(where)}
            ORDER BY COALESCE(t.completed_at, t.updated_at) DESC, t.id DESC
            LIMIT :limit
        """), params).mappings().all()
    return [_task_payload(row) for row in rows]


def counts(engine: Engine, user: dict[str, Any], assignee: str = "all") -> dict[str, Any]:
    """Счётчики для колонок/табов, чипов категорий и вкладок адресации."""
    if assignee not in FILTERS:
        assignee = "all"
    params = _params(user, MAX_LIST)
    active = f"t.is_deleted = 0 AND t.status IN {BOARD_STATUSES}"
    with engine.connect() as connection:
        status_rows = connection.execute(text(f"""
            SELECT t.status, COUNT(*) AS total FROM tasks t
            WHERE {active} AND {_visibility(user, assignee)}
            GROUP BY t.status
        """), params).mappings().all()
        category_rows = connection.execute(text(f"""
            SELECT t.category_code, COUNT(*) AS total FROM tasks t
            WHERE {active} AND {_visibility(user, assignee)}
            GROUP BY t.category_code
        """), params).mappings().all()
        mine_rows = connection.execute(text(f"""
            SELECT COUNT(*) AS total FROM tasks t
            WHERE {active} AND {_visibility(user, "mine")}
        """), params).mappings().all()
    by_status = {status: 0 for status in BOARD_STATUSES}
    by_status.update({row["status"]: int(row["total"]) for row in status_rows})
    return {
        "by_status": by_status,
        "by_category": {row["category_code"]: int(row["total"]) for row in category_rows},
        "mine": int(mine_rows[0]["total"]) if mine_rows else 0,
        "total": sum(by_status.values()),
    }


def assignees(engine: Engine) -> dict[str, Any]:
    """Кого можно выбрать в адресации: должности и активные сотрудники."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT id, COALESCE(display_name, name) AS name, role FROM users
            WHERE is_active = 1 AND role IN ('newbie', 'barista', 'senior', 'mentor')
            ORDER BY role, name COLLATE NOCASE
        """)).mappings().all()
    return {
        "roles": [{"code": code, "label": label} for code, label in ROLE_LABELS.items()],
        "users": [
            {
                "id": int(row["id"]),
                "name": row["name"],
                "role": row["role"],
                "role_label": ROLE_LABELS.get(row["role"], ""),
            }
            for row in rows
        ],
    }


# ---------------------------------------------------------------------------
#  Изменения: создание, правка (перенос/архив/Undo), удаление.
# ---------------------------------------------------------------------------

def can_edit(task: dict[str, Any], user: dict[str, Any]) -> bool:
    """Кто может менять задачу.

    Автор и наставник — всегда; тот, кому задача адресована (лично, по
    должности или как общая), тоже: статус и текст правит вся смена, а не
    только автор — задача на то и командная.
    """
    if task["author_id"] == user["id"] or user["role"] in {"senior", "mentor"}:
        return True
    assignee = task.get("assignee") or {}
    if assignee.get("type") == "user":
        return assignee.get("user_id") == user["id"]
    if assignee.get("type") == "role":
        return assignee.get("role") == user["role"]
    return assignee.get("type") == "all"


def _clean_meta(value: Any) -> str:
    return "\n".join(_metas(value))[:modules.setting("tasks", "MAX_META", MAX_META)]


def _validate_assignee(engine: Engine, payload: dict[str, Any],
                       current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Приводит адресацию к тройке (тип, должность, сотрудник) с проверками."""
    base = current or {}
    atype = str(payload.get("assignee_type") or base.get("type") or "all").strip()
    if atype not in ASSIGNEE_TYPES:
        raise ValueError("Неизвестный тип адресации")
    if atype == "role":
        role = str(payload.get("assignee_role", base.get("role")) or "").strip()
        if role not in ASSIGNEE_ROLES:
            raise ValueError("Выберите должность из списка")
        return {"assignee_type": atype, "assignee_role": role, "assignee_user_id": None}
    if atype == "user":
        try:
            user_id = int(payload.get("assignee_user_id", base.get("user_id")))
        except (TypeError, ValueError):
            raise ValueError("Выберите сотрудника") from None
        with engine.connect() as connection:
            exists = connection.execute(text(
                "SELECT COUNT(*) FROM users WHERE id = :id AND is_active = 1"
            ), {"id": user_id}).scalar_one()
        if not exists:
            raise ValueError("Сотрудник не найден или отключён")
        return {"assignee_type": atype, "assignee_role": None, "assignee_user_id": user_id}
    return {"assignee_type": "all", "assignee_role": None, "assignee_user_id": None}


def _column_ids(connection: Any, status: str) -> list[int]:
    rows = connection.execute(text("""
        SELECT id FROM tasks WHERE status = :status AND is_deleted = 0 ORDER BY position, id
    """), {"status": status}).mappings().all()
    return [int(row["id"]) for row in rows]


def _save_order(connection: Any, ids: list[int]) -> None:
    for index, value in enumerate(ids, start=1):
        connection.execute(
            text("UPDATE tasks SET position = :position WHERE id = :id"),
            {"position": index * 10, "id": value},
        )


def _place(connection: Any, status: str, task_id: int, before_id: int | None) -> None:
    """Ставит задачу в колонку: перед `before_id`, иначе — в конец."""
    ids = [value for value in _column_ids(connection, status) if value != task_id]
    if before_id and before_id in ids:
        ids.insert(ids.index(before_id), task_id)
    else:
        ids.append(task_id)
    _save_order(connection, ids)


def _renumber(connection: Any, status: str) -> None:
    """Пересобирает порядок колонки после ухода задачи (без «дыр» в позициях)."""
    _save_order(connection, _column_ids(connection, status))


def _stamp() -> str:
    """Метка времени в формате SQLite («YYYY-MM-DD HH:MM:SS», UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def create_task(engine: Engine, user: dict[str, Any], payload: dict[str, Any]) -> int:
    """Создаёт задачу и ставит её в конец выбранной колонки."""
    title = str(payload.get("title") or "").strip()[:modules.setting("tasks", "MAX_TITLE", MAX_TITLE)]
    if not title:
        raise ValueError("Название задачи обязательно")
    category = str(payload.get("category_code") or "").strip()
    if category not in category_keys(engine):
        raise ValueError("Выберите категорию задачи")
    status = str(payload.get("status") or "todo").strip()
    if status not in BOARD_STATUSES:
        raise ValueError("Неизвестный этап доски")
    priority = str(payload.get("priority") or "normal").strip()
    if priority not in PRIORITIES:
        raise ValueError("Неизвестный приоритет")
    description = str(payload.get("description") or "").strip()[
        :modules.setting("tasks", "MAX_DESCRIPTION", MAX_DESCRIPTION)
    ]
    assignee = _validate_assignee(engine, payload)
    with engine.begin() as connection:
        result = connection.execute(text("""
            INSERT INTO tasks (title, description, category_code, status, priority, meta_info,
                               assignee_type, assignee_role, assignee_user_id, author_id, position)
            VALUES (:title, :description, :category_code, :status, :priority, :meta_info,
                    :assignee_type, :assignee_role, :assignee_user_id, :author_id, 0)
        """), {
            "title": title, "description": description, "category_code": category,
            "status": status, "priority": priority, "meta_info": _clean_meta(payload.get("meta_info")),
            "author_id": user["id"], **assignee,
        })
        task_id = int(result.lastrowid or 0)
        _place(connection, status, task_id, None)
    return task_id


def get_task(engine: Engine, task_id: int) -> dict[str, Any] | None:
    """Одна задача в том же виде, что отдают списки (включая мягко удалённые)."""
    with engine.connect() as connection:
        row = connection.execute(
            text(f"{_TASK_SELECT} WHERE t.id = :id"), {"id": task_id}
        ).mappings().first()
    return _task_payload(row) if row else None


def update_task(engine: Engine, task_id: int, user: dict[str, Any],
                payload: dict[str, Any]) -> dict[str, Any]:
    """Правка задачи: поля, перенос по этапам, порядок в колонке, архив и Undo."""
    task = get_task(engine, task_id)
    if task is None:
        raise LookupError("Задача не найдена")
    if not can_edit(task, user):
        raise PermissionError("Менять задачу может автор или наставник")

    values: dict[str, Any] = {}
    if "title" in payload:
        title = str(payload.get("title") or "").strip()[
            :modules.setting("tasks", "MAX_TITLE", MAX_TITLE)
        ]
        if not title:
            raise ValueError("Название задачи обязательно")
        values["title"] = title
    if "description" in payload:
        values["description"] = str(payload.get("description") or "").strip()[
            :modules.setting("tasks", "MAX_DESCRIPTION", MAX_DESCRIPTION)]
    if "category_code" in payload:
        category = str(payload.get("category_code") or "").strip()
        if category not in category_keys(engine):
            raise ValueError("Выберите категорию задачи")
        values["category_code"] = category
    if "priority" in payload:
        priority = str(payload.get("priority") or "").strip()
        if priority not in PRIORITIES:
            raise ValueError("Неизвестный приоритет")
        values["priority"] = priority
    if "meta_info" in payload:
        values["meta_info"] = _clean_meta(payload.get("meta_info"))
    if {"assignee_type", "assignee_role", "assignee_user_id"} & set(payload):
        values.update(_validate_assignee(engine, payload, task["assignee"]))
    if payload.get("is_deleted") is False:
        # Undo: карточка возвращается на доску на прежнее место.
        values["is_deleted"] = 0
        values["deleted_at"] = None

    status = task["status"]
    previous = None
    if "status" in payload:
        next_status = str(payload.get("status") or "").strip()
        if next_status not in STATUSES:
            raise ValueError("Неизвестный этап")
        if next_status != status:
            previous, status = status, next_status
            values["status"] = status
            if status == "done" or status in ARCHIVE_STATUSES:
                values["completed_by_id"] = user["id"]
                values["completed_at"] = _stamp()
            else:
                values["completed_by_id"] = None
                values["completed_at"] = None

    has_before = "before_id" in payload
    before_id = payload.get("before_id")
    if before_id is not None:
        try:
            before_id = int(before_id)
        except (TypeError, ValueError):
            before_id = None
    if not values and not has_before:
        return task
    if values:
        assignments = ", ".join(f"{key} = :{key}" for key in values)
        with engine.begin() as connection:
            connection.execute(text(
                f"UPDATE tasks SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = :task_id"
            ), {**values, "task_id": task_id})
            if previous:
                _renumber(connection, previous)
            if previous or has_before:
                _place(connection, status, task_id, before_id)
    return get_task(engine, task_id) or task


def delete_task(engine: Engine, task_id: int, user: dict[str, Any],
                permanent: bool = False) -> dict[str, Any]:
    """Мягкое удаление с окном Undo или окончательное удаление из архива."""
    task = get_task(engine, task_id)
    if task is None:
        raise LookupError("Задача не найдена")
    if not can_edit(task, user):
        raise PermissionError("Удалять задачу может автор или наставник")
    if permanent:
        if task["status"] not in ARCHIVE_STATUSES:
            raise ValueError("Окончательно удалять можно только архивные задачи")
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
        return task
    with engine.begin() as connection:
        connection.execute(text("""
            UPDATE tasks SET is_deleted = 1, deleted_at = CURRENT_TIMESTAMP,
                             updated_at = CURRENT_TIMESTAMP WHERE id = :id
        """), {"id": task_id})
        _renumber(connection, task["status"])
    return task
