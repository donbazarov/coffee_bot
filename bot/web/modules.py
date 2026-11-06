"""Реестр модулей сайта: единый источник истины для навигации и вкл/выкл.

Модуль изолирован по образцу `knowledge_base.py`: здесь только описание
модулей и работа с их глобальным состоянием (`web_app_settings`). Таблица
настроек создаётся `initialize_calendar_schema` раньше, поэтому схему здесь
не создаём — только идемпотентно досеиваем отсутствующие флаги.

Ключ состояния: `module.<code>.enabled` = `"1"` / `"0"`. Отключение
**глобальное** (для всей команды) и доступно только старшим/наставникам —
панель управления админская. Персональных тогглов модулей больше нет.

Поля модуля:
* `code`      — короткий код (id в разметке: `data-control-module="<code>"`);
* `title`     — название в навигации;
* `icon`      — id символа `<use href="#i-...">`;
* `view`      — значение `data-view` и префикс `#<view>-view` (пусто — своей вьюхи нет);
* `admin`     — модуль виден только `senior`/`mentor`;
* `settable`  — можно ли выключать модуль целиком (Обзор и Панель управления — нет).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Порядок задаёт и порядок в навигации докера/меню, и порядок в левом списке
# панели управления. Разделы намеренно не смешиваются: «Работа» → «Знания» →
# «Управление».
MODULES: tuple[dict[str, Any], ...] = (
    {"code": "overview", "title": "Обзор", "icon": "i-overview", "view": "overview",
     "admin": False, "settable": False},
    {"code": "calendar", "title": "График смен", "icon": "i-calendar", "view": "calendar",
     "admin": False, "settable": True},
    {"code": "announcements", "title": "Анонсы", "icon": "i-bell", "view": "announcements",
     "admin": False, "settable": True},
    {"code": "chat", "title": "Чат", "icon": "i-chat", "view": "chat",
     "admin": False, "settable": True},
    {"code": "development", "title": "Развитие", "icon": "i-spark", "view": "development",
     "admin": False, "settable": True},
    {"code": "kb", "title": "База знаний", "icon": "i-book", "view": "kb",
     "admin": False, "settable": True},
    {"code": "drinks", "title": "Описание напитков", "icon": "i-latte", "view": "drinks",
     "admin": False, "settable": True},
    {"code": "stories", "title": "Истории гостей", "icon": "i-camera", "view": "",
     "admin": True, "settable": True},
    {"code": "team", "title": "Команда", "icon": "i-team", "view": "team",
     "admin": True, "settable": True},
    {"code": "analytics", "title": "Аналитика", "icon": "i-chart", "view": "analytics",
     "admin": True, "settable": True},
    {"code": "control", "title": "Панель управления", "icon": "i-settings", "view": "control",
     "admin": True, "settable": False},
)

_BY_CODE: dict[str, dict[str, Any]] = {module["code"]: module for module in MODULES}
_SETTING_PREFIX = "module."


def module_codes() -> tuple[str, ...]:
    """Коды модулей в порядке реестра."""
    return tuple(module["code"] for module in MODULES)


def get_module(code: str) -> dict[str, Any] | None:
    """Описание модуля по коду или None."""
    return _BY_CODE.get(code)


def _state_key(code: str) -> str:
    return f"{_SETTING_PREFIX}{code}.enabled"


def initialize_schema(engine: Engine) -> None:
    """Досеивает отсутствующие флаги модулей (включены по умолчанию).

    `INSERT OR IGNORE`: повторный старт ничего не перезаписывает — выключенный
    наставником модуль не включится сам после рестарта.
    """
    with engine.begin() as connection:
        for code in module_codes():
            connection.execute(text("""
                INSERT OR IGNORE INTO web_app_settings (key, value) VALUES (:key, '1')
            """), {"key": _state_key(code)})


def _read_states(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT key, value FROM web_app_settings WHERE key LIKE :prefix
        """), {"prefix": f"{_SETTING_PREFIX}%"}).mappings().all()
    return {row["key"]: row["value"] for row in rows}


def load_modules(engine: Engine, *, is_manager: bool) -> list[dict[str, Any]]:
    """Состояние модулей для рендера навигации и панели управления.

    Админские модули не отдаём обычным сотрудникам — как и Jinja-гейты по роли.
    """
    stored = _read_states(engine)
    result: list[dict[str, Any]] = []
    for module in MODULES:
        if module["admin"] and not is_manager:
            continue
        result.append({**module, "enabled": stored.get(_state_key(module["code"]), "1") == "1"})
    return result


def save_module_enabled(engine: Engine, code: str, enabled: bool) -> dict[str, Any]:
    """Глобально включает/выключает модуль. Менять можно только `settable`."""
    module = get_module(code)
    if module is None or not module["settable"]:
        raise ValueError("Этот модуль нельзя включить или выключить")
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO web_app_settings (key, value, updated_at)
            VALUES (:key, :value, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        """), {"key": _state_key(code), "value": "1" if enabled else "0"})
    return {**module, "enabled": bool(enabled)}

# ---------------------------------------------------------------------------
#  Редактируемые настройки модулей (те «константы», что раньше жили в коде).
#
#  * СХЕМА (`SETTINGS_SCHEMA`) — метаданные для формы и границы валидации.
#  * ДЕФОЛТЫ регистрируют сами модули (`register_defaults`) — так значение по
#    умолчанию не дублируется и не расходится с кодом.
#  * ЗНАЧЕНИЯ (дефолт или сохранённый оверрайд) читаются из кэша: `setting()`.
#    Кэш наполняет `initialize_settings` на старте и обновляет сохранение.
#
#  Ключ хранения: `setting.<code>.<KEY>`.
# ---------------------------------------------------------------------------

_SETTING_VALUE_PREFIX = "setting."

# type: int | float | str | bool | list. Для int/float — границы min/max/step.
SETTINGS_SCHEMA: dict[str, tuple[dict[str, Any], ...]] = {
    "calendar": (
        {"key": "MAX_PUBLISH_DAYS", "label": "Максимум дней в одной публикации", "type": "int",
         "min": 1, "max": 366, "hint": "92 дня — это 12 снимков по 8 дней"},
    ),
    "announcements": (
        {"key": "MAX_TITLE", "label": "Макс. длина заголовка", "type": "int", "min": 10, "max": 500},
        {"key": "MAX_BODY", "label": "Макс. длина текста", "type": "int", "min": 100, "max": 20000},
        {"key": "MAX_LABEL", "label": "Макс. длина названия", "type": "int", "min": 5, "max": 200},
        {"key": "MAX_LIST", "label": "Макс. размер выборки ленты", "type": "int", "min": 1, "max": 500},
    ),
    "kb": (
        {"key": "MAX_TITLE", "label": "Макс. длина заголовка", "type": "int", "min": 10, "max": 1000},
        {"key": "MAX_SLUG", "label": "Макс. длина слага (URL)", "type": "int", "min": 10, "max": 400},
        {"key": "MAX_TEXT", "label": "Макс. длина текста страницы", "type": "int", "min": 1000, "max": 200000},
        {"key": "MAX_TAG", "label": "Макс. длина тега", "type": "int", "min": 1, "max": 120},
        {"key": "MAX_TAGS", "label": "Макс. число тегов", "type": "int", "min": 1, "max": 50},
        {"key": "SECTIONS", "label": "Разделы базы знаний (кроме «Зерно»/«Напитки»)", "type": "list",
         "hint": "через запятую как ключ=Название, напр. wiki=Вики, standards=Стандарты"},
        {"key": "BEAN_ROAST_TYPES", "label": "Типы обжарки зерна", "type": "list",
         "hint": "через запятую, напр. espresso, filter, omni"},
        {"key": "DRINK_CATEGORIES", "label": "Категории напитков", "type": "list",
         "hint": "через запятую; должны совпадать с menu_drinks.category"},
    ),
    "drinks": (
        {"key": "XP_TEAM", "label": "Опыт за публикацию → «Команда»", "type": "int", "min": 0, "max": 1000},
        {"key": "XP_SENSORY", "label": "Опыт за публикацию → «Сенсорика»", "type": "int", "min": 0, "max": 1000},
        {"key": "XP_MILK", "label": "Опыт за молочный напиток → «Молоко»", "type": "int", "min": 0, "max": 1000},
        {"key": "XP_QUIZ", "label": "Опыт за верный ответ викторины → «Команда»", "type": "int", "min": 0, "max": 1000},
        {"key": "MIN_QUIZ_OPTIONS", "label": "Минимум вариантов викторины", "type": "int", "min": 2, "max": 10},
        {"key": "MAX_QUIZ_OPTIONS", "label": "Максимум вариантов викторины", "type": "int", "min": 2, "max": 12},
        {"key": "MAX_PHOTOS_PER_KIND", "label": "Макс. фото на бариста (латте-арт/мем)", "type": "int", "min": 1, "max": 20},
        {"key": "DEFAULT_PAGE", "label": "Размер страницы ленты по умолчанию", "type": "int", "min": 5, "max": 100},
        {"key": "MAX_PAGE", "label": "Макс. размер страницы ленты", "type": "int", "min": 10, "max": 200},
    ),
    "team": (
        {"key": "DEFAULT_MIN_HOURS", "label": "Нормо-часы: минимум по умолчанию", "type": "int", "min": 0, "max": 744},
        {"key": "DEFAULT_OPT_HOURS", "label": "Нормо-часы: оптимум по умолчанию", "type": "int", "min": 0, "max": 744},
        {"key": "DEFAULT_MAX_HOURS", "label": "Нормо-часы: максимум по умолчанию", "type": "int", "min": 0, "max": 744},
        {"key": "MAX_HOURS_LIMIT", "label": "Предел часов в расчёте", "type": "int", "min": 1, "max": 744,
         "hint": "31 день × 24 часа — больше физически не проставить"},
        {"key": "SATURATION_DEFICIT", "label": "Порог дефицита (доля от оптимума)", "type": "float", "min": 0, "max": 5, "step": 0.05},
        {"key": "SATURATION_OVERLOAD", "label": "Порог переработки (доля от оптимума)", "type": "float", "min": 0, "max": 5, "step": 0.05},
        {"key": "ISDAYOFF_REGION", "label": "Регион производственного календаря", "type": "str", "hint": "код isdayoff.ru, напр. ru-ta"},
    ),
}

_settings_defaults: dict[str, dict[str, Any]] = {}
_settings_values: dict[str, dict[str, Any]] = {}


def register_defaults(code: str, values: dict[str, Any]) -> None:
    """Модуль регистрирует свои дефолты (обычно сразу после констант)."""
    _settings_defaults.setdefault(code, {}).update(values)


def _coerce_stored(field: dict[str, Any], raw: str) -> Any:
    kind = field["type"]
    try:
        if kind == "int":
            return int(float(raw))
        if kind == "float":
            return float(raw)
        if kind == "bool":
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}
        if kind == "list":
            return tuple(item.strip() for item in str(raw).split(",") if item.strip())
        return str(raw)
    except (TypeError, ValueError):
        return field.get("default")


def initialize_settings(engine: Engine) -> None:
    """Наполняет кэш значений на старте (оверрайды из web_app_settings + дефолты)."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT key, value FROM web_app_settings WHERE key LIKE :prefix
        """), {"prefix": f"{_SETTING_VALUE_PREFIX}%"}).mappings().all()
    stored = {row["key"]: row["value"] for row in rows}
    _settings_values.clear()
    for code, fields in SETTINGS_SCHEMA.items():
        values: dict[str, Any] = {}
        for field in fields:
            default = _settings_defaults.get(code, {}).get(field["key"], field.get("default"))
            storage_key = f"{_SETTING_VALUE_PREFIX}{code}.{field['key']}"
            values[field["key"]] = _coerce_stored(field, stored[storage_key]) if storage_key in stored else default
        _settings_values[code] = values


def setting(code: str, key: str, default: Any = None) -> Any:
    """Текущее значение настройки: оверрайд, дефолт модуля или переданный fallback."""
    values = _settings_values.get(code)
    if values is not None and key in values:
        return values[key]
    return _settings_defaults.get(code, {}).get(key, default)

def _normalize_input(field: dict[str, Any], raw: Any) -> Any:
    kind = field["type"]
    label = field["label"]
    if kind in {"int", "float"}:
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{label}: нужно число")
        if "min" in field and number < field["min"]:
            raise ValueError(f"{label}: не меньше {field['min']}")
        if "max" in field and number > field["max"]:
            raise ValueError(f"{label}: не больше {field['max']}")
        return int(number) if kind == "int" else round(number, 4)
    if kind == "bool":
        return bool(raw)
    if kind == "list":
        source = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
        items = [str(item).strip() for item in source]
        items = [item for item in items if item]
        if not items:
            raise ValueError(f"{label}: нужен хотя бы один элемент")
        return tuple(items)
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"{label}: значение не может быть пустым")
    return text


def _storage_text(field: dict[str, Any], value: Any) -> str:
    if field["type"] == "list":
        return ", ".join(str(item) for item in value)
    if field["type"] == "bool":
        return "1" if value else "0"
    return str(value)


def module_settings_view(code: str) -> list[dict[str, Any]]:
    """Поля настроек модуля для формы: схема + текущее значение."""
    view: list[dict[str, Any]] = []
    for field in SETTINGS_SCHEMA.get(code, ()):
        value = setting(code, field["key"])
        item = {**field, "value": value}
        if field["type"] == "list":
            item["value_text"] = ", ".join(str(entry) for entry in (value or ()))
        elif value is None:
            item["value_text"] = ""
        else:
            item["value_text"] = str(value)
        item["step"] = field.get("step", 1 if field["type"] == "int" else "any")
        view.append(item)
    return view


def save_module_settings(engine: Engine, code: str, values: dict[str, Any]) -> list[dict[str, Any]]:
    """Валидирует и сохраняет настройки модуля, обновляет кэш."""
    fields = SETTINGS_SCHEMA.get(code)
    if fields is None:
        raise ValueError("У этого модуля нет редактируемых настроек")
    known = {field["key"]: field for field in fields}
    if not isinstance(values, dict) or not values:
        raise ValueError("Ожидались настройки для сохранения")
    if set(values) - set(known):
        raise ValueError("Неизвестные настройки")
    normalized = {key: _normalize_input(known[key], raw) for key, raw in values.items()}
    with engine.begin() as connection:
        for key, value in normalized.items():
            storage_key = f"{_SETTING_VALUE_PREFIX}{code}.{key}"
            connection.execute(text("""
                INSERT INTO web_app_settings (key, value, updated_at)
                VALUES (:key, :value, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """), {"key": storage_key, "value": _storage_text(known[key], value)})
    _settings_values.setdefault(code, {}).update(normalized)
    return module_settings_view(code)


