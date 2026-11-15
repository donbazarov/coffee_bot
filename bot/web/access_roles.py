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

import logging
import re
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Лог домена: сводка по должностям пишется на старте — поломка справочника делает
# вход невозможным, и это должно быть видно в логах сервиса, а не только по жалобе
# «не пускает».
logger = logging.getLogger("bot.web")

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

# Ключи прежней шкалы (до 2026-11). Используются ТОЛЬКО для аварийного
# восстановления справочника по фактическим ролям команды (`_bootstrap_roles`):
# это не дефолтный набор должностей — новые заводятся руками в панели управления.
LEGACY_ROLE_LEVELS: dict[str, int] = {"newbie": 0, "barista": 5, "senior": 10, "mentor": 15}

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

# Должности сотрудника: их может быть несколько (`user_roles`). Порядок задаёт
# справочник (`role_access_config.sort_order`), «основная» — первая в списке: по
# ней работают график смен, нормо-часы, чат и push (эти подсистемы хранят одну
# роль), а права берутся как максимум по всем активным должностям.
# Операторы идут по одному: sqlite3 исполняет за раз ровно одно выражение.
_USER_ROLES_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS user_roles (
        user_id INTEGER NOT NULL REFERENCES users(id),
        role_key TEXT NOT NULL,
        PRIMARY KEY (user_id, role_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_roles_role ON user_roles (role_key)",
)

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


def _bootstrap_roles(connection: Any) -> list[str]:
    """Восстанавливает справочник, если активной должности нет ни у кого.

    Ситуация «люди работают, а справочник пуст» делает вход невозможным: права и
    сессии читаются только из активных должностей, а завести их можно лишь из
    панели, куда никого не пускает. Поэтому справочник собираем по фактическим
    `users.role` (уровни — по прежней шкале), включаем отключённые строки и
    гарантируем, что доступ к панели управления у кого-то остался. Операция
    самозавершающаяся: при рабочем справочнике не делает ничего.
    """
    working = int(connection.execute(text("""
        SELECT COUNT(*) FROM users u
        JOIN role_access_config r ON r.role_key = u.role AND r.is_active = 1
        WHERE u.is_active = 1 AND u.role != :pending
    """), {"pending": PENDING_ROLE}).scalar_one() or 0)
    if working:
        return []

    known = set(connection.execute(text("SELECT role_key FROM role_access_config")).scalars().all())
    roles = [str(row[0] or "") for row in connection.execute(text("""
        SELECT role FROM users WHERE role != :pending GROUP BY role
    """), {"pending": PENDING_ROLE}).all()]
    touched: list[str] = []
    order = 0
    for role_key in roles:
        if not role_key:
            continue
        order += 10
        if role_key in known:
            connection.execute(text(
                "UPDATE role_access_config SET is_active = 1 WHERE role_key = :role_key"
            ), {"role_key": role_key})
        else:
            connection.execute(text("""
                INSERT INTO role_access_config
                    (role_key, title, access_level, description, capabilities, is_active, sort_order)
                VALUES (:role_key, :role_key, :access_level, '', '', 1, :sort_order)
            """), {"role_key": role_key,
                   "access_level": _legacy_level(LEGACY_ROLE_LEVELS.get(role_key, 0)),
                   "sort_order": order})
        touched.append(role_key)

    # Панель управления обязана быть доступна: иначе должности не завести руками.
    admin_level = min_level("admin")
    admins = int(connection.execute(text("""
        SELECT COUNT(*) FROM users u
        JOIN role_access_config r ON r.role_key = u.role AND r.is_active = 1
        WHERE u.is_active = 1 AND r.access_level >= :admin_level
    """), {"admin_level": admin_level}).scalar_one() or 0)
    if not admins:
        owner = connection.execute(text("""
            SELECT role FROM users WHERE is_active = 1 AND role != :pending ORDER BY id LIMIT 1
        """), {"pending": PENDING_ROLE}).first()
        if owner and owner[0]:
            connection.execute(text("""
                UPDATE role_access_config SET access_level = :admin_level WHERE role_key = :role_key
            """), {"admin_level": admin_level, "role_key": owner[0]})
            touched.append(f"{owner[0]} → уровень {admin_level}")
    return touched


def _bootstrap_user_roles(connection: Any) -> int:
    """Переносит единственную прежнюю должность (`users.role`) в `user_roles`.

    Идемпотентно: у кого назначение уже есть — не трогаем; заявки (`guest`) и
    пустые роли пропускаем: должности у них нет.
    """
    result = connection.execute(text("""
        INSERT OR IGNORE INTO user_roles (user_id, role_key)
        SELECT id, role FROM users
        WHERE role != :pending AND TRIM(COALESCE(role, '')) != ''
    """), {"pending": PENDING_ROLE})
    return int(result.rowcount or 0)


def initialize_schema(engine: Engine) -> None:
    """Создаёт справочник, переводит прежние уровни в 0..4 и наполняет кэш.

    Дефолтных должностей нет намеренно: состав команды заводят руками в панели
    управления, в коде хардкодится только шкала уровней. Единственное исключение —
    аварийное восстановление (`_bootstrap_roles`): если активной должности нет ни
    у одного сотрудника, справочник собирается по фактическим ролям команды.
    """
    with engine.begin() as connection:
        connection.execute(text(_DDL))
        for statement in _USER_ROLES_DDL:
            connection.execute(text(statement))
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
        restored = _bootstrap_roles(connection)
        moved = _bootstrap_user_roles(connection)
    _load(engine)
    if moved:
        logger.info("Должности: перенесено назначений из users.role — %s", moved)
    if restored:
        logger.critical(
            "Должности: справочник был нерабочим (ни у кого не было активной должности) "
            "и восстановлен по фактическим ролям команды: %s. Проверьте уровни в панели "
            "управления → «Должности и допуски».", ", ".join(restored),
        )
    logger.info(
        "Должности: активных %s (%s); доступ к панели управления — %s",
        len(role_keys()), ", ".join(role_keys()) or "нет",
        ", ".join(key for key in role_keys() if level_of(key) >= min_level("admin")) or "нет",
    )


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

    Неизвестная или отключённая должность — уровень 0 (права fail-closed):
    отключение снимает права, но не выбрасывает человека из системы.
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
    if not record or not int(record["is_active"]):
        return 0
    return int(record["access_level"])


def title_of(role: Any) -> str:
    """Читаемое название должности; неизвестный ключ остаётся как есть."""
    record = _roles.get(str(role or ""))
    return record["title"] if record else str(role or "")


def levels() -> list[dict[str, Any]]:
    """Справочник уровней (0–4) для панели управления и селектов."""
    return [dict(item) for item in ACCESS_LEVELS]


# --- Должности сотрудника ---------------------------------------------------- #
#
# Их может быть несколько (`user_roles`): права берутся как максимум по активным
# должностям, а «основная» (первая по порядку справочника) остаётся в `users.role`
# для подсистем, которые хранят одну роль — график смен, нормо-часы, чат, push.


def roles_of(connection: Any, user_id: int) -> list[str]:
    """Должности сотрудника: от высокой к низкой (первая — основная)."""
    return [str(row[0]) for row in connection.execute(text("""
        SELECT ur.role_key
        FROM user_roles ur
        LEFT JOIN role_access_config r ON r.role_key = ur.role_key
        WHERE ur.user_id = :user_id
        ORDER BY COALESCE(r.access_level, -1) DESC, COALESCE(r.sort_order, 2147483647), ur.role_key
    """), {"user_id": user_id}).all()]


def user_level(roles: Iterable[str]) -> int:
    """Уровень доступа сотрудника: максимум по должностям (отключённые — 0)."""
    return max((level_of(key) for key in roles), default=0)


def user_roles_map(connection: Any) -> dict[int, list[dict[str, Any]]]:
    """Все назначения разом: `user_id` → должности от высокой к низкой.

    Нужен панели (`/api/users`), чтобы не делать отдельный запрос на сотрудника.
    """
    rows = connection.execute(text("""
        SELECT ur.user_id, ur.role_key, r.title, r.access_level, r.is_active
        FROM user_roles ur
        LEFT JOIN role_access_config r ON r.role_key = ur.role_key
        ORDER BY ur.user_id, COALESCE(r.access_level, -1) DESC,
                 COALESCE(r.sort_order, 2147483647), ur.role_key
    """)).mappings().all()
    result: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        key = str(row["role_key"])
        result.setdefault(int(row["user_id"]), []).append({
            "key": key,
            "title": row["title"] or title_of(key),
            "level": level_of(key),
            "is_active": bool(row["is_active"]),
        })
    return result


def set_user_roles(connection: Any, user_id: int, role_keys: Iterable[str]) -> list[str]:
    """Записывает должности сотрудника и синхронизирует основную (`users.role`).

    Основной становится самая высокая по уровню должность (при равных уровнях —
    выше в списке справочника): по ней работают график смен, нормо-часы, чат и
    push. Без должности сотрудник остаться не может: «нет должности» — это
    состояние заявки на доступ, а не рабочая конфигурация.
    """
    keys = [str(key or "").strip() for key in role_keys]
    keys = [key for key in keys if key]
    if not keys:
        raise ValueError("У сотрудника должна быть хотя бы одна должность")
    if len(set(keys)) != len(keys):
        raise ValueError("Должности не должны повторяться")
    unknown = [key for key in keys if key not in _roles]
    if unknown:
        raise ValueError(f"Неизвестная должность: {', '.join(unknown)}")
    ordered = sorted(keys, key=lambda key: (-int(_roles[key]["access_level"]),
                                            int(_roles[key]["sort_order"]), key))
    connection.execute(text("DELETE FROM user_roles WHERE user_id = :user_id"), {"user_id": user_id})
    for key in ordered:
        connection.execute(
            text("INSERT INTO user_roles (user_id, role_key) VALUES (:user_id, :role_key)"),
            {"user_id": user_id, "role_key": key},
        )
    connection.execute(text("""
        UPDATE users SET role = :role, updated_at = CURRENT_TIMESTAMP WHERE id = :user_id
    """), {"role": ordered[0], "user_id": user_id})
    return ordered


def has_active_role_sql(alias: str = "users") -> str:
    """SQL «у сотрудника есть активная должность» — для запросов вне модуля.

    Проверяет `user_roles`, а не `users.role`: должностей может быть несколько, и
    отключение основной не должно выкидывать человека из чата, push и ТО.
    """
    return (f"EXISTS (SELECT 1 FROM user_roles ur JOIN role_access_config r "
            f"ON r.role_key = ur.role_key WHERE ur.user_id = {alias}.id AND r.is_active = 1)")


def has_any_role_sql(alias: str, placeholders: str) -> str:
    """SQL «у сотрудника есть любая из перечисленных должностей» (параметры снаружи)."""
    return (f"EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_id = {alias}.id "
            f"AND ur.role_key IN ({placeholders}))")


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
                   (SELECT COUNT(*) FROM user_roles ur WHERE ur.role_key = r.role_key) AS user_count,
                   (SELECT COUNT(*) FROM user_roles ur JOIN users u ON u.id = ur.user_id
                    WHERE ur.role_key = r.role_key AND u.is_active = 1) AS active_user_count
            FROM role_access_config r
            ORDER BY r.access_level DESC, r.sort_order, r.role_key
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
    """Сколько активных сотрудников сейчас имеют уровень «панель управления».

    Учитываются все должности сотрудника (`user_roles`), а не только основная:
    доступ к панели может прийти с любой из них.
    """
    condition = "" if exclude_user_id is None else "AND u.id != :exclude_id"
    params: dict[str, Any] = {"admin_level": min_level("admin")}
    if exclude_user_id is not None:
        params["exclude_id"] = exclude_user_id
    return int(connection.execute(text(f"""
        SELECT COUNT(*) FROM users u
        WHERE u.is_active = 1 AND EXISTS (
            SELECT 1 FROM user_roles ur
            JOIN role_access_config r ON r.role_key = ur.role_key
            WHERE ur.user_id = u.id AND r.is_active = 1 AND r.access_level >= :admin_level
        ) {condition}
    """), params).scalar_one() or 0)


def create_role(engine: Engine, payload: dict[str, Any]) -> dict[str, Any]:
    """Заводит должность руками из панели управления.

    Ключ можно не указывать — сгенерируем из названия латиницей. Ключ неизменяем:
    на него ссылаются `users.role`, история оценок, задач и графика.
    """
    if not isinstance(payload, dict):
        raise ValueError("Не переданы данные должности")
    allowed = {"role_key", "title", "description", "access_level", "is_active", "sort_order"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"Переданы неизвестные поля: {', '.join(sorted(unknown))}")

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
    allowed = {"title", "description", "access_level", "is_active", "sort_order", "role_key"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"Переданы неизвестные поля: {', '.join(sorted(unknown))}")
    # `role_key` клиент присылает вместе с остальными полями (одна форма на создание
    # и правку): ключ неизменен, но если он совпадает с адресом — это не ошибка.
    sent_key = str(payload.get("role_key") or "").strip()
    if sent_key and sent_key != role_key:
        raise ValueError("Ключ должности изменить нельзя — создайте новую должность")

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
            raise ValueError(
                "В системе должен остаться хотя бы один супер-пользователь (уровень 4). "
                "Сначала создайте должность с уровнем 4 и назначьте её себе, "
                "затем понижайте эту."
            )
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
            text("SELECT COUNT(*) FROM user_roles WHERE role_key = :role_key"), {"role_key": role_key}
        ).scalar_one() or 0)
        if users:
            raise ValueError(f"Должность назначена сотрудникам ({users}) — сначала переназначьте "
                             "их или отключите должность, чтобы её нельзя было назначать")
        connection.execute(text("DELETE FROM role_access_config WHERE role_key = :role_key"),
                           {"role_key": role_key})
    refresh(engine)
    return {"role_key": role_key}
