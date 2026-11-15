"""Должности команды и уровни доступа — единственный источник истины.

До этого модуля роль была перечислением в коде: гейты писали
`user["role"] in {"senior", "mentor"}`, а списки сотрудников — SQL
`role IN ('newbie','barista','senior','mentor')`. Теперь должности живут в
таблице `role_access_config`, а гейты сравнивают числовой `access_level`
с порогом.

Пороги считаются из данных, а не задаются константой:
* `manager_min_level()` — минимальный уровень среди должностей с `manage`;
* `mentor_min_level()`  — минимальный уровень среди должностей с `mentor`.
Поэтому добавленная в панели должность «Старший бариста» с уровнем 12 и
возможностью `manage` автоматически становится управленческой.

Возможности (`capabilities`, CSV) — то, что числовым уровнем не выражается,
потому что наборы не монотонны (например, `xp_base` у бариста и старшего, но
не у наставника с самым высоким уровнем):
* `manage`        — управленческий доступ: панель управления, команда, аналитика;
* `mentor`        — инструменты наставника: правка оценок, оценка напитков;
* `xp_base`       — базовый опыт при назначении на должность;
* `evaluated`     — сотрудника можно оценивать (контроль качества);
* `schedule_edit` — правка замен и расписания.

Инициализация идемпотентна: схема — `CREATE TABLE IF NOT EXISTS`, дефолты —
`INSERT OR IGNORE`, поэтому правки наставника рестарт не перетирает. Значения
читаются из кэша уровня модуля (как настройки модулей в `modules.py`); кэш
наполняется на старте и пересобирается сохранением из панели управления.
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Возможности в порядке отображения в панели управления.
CAPABILITIES: tuple[str, ...] = ("manage", "mentor", "xp_base", "evaluated", "schedule_edit")
CAPABILITY_TITLES: dict[str, str] = {
    "manage": "Управление",
    "mentor": "Наставничество",
    "xp_base": "Базовый опыт",
    "evaluated": "Можно оценивать",
    "schedule_edit": "Правка графика",
}

MAX_TITLE = 60
MAX_DESCRIPTION = 400
MAX_LEVEL = 1000

# Дефолтные должности: (ключ, название, уровень, описание, возможности, порядок).
# Уровни сохраняют прежнюю иерархию: 0 < 5 < 10 < 15.
DEFAULT_ROLES: tuple[tuple[str, str, int, str, str, int], ...] = (
    ("newbie", "Стажёр", 0,
     "Первые месяцы: график, база знаний, чат и свой прогресс без правок.",
     "evaluated", 10),
    ("barista", "Бариста", 5,
     "Полная смена: задачи, правка замен, описания напитков, оценки качества.",
     "xp_base,evaluated,schedule_edit", 20),
    ("senior", "Старший", 10,
     "Ведёт смену: команда, аналитика и панель управления сайтом.",
     "manage,xp_base,evaluated,schedule_edit", 30),
    ("mentor", "Наставник", 15,
     "Обучает и оценивает: правка оценок, оценка напитков, все права старшего.",
     "manage,mentor,schedule_edit", 40),
)

# Ключ псевдо-роли для заявок на доступ: это не должность, а статус заявки,
# поэтому в справочник он не попадает.
PENDING_ROLE = "guest"

_DDL = """
CREATE TABLE IF NOT EXISTS role_access_config (
    role_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    access_level INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    capabilities TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Кэш уровня модуля: ключ → запись, как в `modules._settings_values`.
_roles: dict[str, dict[str, Any]] = {}
_levels: dict[str, int] = {}


def _capability_csv(raw: Any) -> str:
    """Приводит возможности к каноническому виду `manage,xp_base` (без пробелов).

    Канонический вид нужен и для отображения, и для поиска в SQL
    (`instr(',' || capabilities || ',', ',manage,')`).
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        items: Iterable[Any] = raw.replace(";", ",").split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        raise ValueError("Возможности должны быть строкой или списком")
    provided = {str(item).strip() for item in items}
    return ",".join(code for code in CAPABILITIES if code in provided)


def _capabilities(raw: Any) -> tuple[str, ...]:
    return tuple(code for code in _capability_csv(raw).split(",") if code)


def _record(row: Any) -> dict[str, Any]:
    return {
        "role_key": str(row["role_key"]),
        "title": str(row["title"]),
        "access_level": int(row["access_level"] or 0),
        "description": row["description"] or "",
        "capabilities": _capability_csv(row["capabilities"]),
        "is_active": int(row["is_active"] or 0),
        "sort_order": int(row["sort_order"] or 0),
    }


def _bootstrap() -> None:
    """Дефолты в кэш: модуль работает до первого чтения из базы (и без неё)."""
    for role_key, title, level, description, capabilities, order in DEFAULT_ROLES:
        _roles[role_key] = {
            "role_key": role_key, "title": title, "access_level": level,
            "description": description, "capabilities": capabilities,
            "is_active": 1, "sort_order": order,
        }
    _rebuild_levels()


def _rebuild_levels() -> None:
    _levels.clear()
    for role_key, role in _roles.items():
        _levels[role_key] = int(role["access_level"])


def _load(engine: Engine) -> None:
    """Перечитывает справочник из базы (включая отключённые должности)."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT role_key, title, access_level, description, capabilities, is_active, sort_order
            FROM role_access_config
        """)).mappings().all()
    if not rows:
        return
    for row in rows:
        role = _record(row)
        _roles[role["role_key"]] = role
    _rebuild_levels()


def initialize_schema(engine: Engine) -> None:
    """Создаёт справочник, досеивает дефолты и наполняет кэш (идемпотентно)."""
    with engine.begin() as connection:
        connection.execute(text(_DDL))
        for role_key, title, level, description, capabilities, order in DEFAULT_ROLES:
            connection.execute(text("""
                INSERT OR IGNORE INTO role_access_config
                    (role_key, title, access_level, description, capabilities, is_active, sort_order)
                VALUES (:role_key, :title, :level, :description, :capabilities, 1, :order)
            """), {"role_key": role_key, "title": title, "level": level,
                   "description": description, "capabilities": capabilities, "order": order})
    _load(engine)


def refresh(engine: Engine) -> None:
    """Пересобирает кэш уровней (вызывается после правки справочника)."""
    _load(engine)


# --- Чтение из кэша ---------------------------------------------------------- #


def _ordered_roles() -> list[dict[str, Any]]:
    return sorted(_roles.values(),
                  key=lambda role: (int(role["sort_order"]), int(role["access_level"]), role["role_key"]))


def role_keys(active_only: bool = True) -> tuple[str, ...]:
    """Ключи должностей в порядке справочника; `active_only` — включённые."""
    return tuple(role["role_key"] for role in _ordered_roles() if not active_only or role["is_active"])


def staff_roles() -> tuple[str, ...]:
    """Активные должности — замена прежним `STAFF_ROLES`/`WORK_ROLES`/`ALL_ROLES`."""
    return role_keys()


def labels() -> dict[str, str]:
    """Ключ должности → читаемое название (замена `ROLE_NAMES`)."""
    return {role["role_key"]: role["title"] for role in _roles.values()}


def level_of(user_or_role: Any) -> int:
    """Числовой уровень доступа: по пользователю (сессия) или по ключу роли."""
    if isinstance(user_or_role, dict):
        # Сессия уже отдаёт уровень из справочника — берём его как есть;
        # вычисление по ключу нужно для записей без JOIN (офлайн-кеш клиента).
        raw = user_or_role.get("access_level")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return 0
        return _levels.get(str(user_or_role.get("role") or ""), 0)
    return _levels.get(str(user_or_role or ""), 0)


def title_of(role: Any) -> str:
    """Читаемое название должности; неизвестный ключ остаётся как есть."""
    record = _roles.get(str(role or ""))
    return record["title"] if record else str(role or "")


def capabilities_of(user_or_role: Any) -> tuple[str, ...]:
    """Возможности должности: по пользователю или по ключу."""
    key = str(user_or_role.get("role") if isinstance(user_or_role, dict) else user_or_role or "")
    record = _roles.get(key)
    return _capabilities(record["capabilities"]) if record else ()


def has_capability(user_or_role: Any, code: str) -> bool:
    return code in capabilities_of(user_or_role)


def roles_with_capability(code: str) -> tuple[str, ...]:
    """Активные должности с возможностью `code` — для SQL-фильтров и UI."""
    return tuple(key for key in role_keys() if code in _capabilities(_roles[key]["capabilities"]))


# --- Пороги доступа ---------------------------------------------------------- #


def _min_level_for(code: str) -> int:
    """Минимальный уровень среди активных должностей с возможностью `code`.

    Нет ни одной такой должности — порог выше любого возможного уровня, то есть
    возможность не даёт прав никому. Инвариант панели не позволяет остаться без
    управляющих, но справочник можно испортить вручную через БД.
    """
    levels = [int(_roles[key]["access_level"]) for key in roles_with_capability(code)]
    return min(levels) if levels else MAX_LEVEL + 1


def manager_min_level() -> int:
    """Порог управленческого доступа (бывший хардкод `{"senior", "mentor"}`)."""
    return _min_level_for("manage")


def mentor_min_level() -> int:
    """Порог инструментов наставника (бывший хардкод `role != "mentor"`)."""
    return _min_level_for("mentor")


def is_manager(user_or_role: Any) -> bool:
    """Управленческий доступ: возможность `manage` и уровень не ниже порога."""
    return has_capability(user_or_role, "manage") and level_of(user_or_role) >= manager_min_level()


def is_mentor(user_or_role: Any) -> bool:
    return has_capability(user_or_role, "mentor") and level_of(user_or_role) >= mentor_min_level()


def grants_base_xp(user_or_role: Any) -> bool:
    """Право на базовый опыт при назначении (бывший хардкод `{"barista","senior"}`)."""
    return has_capability(user_or_role, "xp_base")


def is_evaluated(user_or_role: Any) -> bool:
    """Должность, которую оценивают в контроле качества."""
    return has_capability(user_or_role, "evaluated")


def can_edit_schedule(user_or_role: Any) -> bool:
    """Право править замены и расписание (бывший хардкод `role != "newbie"`)."""
    return has_capability(user_or_role, "schedule_edit")


def staff_level() -> int:
    """Порог «полноценный сотрудник»: минимальный уровень среди правящих график."""
    levels = [int(_roles[key]["access_level"]) for key in roles_with_capability("schedule_edit")]
    return min(levels) if levels else 0


def highest_role() -> dict[str, Any]:
    """Должность с максимальным уровнем — для системных действий от имени команды."""
    ordered = sorted(_roles.values(), key=lambda role: (int(role["access_level"]), role["role_key"]))
    return dict(ordered[-1]) if ordered else {"role_key": "", "access_level": 0, "capabilities": ""}


# --- Справочник для панели управления --------------------------------------- #


def list_roles(engine: Engine) -> list[dict[str, Any]]:
    """Все должности с числом активных сотрудников — для панели управления."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT r.role_key, r.title, r.access_level, r.description, r.capabilities,
                   r.is_active, r.sort_order,
                   (SELECT COUNT(*) FROM users u
                     WHERE u.role = r.role_key AND u.is_active = 1) AS user_count
            FROM role_access_config r
            ORDER BY r.sort_order, r.access_level, r.role_key
        """)).mappings().all()
    result: list[dict[str, Any]] = []
    for row in rows:
        role = _record(row)
        role["user_count"] = int(row["user_count"] or 0)
        role["capability_list"] = list(_capabilities(role["capabilities"]))
        result.append(role)
    return result


def view(engine: Engine) -> dict[str, Any]:
    """Справочник + вычисленные пороги и словарь возможностей для интерфейса."""
    return {
        "roles": list_roles(engine),
        "thresholds": {
            "manager_min_level": manager_min_level(),
            "mentor_min_level": mentor_min_level(),
            "staff_level": staff_level(),
        },
        "capabilities": [{"code": code, "title": CAPABILITY_TITLES.get(code, code)} for code in CAPABILITIES],
        "pending_role": PENDING_ROLE,
    }


def _clean_title(value: Any) -> str:
    title = str(value or "").strip()
    if not title or len(title) > MAX_TITLE:
        raise ValueError(f"Название должности должно содержать от 1 до {MAX_TITLE} символов")
    return title


def _clean_description(value: Any) -> str:
    if value is None:
        return ""
    cleaned = str(value).strip()
    if len(cleaned) > MAX_DESCRIPTION:
        raise ValueError(f"Описание не длиннее {MAX_DESCRIPTION} символов")
    return cleaned


def _clean_level(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Уровень доступа должен быть числом")
    try:
        level = int(value)
    except (TypeError, ValueError):
        raise ValueError("Уровень доступа должен быть числом") from None
    if level < 0 or level > MAX_LEVEL:
        raise ValueError(f"Уровень доступа должен быть от 0 до {MAX_LEVEL}")
    return level


def other_manager_count(connection: Any, exclude_user_id: int) -> int:
    """Сколько активных управляющих останется, если исключить сотрудника.

    Нужно точечной правке сотрудника: понижение должности или отключение не
    должно оставить команду без управления. Возможность `manage` проверяется в
    SQL по канонической строке возможностей.
    """
    return int(connection.execute(text("""
        SELECT COUNT(*) FROM users u
        JOIN role_access_config r ON r.role_key = u.role
        WHERE u.id != :exclude_id AND u.is_active = 1 AND r.is_active = 1
          AND instr(',' || r.capabilities || ',', ',manage,') > 0
    """), {"exclude_id": exclude_user_id}).scalar_one() or 0)


def _active_manager_count(connection: Any) -> int:
    """Сколько активных сотрудников сохраняют управленческий доступ.

    Считается по состоянию справочника внутри транзакции правки: если она сняла
    права у всех, `update_role` выбрасывает ошибку и изменения откатываются.
    """
    rows = connection.execute(text("""
        SELECT r.access_level AS level, r.capabilities AS capabilities, COUNT(u.id) AS users
        FROM role_access_config r
        JOIN users u ON u.role = r.role_key AND u.is_active = 1
        WHERE r.is_active = 1
        GROUP BY r.role_key
    """)).mappings().all()
    managers = [row for row in rows if "manage" in _capabilities(row["capabilities"])]
    if not managers:
        return 0
    threshold = min(int(row["level"] or 0) for row in managers)
    return sum(int(row["users"]) for row in managers if int(row["level"] or 0) >= threshold)


def update_role(engine: Engine, role_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Обновляет должность и проверяет, что управляющие в системе остались.

    Меняются только название, описание, уровень, возможности и активность: ключ
    должности — это значение `users.role`, история оценок, задач и графика
    ссылается на него, поэтому ключ неизменен.
    """
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Не передано ни одного поля для сохранения")
    allowed = {"title", "description", "access_level", "capabilities", "is_active"}
    if set(payload) - allowed:
        raise ValueError("Переданы неизвестные поля")

    values: dict[str, Any] = {}
    if "title" in payload:
        values["title"] = _clean_title(payload["title"])
    if "description" in payload:
        values["description"] = _clean_description(payload["description"])
    if "access_level" in payload:
        values["access_level"] = _clean_level(payload["access_level"])
    if "capabilities" in payload:
        values["capabilities"] = _capability_csv(payload["capabilities"])
    if "is_active" in payload:
        if not isinstance(payload["is_active"], bool):
            raise ValueError("Признак активности должности должен быть булевым")
        values["is_active"] = int(payload["is_active"])

    with engine.begin() as connection:
        exists = connection.execute(
            text("SELECT role_key FROM role_access_config WHERE role_key = :role_key"),
            {"role_key": role_key},
        ).first()
        if not exists:
            raise LookupError("Должность не найдена")
        assignments = ", ".join(f"{name} = :{name}" for name in values)
        connection.execute(text(f"""
            UPDATE role_access_config
            SET {assignments}, updated_at = CURRENT_TIMESTAMP
            WHERE role_key = :role_key
        """), {**values, "role_key": role_key})
        if not _active_manager_count(connection):
            raise ValueError("В системе должен остаться хотя бы один управляющий: "
                             "правка снимает управленческий доступ у всей команды")
    refresh(engine)
    for role in list_roles(engine):
        if role["role_key"] == role_key:
            return role
    raise LookupError("Должность не найдена")


_bootstrap()
