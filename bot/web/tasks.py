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
* `role` — на должность из справочника `role_access_config`;
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

from bot.web import access_roles, modules, notifications, page_links
from bot.web.announcements import COLOR_RE, TRANSLIT
from bot.web.calendar_service import _ensure_columns

logger = logging.getLogger("bot.web.tasks")

# Категории задач не засеиваются шаблонами: единственный источник — панель
# управления (senior/mentor). Задача может жить и без категории («БЕЗ КАТЕГОРИИ»),
# поэтому новый модуль работает «из коробки» с пустым справочником.

STATUSES = ("todo", "in_progress", "done", "archived_done", "archived_cancelled")
BOARD_STATUSES = ("todo", "in_progress", "done")
ARCHIVE_STATUSES = ("archived_done", "archived_cancelled")
PRIORITIES = ("normal", "high", "urgent")
ASSIGNEE_TYPES = ("all", "role", "user")
# Режимы выборки: всё доступное, только моё, только общие.
FILTERS = ("all", "mine", "shared")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

FALLBACK_COLOR = "#7b8794"

MAX_TITLE = 120
MAX_DESCRIPTION = 2000
MAX_META = 120
MAX_LABEL = 60
MAX_LIST = 300

# Авто-архив: сколько дней выполненная задача остаётся в колонке «Готово»
# (0 — не архивировать автоматически). TRASH_KEEP_DAYS — срок жизни мягко
# удалённых задач в архиве (0 — хранить бессрочно).
ARCHIVE_AFTER_DAYS = 1
TRASH_KEEP_DAYS = 30

modules.register_defaults("tasks", {
    "MAX_TITLE": MAX_TITLE, "MAX_DESCRIPTION": MAX_DESCRIPTION,
    "MAX_META": MAX_META, "MAX_LABEL": MAX_LABEL,
    "ARCHIVE_AFTER_DAYS": ARCHIVE_AFTER_DAYS, "TRASH_KEEP_DAYS": TRASH_KEEP_DAYS,
})


_TASKS_DDL = """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        category_code TEXT,
        status TEXT NOT NULL DEFAULT 'todo',
        priority TEXT NOT NULL DEFAULT 'normal',
        meta_info TEXT,
        due_date TEXT,
        due_time TEXT,
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
"""

_TASK_COLUMNS = {
    "priority": "TEXT NOT NULL DEFAULT 'normal'",
    "meta_info": "TEXT",
    "due_date": "TEXT",
    "due_time": "TEXT",
    "assignee_role": "TEXT",
    "assignee_user_id": "INTEGER",
    "completed_by_id": "INTEGER",
    "completed_at": "TEXT",
    "position": "INTEGER NOT NULL DEFAULT 0",
    "is_deleted": "INTEGER NOT NULL DEFAULT 0",
    "deleted_at": "TEXT",
}


def _relax_category_nullable(connection: Any) -> None:
    """Снимает NOT NULL с `category_code` на базах прошлой версии.

    Категория стала необязательной, а SQLite не умеет менять ограничения через
    ALTER: таблицу пересобираем (данные копируются, индексы создаются следом).
    """
    info = connection.execute(text("PRAGMA table_info(tasks)")).mappings().all()
    column = next((row for row in info if row["name"] == "category_code"), None)
    if column is None or not int(column["notnull"]):
        return
    logger.info("tasks: category_code теперь необязательна — пересобираю таблицу задач")
    columns = [row["name"] for row in info]
    connection.execute(text("ALTER TABLE tasks RENAME TO tasks_legacy"))
    connection.execute(text(_TASKS_DDL))
    fresh = [row["name"] for row in connection.execute(text("PRAGMA table_info(tasks)")).mappings().all()]
    shared = [name for name in fresh if name in columns]
    names = ", ".join(shared)
    connection.execute(text(f"INSERT INTO tasks ({names}) SELECT {names} FROM tasks_legacy"))
    connection.execute(text("DROP TABLE tasks_legacy"))


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
        # Категории не засеиваются: их ведёт панель управления (см. create_category).
        connection.execute(text(_TASKS_DDL))
        if engine.dialect.name == "sqlite":
            # Поля докатываются и на живой базе — таблица живёт между релизами.
            _ensure_columns(connection, "tasks", _TASK_COLUMNS)
            _relax_category_nullable(connection)
        for name, ddl in (
            ("idx_tasks_column", "(status, position)"),
            ("idx_tasks_category", "(category_code)"),
            ("idx_tasks_assignee", "(assignee_type, assignee_user_id, assignee_role)"),
        ):
            connection.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON tasks {ddl}"))


def archive_after_days() -> int:
    """Сколько дней «Готово» держится на доске до авто-архива (0 — выключено)."""
    try:
        return int(modules.setting("tasks", "ARCHIVE_AFTER_DAYS", ARCHIVE_AFTER_DAYS))
    except (TypeError, ValueError):
        return ARCHIVE_AFTER_DAYS


def auto_archive(engine: Engine, days: int | None = None) -> int:
    """Переводит выполненные задачи в архив через N дней после выполнения.

    Планировщика в приложении нет, поэтому уборка ленивая: вызывается на старте
    и перед выдачей доски — так счётчики колонок всегда актуальны.
    """
    if days is None:
        days = archive_after_days()
    if days <= 0:
        return 0
    with engine.begin() as connection:
        result = connection.execute(text("""
            UPDATE tasks SET status = 'archived_done', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'done' AND is_deleted = 0 AND completed_at IS NOT NULL
              AND completed_at <= datetime('now', :offset)
        """), {"offset": f"-{days} days"})
    return int(result.rowcount or 0)


def prune_deleted(engine: Engine, days: int | None = None) -> int:
    """Убирает удалённые (мягко) задачи старше TRASH_KEEP_DAYS.

    Удалённая задача живёт в архиве и может быть восстановлена; срок хранения
    правится в панели управления (0 — хранить бессрочно).
    """
    if days is None:
        days = modules.setting("tasks", "TRASH_KEEP_DAYS", TRASH_KEEP_DAYS)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = TRASH_KEEP_DAYS
    if days <= 0:
        return 0
    with engine.begin() as connection:
        result = connection.execute(text("""
            DELETE FROM tasks
            WHERE is_deleted = 1 AND deleted_at IS NOT NULL
              AND deleted_at < datetime('now', :offset)
        """), {"offset": f"-{days} days"})
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
           t.meta_info, t.due_date, t.due_time,
           t.assignee_type, t.assignee_role, t.assignee_user_id,
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


def _roles_of(user: dict[str, Any]) -> list[str]:
    """Должности сотрудника: их может быть несколько (`user_roles`, сессия).

    «Моя» задача — та, что адресована любой из его должностей, поэтому фильтр по
    должности в SQL идёт списком, а не одним значением.
    """
    roles = [str(key) for key in (user.get("roles") or []) if key]
    if roles:
        return roles
    return [str(user["role"])] if user.get("role") else [""]


def _visibility(user: dict[str, Any], assignee: str) -> str:
    """SQL-условие видимости: общие + мои должности + лично мне + созданные мной.

    Автор видит задачу, которую поставил коллеге — иначе выданная задача
    пропадала бы из его поля зрения сразу после создания.
    """
    if assignee == "shared":
        return "t.assignee_type = 'all'"
    placeholders = ", ".join(f":role{index}" for index in range(len(_roles_of(user))))
    mine = (f"(t.assignee_type = 'role' AND t.assignee_role IN ({placeholders})) "
            "OR (t.assignee_type = 'user' AND t.assignee_user_id = :uid) "
            "OR t.author_id = :uid")
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
        "category": row["category_code"] or "",
        "category_label": row["category_label"] or "",
        "category_color": _clean_color(row["category_color"]),
        "status": row["status"],
        "priority": row["priority"] or "normal",
        "meta": _metas(row["meta_info"]),
        "due_date": row["due_date"] or "",
        "due_time": row["due_time"] or "",
        "assignee": {
            "type": row["assignee_type"] or "all",
            "role": role,
            "role_label": access_roles.title_of(role),
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
    """Параметры запросов: должности идут списком (`:role0`, `:role1`, …)."""
    params: dict[str, Any] = {"uid": user["id"], "limit": limit}
    for index, key in enumerate(_roles_of(user)):
        params[f"role{index}"] = key
    return params


NO_CATEGORY = "none"


def _apply_category(category: str | None, where: list[str], params: dict[str, Any]) -> None:
    """Фильтр по категории; NO_CATEGORY — только задачи без категории."""
    if not category:
        return
    if category == NO_CATEGORY:
        where.append("(t.category_code IS NULL OR t.category_code = '')")
        return
    where.append("t.category_code = :category")
    params["category"] = category


def list_active(engine: Engine, user: dict[str, Any], assignee: str = "all",
                category: str | None = None, limit: int = MAX_LIST) -> list[dict[str, Any]]:
    """Актуальные задачи доски (без архива и мягко удалённых)."""
    if assignee not in FILTERS:
        assignee = "all"
    params = _params(user, limit)
    where = [f"t.is_deleted = 0 AND t.status IN {BOARD_STATUSES}", _visibility(user, assignee)]
    _apply_category(category, where, params)
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            {_TASK_SELECT}
            WHERE {" AND ".join(where)}
            ORDER BY CASE t.status WHEN 'todo' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
                     t.position, t.id
            LIMIT :limit
        """), params).mappings().all()
    return [_task_payload(row) for row in rows]


def list_overview(engine: Engine, user: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    """Виджет на «Обзоре»: до пяти свежих незакрытых задач.

    Видимость — та же, что у режима «Все» на доске: задача касается меня, если
    она общая, адресована моей должности, лично мне или создана мной. Этапы
    `done` и архив в виджет не попадают: актуально то, что ещё не закрыто.
    """
    visible = _visibility(user, "all")
    params = _params(user, limit)
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            {_TASK_SELECT}
            WHERE t.is_deleted = 0 AND t.status IN ('todo', 'in_progress')
              AND {visible}
            ORDER BY t.created_at DESC, t.id DESC
            LIMIT :limit
        """), params).mappings().all()
    return [_task_payload(row) for row in rows]


def list_archive(engine: Engine, user: dict[str, Any], assignee: str = "all",
                 category: str | None = None, limit: int = MAX_LIST) -> list[dict[str, Any]]:
    """Архив: выполненные, отменённые и мягко удалённые задачи, свежие сверху.

    Удалённая задача не исчезает совсем: она лежит в архиве до восстановления
    или до истечения TRASH_KEEP_DAYS — так с доски ничего не пропадает молча.
    """
    if assignee not in FILTERS:
        assignee = "all"
    params = _params(user, limit)
    where = [f"(t.status IN {ARCHIVE_STATUSES} OR t.is_deleted = 1)", _visibility(user, assignee)]
    _apply_category(category, where, params)
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            {_TASK_SELECT}
            WHERE {" AND ".join(where)}
            ORDER BY COALESCE(t.deleted_at, t.completed_at, t.updated_at) DESC, t.id DESC
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
        "by_category": {(row["category_code"] or ""): int(row["total"]) for row in category_rows},
        "mine": int(mine_rows[0]["total"]) if mine_rows else 0,
        "total": sum(by_status.values()),
    }


def assignees(engine: Engine) -> dict[str, Any]:
    """Кого можно выбрать в адресации: должности и активные сотрудники.

    Сотрудник попадает в список, если у него есть хотя бы одна активная должность
    (`user_roles`), а в карточке видны все его должности.
    """
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT u.id, COALESCE(u.display_name, u.name) AS name, u.role FROM users u
            WHERE u.is_active = 1 AND {access_roles.has_active_role_sql("u")}
            ORDER BY u.role, name COLLATE NOCASE
        """)).mappings().all()
        roles_map = access_roles.user_roles_map(connection)
    return {
        "roles": [{"code": key, "label": access_roles.title_of(key)} for key in access_roles.role_keys()],
        "users": [
            {
                "id": int(row["id"]),
                "name": row["name"],
                "role": row["role"],
                "role_label": access_roles.title_of(row["role"]),
                "roles": [item["key"] for item in roles_map.get(int(row["id"]), [])],
            }
            for row in rows
        ],
    }


# ---------------------------------------------------------------------------
#  Изменения: создание, правка (перенос/архив/Undo), удаление.
# ---------------------------------------------------------------------------

def can_edit(task: dict[str, Any], user: dict[str, Any]) -> bool:
    """Кто может менять задачу.

    Автор и право «Задачи» — всегда; тот, кому задача адресована (лично, по любой
    из его должностей или как общая), тоже: статус и текст правит вся смена, а не
    только автор — задача на то и командная.
    """
    if task["author_id"] == user["id"] or access_roles.allows(user, "tasks"):
        return True
    assignee = task.get("assignee") or {}
    if assignee.get("type") == "user":
        return assignee.get("user_id") == user["id"]
    if assignee.get("type") == "role":
        return assignee.get("role") in _roles_of(user)
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
        if role not in access_roles.role_keys(active_only=False):
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


def _notify_task_assigned(engine: Engine, task_id: int, title: str,
                          assignee: dict[str, Any], actor: dict[str, Any]) -> None:
    """Единый центр уведомлений: «вам поставили задачу» (по адресации).

    Адресация повторяет видимость задачи: лично — одному сотруднику, на
    должность — роли, общая — всей команде. Автор действия уведомление не
    получает (его отсекает сам центр).
    """
    try:
        url = f"/?view=tasks&task={task_id}"
        body = f"{actor.get('display_name') or actor.get('name') or 'Коллега'} поставил задачу"
        kind = str(assignee.get("assignee_type") or "all")
        actor_id = int(actor["id"])
        if kind == "user" and assignee.get("assignee_user_id"):
            notifications.notify_async(
                engine, [int(assignee["assignee_user_id"])], "task_assigned", title, body, url,
                entity_type="task", entity_id=task_id, actor_id=actor_id,
            )
            return
        roles = (str(assignee["assignee_role"]),) if kind == "role" and assignee.get("assignee_role") else None
        # Общая задача на всю команду идёт только в ленту: push на каждый
        # «для всех» был бы спамом, а персональные и должностные — со push.
        notifications.notify_audience_async(
            engine, "task_assigned", title, body, url,
            entity_type="task", entity_id=task_id, actor_id=actor_id, roles=roles,
            **({} if roles else {"channels": ("in_app",)}),
        )
    except Exception as error:  # noqa: BLE001 — уведомление не должно ломать задачу
        logger.warning("Задачи: уведомление не отправлено: %s", error)


def _notify_task_completed(engine: Engine, task: dict[str, Any], actor: dict[str, Any]) -> None:
    """«Задача выполнена»: сообщаем автору, если закрыл её кто-то другой."""
    try:
        author_id = task.get("author_id")
        if not author_id:
            return
        url = f"/?view=tasks&task={task['id']}"
        body = f"{actor.get('display_name') or actor.get('name') or 'Коллега'} перевёл задачу в «Готово»"
        notifications.notify_async(
            engine, [int(author_id)], "task_completed", str(task.get("title") or "Задача"),
            body, url, entity_type="task", entity_id=int(task["id"]), actor_id=int(actor["id"]),
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Задачи: уведомление о выполнении не отправлено: %s", error)


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


def _clean_category(engine: Engine, value: Any) -> str | None:
    """Категория необязательна: пусто → NULL, карточка покажет «Без категории»."""
    code = str(value or "").strip()
    if not code or code == "all":
        return None
    if code not in category_keys(engine):
        raise ValueError("Такой категории нет — выберите из списка")
    return code


def _clean_deadline(payload: dict[str, Any],
                    current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Дедлайн — необязательная пара «дата + время».

    Пустая дата снимает дедлайн целиком: время без даты смысла не имеет.
    """
    base = current or {}
    due_date = str(payload.get("due_date", base.get("due_date")) or "").strip()
    due_time = str(payload.get("due_time", base.get("due_time")) or "").strip()
    if due_date and not DATE_RE.match(due_date):
        raise ValueError("Дедлайн: дата в формате ГГГГ-ММ-ДД")
    if due_time and not TIME_RE.match(due_time):
        raise ValueError("Дедлайн: время в формате ЧЧ:ММ")
    if not due_date:
        if due_time:
            raise ValueError("Дедлайн: сначала выберите дату, потом время")
        return {"due_date": None, "due_time": None}
    return {"due_date": due_date, "due_time": due_time or None}


def create_task(engine: Engine, user: dict[str, Any], payload: dict[str, Any]) -> int:
    """Создаёт задачу и ставит её в конец выбранной колонки."""
    title = str(payload.get("title") or "").strip()[:modules.setting("tasks", "MAX_TITLE", MAX_TITLE)]
    if not title:
        raise ValueError("Название задачи обязательно")
    category = _clean_category(engine, payload.get("category_code"))
    status = str(payload.get("status") or "todo").strip()
    if status not in BOARD_STATUSES:
        raise ValueError("Неизвестный этап доски")
    priority = str(payload.get("priority") or "normal").strip()
    if priority not in PRIORITIES:
        raise ValueError("Неизвестный приоритет")
    # Обрезка не рвёт маркер ссылки `@[Заголовок](kb:12)` — иначе в карточке
    # останется служебный огрызок вместо читаемого текста.
    description = page_links.truncate_safe(
        str(payload.get("description") or "").strip(),
        modules.setting("tasks", "MAX_DESCRIPTION", MAX_DESCRIPTION),
    )
    assignee = _validate_assignee(engine, payload)
    with engine.begin() as connection:
        result = connection.execute(text("""
            INSERT INTO tasks (title, description, category_code, status, priority, meta_info,
                               due_date, due_time,
                               assignee_type, assignee_role, assignee_user_id, author_id, position)
            VALUES (:title, :description, :category_code, :status, :priority, :meta_info,
                    :due_date, :due_time,
                    :assignee_type, :assignee_role, :assignee_user_id, :author_id, 0)
        """), {
            "title": title, "description": description, "category_code": category,
            "status": status, "priority": priority, "meta_info": _clean_meta(payload.get("meta_info")),
            "author_id": user["id"], **_clean_deadline(payload), **assignee,
        })
        task_id = int(result.lastrowid or 0)
        _place(connection, status, task_id, None)
    _notify_task_assigned(engine, task_id, title, assignee, user)
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
        raise PermissionError("Менять задачу может автор или право «Задачи»")

    values: dict[str, Any] = {}
    assignee_change: dict[str, Any] | None = None
    if "title" in payload:
        title = str(payload.get("title") or "").strip()[
            :modules.setting("tasks", "MAX_TITLE", MAX_TITLE)
        ]
        if not title:
            raise ValueError("Название задачи обязательно")
        values["title"] = title
    if "description" in payload:
        values["description"] = page_links.truncate_safe(
            str(payload.get("description") or "").strip(),
            modules.setting("tasks", "MAX_DESCRIPTION", MAX_DESCRIPTION),
        )
    if "category_code" in payload:
        values["category_code"] = _clean_category(engine, payload.get("category_code"))
    if {"due_date", "due_time"} & set(payload):
        values.update(_clean_deadline(payload, task))
    if "priority" in payload:
        priority = str(payload.get("priority") or "").strip()
        if priority not in PRIORITIES:
            raise ValueError("Неизвестный приоритет")
        values["priority"] = priority
    if "meta_info" in payload:
        values["meta_info"] = _clean_meta(payload.get("meta_info"))
    if {"assignee_type", "assignee_role", "assignee_user_id"} & set(payload):
        assignee_change = _validate_assignee(engine, payload, task["assignee"])
        values.update(assignee_change)
    if payload.get("is_deleted") is False:
        # Undo: карточка возвращается на доску; из архива её достаёт status='todo'.
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
    # События Единого центра: закрытие задачи и смена адресата. Отправляем уже
    # после коммита — фоновая запись ленты не должна ловить блокировку SQLite.
    if previous and status in {"done", *ARCHIVE_STATUSES}:
        _notify_task_completed(engine, {**task, "status": status}, user)
    if assignee_change:
        before = task.get("assignee") or {}
        changed = (
            assignee_change.get("assignee_type"), assignee_change.get("assignee_role"),
            assignee_change.get("assignee_user_id"),
        ) != (before.get("type"), before.get("role"), before.get("user_id"))
        if changed:
            _notify_task_assigned(
                engine, task_id, str(values.get("title") or task["title"]), assignee_change, user
            )
    return get_task(engine, task_id) or task


def delete_task(engine: Engine, task_id: int, user: dict[str, Any],
                permanent: bool = False) -> dict[str, Any]:
    """Мягкое удаление с окном Undo или окончательное удаление из архива."""
    task = get_task(engine, task_id)
    if task is None:
        raise LookupError("Задача не найдена")
    if not can_edit(task, user):
        raise PermissionError("Удалять задачу может автор или право «Задачи»")
    if permanent:
        # Окончательно сносим только то, что уже лежит в архиве: либо архивный
        # статус, либо мягкое удаление (удалённая задача тоже видна в архиве).
        if task["status"] not in ARCHIVE_STATUSES and not task["is_deleted"]:
            raise ValueError("Окончательно удалять можно только из архива — сначала перенесите задачу")
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
