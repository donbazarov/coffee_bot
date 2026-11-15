"""Уровни доступа, права и должности команды — единственный источник истины.

Права на сайте задаёт УРОВЕНЬ ДОСТУПА (0–4), а не должность и не опыт (XP):

* `ACCESS_LEVELS` — фиксированный справочник уровней: хардкод уровней живёт
  здесь и только здесь;
* `PERMISSIONS` — таблица «право → минимальный уровень»: одна строка на новую
  глобальную фичу, которой нужно разделение прав;
* должности (`role_access_config`) заводятся руками в панели управления и лишь
  ссылаются на уровень: ключ должности сам по себе ничего не решает, а опыт
  (XP) на права не влияет вообще.

Как добавить новую глобальную фичу с разделением по уровням:

1. `PERMISSIONS` — строка `"my_feature": 3` (+ подпись в `PERMISSION_TITLES`);
2. сервер — `Depends(require_permission("my_feature"))` (`bot/web/deps.py`) или
   `access_roles.allows(user, "my_feature")` для точечной проверки;
3. клиент — `can("my_feature")` в `static/app.js` и
   `{% if "my_feature" in allowed %}` в шаблоне.

Больше править нечего: порог права сразу виден в панели управления, а состав
должностей команда ведёт сама.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

# --- Хардкод уровней доступа ------------------------------------------------- #

# Уровни — новая сущность, не связанная с уровнями геймификации: ровно 0..4.
# Панель управления показывает этот справочник как есть, поэтому подписи и
# пояснения живут в одном месте с числами.
ACCESS_LEVELS: tuple[dict[str, Any], ...] = (
    {"level": 0, "title": "Базовый доступ",
     "summary": "График смен, база знаний (чтение), чат, свой прогресс и свои оценки."},
    {"level": 1, "title": "Замены",
     "summary": "Самостоятельная работа в смене: сотрудник сам закрывает замены."},
    {"level": 2, "title": "Смена и порядок",
     "summary": "Ведёт смену: ставит задачи и анонсы, следит за порядком и чистотой."},
    {"level": 3, "title": "Полный контроль",
     "summary": "Отвечает за стандарты: аналитика, база знаний, расписания, напитки, чат."},
    {"level": 4, "title": "Супер-пользователь",
     "summary": "Управляет сайтом: модули, должности, сотрудники, бренд, точки, нормо-часы."},
)

MAX_LEVEL = len(ACCESS_LEVELS) - 1

# Права сайта: ключ → минимальный уровень доступа. Единственное место, где
# перечислены глобальные права; порядок задаёт порядок в панели управления.
PERMISSIONS: dict[str, int] = {
    "swaps": 1,
    "tasks": 2,
    "announcements": 2,
    "maintenance": 2,
    "quality_cleanliness": 2,
    "analytics": 3,
    "kb_edit": 3,
    "schedule_publish": 3,
    "quality_full": 3,
    "chat_moderation": 3,
    "achievements": 3,
    "stories": 3,
    "admin": 4,
}

PERMISSION_TITLES: dict[str, str] = {
    "swaps": "Проведение замен",
    "tasks": "Задачи: создание, адресация, категории",
    "announcements": "Анонсы и их категории",
    "maintenance": "Тех. обслуживание: раздел и справочники",
    "quality_cleanliness": "Контроль качества: порядок и чистота",
    "analytics": "Аналитика и отчёты",
    "kb_edit": "Записи в базе знаний",
    "schedule_publish": "Правка и публикация расписаний",
    "quality_full": "Контроль качества: напитки, правка оценок",
    "chat_moderation": "Модерация чата и тем",
    "achievements": "Награды: управление",
    "stories": "Истории гостей: модерация",
    "admin": "Панель управления, сотрудники и должности",
}

# Право, отделяющее «оценивающих» от «оцениваемых»: сотрудников ниже этого
# уровня контролируют, тем, кто на нём и выше, оценки не ставят.
EVALUATED_BELOW = "quality_full"

# Ключ псевдо-роли для заявок на доступ: это не должность, а статус заявки,
# поэтому в справочник он не попадает.
PENDING_ROLE = "guest"

MAX_TITLE = 60
MAX_DESCRIPTION = 400
MAX_KEY = 40

# Значения прежней шкалы (XP-подобные уровни 0/5/10/15 до 2026-11): разовая
# миграция переводит их в 0..4. Условие самозавершающееся — после прогона в
# таблице не остаётся уровней вне 0..4 и непустых «возможностей».
LEGACY_LEVELS: dict[int, int] = {0: 0, 5: 1, 10: 2, 15: 4}

_DDL = """
CREATE TABLE IF NOT EXISTS role_access_config (
    role_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    access_level INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    -- legacy: колонка прежней модели «возможностей». Не читается и не пишется,
    -- оставлена, чтобы старые базы не требовали ALTER TABLE.
    capabilities TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Кэш уровня модуля: ключ должности → запись. Наполняет `_load` (старт и любая
# правка справочника) — как кэш настроек в `modules.py`.
_roles: dict[str, dict[str, Any]] = {}

# Латиница для ключа должности: ключ уходит в `users.role`, поэтому из названия
# «Старший бариста» получается `starshii_barista`.
_TRANSLIT: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# --- Служебное: ключи, значения, кэш ---------------------------------------- #


def _slug(value: Any) -> str:
    """Ключ должности из названия: латиница, цифры и подчёркивания."""
    source = str(value or "").strip().lower()
    transliterated = "".join(_TRANSLIT.get(char, char) for char in source)
    return re.sub(r"[^a-z0-9]+", "_", transliterated).strip("_")[:MAX_KEY].strip("_")


def _unique_key(base: str, taken: set[str]) -> str:
    """Свободный ключ: `barista`, `barista_2`, `barista_3` — если базовый занят."""
    candidate = base or "role"
    index = 2
    while candidate in taken or candidate == PENDING_ROLE:
        candidate = f"{base}_{index}"[:MAX_KEY].strip("_")
        index += 1
    return candidate


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
        raise ValueError(f"Уровень доступа должен быть числом от 0 до {MAX_LEVEL}")
    try:
        level = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Уровень доступа должен быть числом от 0 до {MAX_LEVEL}") from None
    if level < 0 or level > MAX_LEVEL:
        raise ValueError(f"Уровень доступа должен быть от 0 до {MAX_LEVEL}")
    return level


def _clean_sort_order(value: Any) -> int:
    try:
        return max(0, min(100000, int(value)))
    except (TypeError, ValueError):
        raise ValueError("Порядок должности должен быть числом") from None


def _legacy_level(value: int) -> int:
    """Прежний XP-подобный уровень → 0..4: ближайшая нижняя ступень старой шкалы.

    Значения 0..4 — это уже новая шкала, их не трогаем (иначе миграция не была бы
    идемпотентной и обнуляла бы уровни при каждом рестарте). Неизвестное значение
    из старой шкалы никогда не повышает права: 7 → 1 (бариста), 1000 → 4.
    """
    if value <= MAX_LEVEL:
        return value
    known = [key for key in LEGACY_LEVELS if key <= value]
    return LEGACY_LEVELS[max(known)] if known else MAX_LEVEL


def _record(row: Any) -> dict[str, Any]:
    return {
        "role_key": str(row["role_key"]),
        "title": str(row["title"]),
        "access_level": int(row["access_level"] or 0),
        "description": row["description"] or "",
        "is_active": int(row["is_active"] or 0),
        "sort_order": int(row["sort_order"] or 0),
    }


def _load(engine: Engine) -> None:
    """Перечитывает справочник из базы (включая отключённые должности)."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT role_key, title, access_level, description, is_active, sort_order
            FROM role_access_config
        """)).mappings().all()
    _roles.clear()
    for row in rows:
        record = _record(row)
        _roles[record["role_key"]] = record


def initialize_schema(engine: Engine) -> None:
    """Создаёт справочник, переводит прежние уровни в 0..4 и наполняет кэш.

    Дефолтных должностей нет намеренно: состав команды заводят руками в панели
    управления, в коде хардкодится только шкала уровней.
    """
    with engine.begin() as connection:
        connection.execute(text(_DDL))
        rows = connection.execute(text("""
            SELECT role_key, access_level, capabilities FROM role_access_config
        """)).mappings().all()
        for row in rows:
            updates: dict[str, Any] = {}
            legacy = int(row["access_level"] or 0)
            level = _legacy_level(legacy)
            if level != legacy:
                updates["access_level"] = level
            if str(row["capabilities"] or ""):
                updates["capabilities"] = ""
            if not updates:
                continue
            assignments = ", ".join(f"{name} = :{name}" for name in updates)
            connection.execute(
                text(f"UPDATE role_access_config SET {assignments} WHERE role_key = :role_key"),
                {**updates, "role_key": row["role_key"]},
            )
    _load(engine)


def refresh(engine: Engine) -> None:
    """Пересобирает кэш (вызывается после правки справочника)."""
    _load(engine)


# --- Чтение прав и должностей ----------------------------------------------- #


def _ordered() -> list[dict[str, Any]]:
    return sorted(_roles.values(),
                  key=lambda role: (int(role["sort_order"]), int(role["access_level"]), role["role_key"]))


def role_keys(active_only: bool = True) -> tuple[str, ...]:
    """Ключи должностей в порядке справочника; `active_only` — включённые."""
    return tuple(role["role_key"] for role in _ordered() if not active_only or role["is_active"])


def staff_roles() -> tuple[str, ...]:
    """Активные должности — замена прежним `STAFF_ROLES`/`WORK_ROLES`/`ALL_ROLES`."""
    return role_keys()


def labels() -> dict[str, str]:
    """Ключ должности → читаемое название (замена `ROLE_NAMES`)."""
    return {role_key: role["title"] for role_key, role in _roles.items()}


def level_of(user_or_role: Any) -> int:
    """Уровень доступа: по пользователю (сессия) или по ключу должности.

    Неизвестная должность (в том числе заявка `guest`) — уровень 0.
    """
    if isinstance(user_or_role, dict):
        raw = user_or_role.get("access_level")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return 0
        return level_of(user_or_role.get("role"))
    record = _roles.get(str(user_or_role or ""))
    return int(record["access_level"]) if record else 0


def title_of(role: Any) -> str:
    """Читаемое название должности; неизвестный ключ остаётся как есть."""
    record = _roles.get(str(role or ""))
    return record["title"] if record else str(role or "")


def levels() -> list[dict[str, Any]]:
    """Справочник уровней (0–4) для панели управления и селектов."""
    return [dict(item) for item in ACCESS_LEVELS]


def level_title(level: Any) -> str:
    """Название уровня по числу; неизвестное число — пустая строка."""
    try:
        value = int(level)
    except (TypeError, ValueError):
        return ""
    for item in ACCESS_LEVELS:
        if int(item["level"]) == value:
            return str(item["title"])
    return ""


def min_level(permission: str) -> int:
    """Минимальный уровень доступа для права; неизвестное право — ошибка кода."""
    if permission not in PERMISSIONS:
        raise KeyError(f"Неизвестное право доступа: {permission}")
    return int(PERMISSIONS[permission])


def allows(user_or_role: Any, permission: str) -> bool:
    """Есть ли у пользователя (или у должности) право — единственный гейт прав."""
    return level_of(user_or_role) >= min_level(permission)


def allowed_keys(user_or_role: Any) -> tuple[str, ...]:
    """Права, доступные уровню, — для Jinja-гейтов (`{% if 'admin' in allowed %}`)."""
    level = level_of(user_or_role)
    return tuple(key for key, threshold in PERMISSIONS.items() if level >= threshold)


def permission_levels() -> dict[str, int]:
    """Права и их пороги — сервер отдаёт это клиенту, чтобы работал `can()`."""
    return dict(PERMISSIONS)


def is_evaluated(user_or_role: Any) -> bool:
    """Кого оценивают в контроле качества: всё, что ниже полного контроля."""
    return level_of(user_or_role) < min_level(EVALUATED_BELOW)


# --- Панель управления: справочник должностей ------------------------------- #


def list_roles(engine: Engine) -> list[dict[str, Any]]:
    """Все должности с числом сотрудников — для панели управления."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT r.role_key, r.title, r.access_level, r.description,
                   r.is_active, r.sort_order,
                   (SELECT COUNT(*) FROM users u WHERE u.role = r.role_key) AS user_count,
                   (SELECT COUNT(*) FROM users u
                     WHERE u.role = r.role_key AND u.is_active = 1) AS active_user_count
            FROM role_access_config r
            ORDER BY r.sort_order, r.access_level, r.role_key
        """)).mappings().all()
    result: list[dict[str, Any]] = []
    for row in rows:
        role = _record(row)
        role["user_count"] = int(row["user_count"] or 0)
        role["active_user_count"] = int(row["active_user_count"] or 0)
        role["level_title"] = level_title(role["access_level"])
        result.append(role)
    return result


def _role_view(engine: Engine, role_key: str) -> dict[str, Any]:
    for role in list_roles(engine):
        if role["role_key"] == role_key:
            return role
    raise LookupError("Должность не найдена")


def view(engine: Engine) -> dict[str, Any]:
    """Справочник должностей + шкала уровней + таблица прав для панели."""
    return {
        "roles": list_roles(engine),
        "levels": levels(),
        "permissions": [
            {"key": key, "title": PERMISSION_TITLES.get(key, key), "level": level}
            for key, level in PERMISSIONS.items()
        ],
        "pending_role": PENDING_ROLE,
    }


def admin_user_count(connection: Any, exclude_user_id: int | None = None) -> int:
    """Сколько активных сотрудников сейчас имеют уровень «панель управления»."""
    condition = "" if exclude_user_id is None else "AND u.id != :exclude_id"
    params: dict[str, Any] = {"admin_level": min_level("admin")}
    if exclude_user_id is not None:
        params["exclude_id"] = exclude_user_id
    return int(connection.execute(text(f"""
        SELECT COUNT(*) FROM users u
        JOIN role_access_config r ON r.role_key = u.role
        WHERE u.is_active = 1 AND r.is_active = 1
          AND r.access_level >= :admin_level {condition}
    """), params).scalar_one() or 0)


def create_role(engine: Engine, payload: dict[str, Any]) -> dict[str, Any]:
    """Заводит должность руками из панели управления.

    Ключ можно не указывать — сгенерируем из названия латиницей. Ключ неизменяем:
    на него ссылаются `users.role`, история оценок, задач и графика.
    """
    if not isinstance(payload, dict):
        raise ValueError("Не переданы данные должности")
    allowed = {"role_key", "title", "description", "access_level", "is_active", "sort_order"}
    if set(payload) - allowed:
        raise ValueError("Переданы неизвестные поля")

    title = _clean_title(payload.get("title"))
    level = _clean_level(payload.get("access_level", 0))
    description = _clean_description(payload.get("description"))
    is_active = payload.get("is_active", True)
    if not isinstance(is_active, bool):
        raise ValueError("Признак активности должности должен быть булевым")
    explicit = str(payload.get("role_key") or "").strip()
    sort_order = payload.get("sort_order")

    with engine.begin() as connection:
        taken = set(connection.execute(text("SELECT role_key FROM role_access_config")).scalars().all())
        if explicit:
            key = _slug(explicit)
            if not key:
                raise ValueError("Ключ должности: латиница, цифры и подчёркивания")
            if key in taken:
                raise ValueError(f"Ключ «{key}» уже занят другой должностью")
        else:
            key = _unique_key(_slug(title), taken)
        if sort_order is None:
            sort_order = int(connection.execute(text(
                "SELECT COALESCE(MAX(sort_order), 0) + 10 FROM role_access_config"
            )).scalar_one() or 10)
        else:
            sort_order = _clean_sort_order(sort_order)
        connection.execute(text("""
            INSERT INTO role_access_config
                (role_key, title, access_level, description, capabilities, is_active, sort_order)
            VALUES (:role_key, :title, :access_level, :description, '', :is_active, :sort_order)
        """), {"role_key": key, "title": title, "access_level": level,
               "description": description, "is_active": int(is_active), "sort_order": sort_order})
    refresh(engine)
    return _role_view(engine, key)


def update_role(engine: Engine, role_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Обновляет должность и проверяет, что супер-пользователи в системе остались.

    Ключ должности неизменен: на него ссылаются сотрудники, оценки и график.
    """
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Не передано ни одного поля для сохранения")
    allowed = {"title", "description", "access_level", "is_active", "sort_order"}
    if set(payload) - allowed:
        raise ValueError("Переданы неизвестные поля")

    values: dict[str, Any] = {}
    if "title" in payload:
        values["title"] = _clean_title(payload["title"])
    if "description" in payload:
        values["description"] = _clean_description(payload["description"])
    if "access_level" in payload:
        values["access_level"] = _clean_level(payload["access_level"])
    if "is_active" in payload:
        if not isinstance(payload["is_active"], bool):
            raise ValueError("Признак активности должности должен быть булевым")
        values["is_active"] = int(payload["is_active"])
    if "sort_order" in payload:
        values["sort_order"] = _clean_sort_order(payload["sort_order"])
    if not values:
        raise ValueError("Не передано ни одного поля для сохранения")

    with engine.begin() as connection:
        exists = connection.execute(
            text("SELECT role_key FROM role_access_config WHERE role_key = :role_key"),
            {"role_key": role_key},
        ).first()
        if not exists:
            raise LookupError("Должность не найдена")
        before = admin_user_count(connection)
        assignments = ", ".join(f"{name} = :{name}" for name in values)
        connection.execute(text(f"""
            UPDATE role_access_config
            SET {assignments}, updated_at = CURRENT_TIMESTAMP
            WHERE role_key = :role_key
        """), {**values, "role_key": role_key})
        # Проверка по состоянию «после»: если супер-пользователи в системе уже
        # были, правка не имеет права оставить её без доступа к панели.
        if before and not admin_user_count(connection):
            raise ValueError("В системе должен остаться хотя бы один супер-пользователь "
                             "(уровень 4): правка снимает доступ у всех")
    refresh(engine)
    return _role_view(engine, role_key)


def delete_role(engine: Engine, role_key: str) -> dict[str, Any]:
    """Удаляет должность. Назначенную сотрудникам удалять нельзя — только отключить."""
    with engine.begin() as connection:
        exists = connection.execute(
            text("SELECT role_key FROM role_access_config WHERE role_key = :role_key"),
            {"role_key": role_key},
        ).first()
        if not exists:
            raise LookupError("Должность не найдена")
        users = int(connection.execute(
            text("SELECT COUNT(*) FROM users WHERE role = :role_key"), {"role_key": role_key}
        ).scalar_one() or 0)
        if users:
            raise ValueError(f"Должность назначена сотрудникам ({users}) — сначала переназначьте "
                             "их или отключите должность, чтобы её нельзя было назначать")
        connection.execute(text("DELETE FROM role_access_config WHERE role_key = :role_key"),
                           {"role_key": role_key})
    refresh(engine)
    return {"role_key": role_key}
