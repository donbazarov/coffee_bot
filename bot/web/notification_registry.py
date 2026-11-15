"""Реестр событий Единого центра уведомлений.

Единственный источник истины о том, какие события вообще существуют: модуль-
источник, русскоязычная метка для интерфейса, роли-получатели и дефолтные
настройки каналов («В приложении» / «Push»). Логика хранения и доставки живёт
в `bot/web/notifications.py`, точки отправки — `notifications.notify()`.

Событие (dict):
* `code`   — стабильный ключ (`qc_scored`, `task_assigned`, …) — он же
             `notifications.event_type` и ключ в `user_notification_settings`;
* `module` — код модуля-источника из `NOTIFICATION_MODULES`;
* `label`  — название события в личном кабинете;
* `hint`   — короткое пояснение, когда событие срабатывает;
* `roles`  — кому событие может приходить (роли получателей);
* `in_app` — дефолт канала «В приложении»;
* `push`   — дефолт канала «Push».

Модули (`NOTIFICATION_MODULES`) намеренно отделены от `modules.MODULES`: это
подписи и иконки для центра уведомлений, а не флаги вкл/выкл разделов сайта.
Коды модулей совпадают с владельцем события, поэтому бейдж в сайдбаре можно
привязать к пункту меню по `view`.
"""

from __future__ import annotations

from typing import Any

# Модули-источники событий: код → название, иконка (`<use href="#i-...">`) и
# вьюха SPA, к которой относится бейдж в сайдбаре.
NOTIFICATION_MODULES: tuple[dict[str, str], ...] = (
    {"code": "qc", "title": "Контроль качества", "icon": "i-poll", "view": "quality"},
    {"code": "shifts", "title": "График смен", "icon": "i-calendar", "view": "calendar"},
    {"code": "tasks", "title": "Задачи", "icon": "i-kanban", "view": "tasks"},
    {"code": "kb", "title": "База знаний", "icon": "i-book", "view": "kb"},
    {"code": "announcements", "title": "Анонсы", "icon": "i-bell", "view": "announcements"},
    {"code": "chat", "title": "Чат", "icon": "i-chat", "view": "chat"},
)

# Роли, которые получают уведомления: те же, что и в push-рассылке.
ALL_ROLES: tuple[str, ...] = ("newbie", "barista", "senior", "mentor")

NOTIFICATION_EVENTS: tuple[dict[str, Any], ...] = (
    {
        "code": "qc_scored", "module": "qc", "label": "Новая оценка качества",
        "hint": "Наставник выставил вам оценку напитка, чистоты или порядка",
        "roles": ALL_ROLES, "in_app": True, "push": True,
    },
    {
        "code": "shift_swapped", "module": "shifts", "label": "Замена смены",
        "hint": "В графике появилась замена, которая касается вас",
        "roles": ALL_ROLES, "in_app": True, "push": True,
    },
    {
        "code": "shift_published", "module": "shifts", "label": "Опубликован график",
        "hint": "Опубликован новый график смен или правки к нему",
        "roles": ALL_ROLES, "in_app": True, "push": True,
    },
    {
        "code": "task_assigned", "module": "tasks", "label": "Назначена задача",
        "hint": "Вам или вашей должности поставили задачу",
        "roles": ALL_ROLES, "in_app": True, "push": True,
    },
    {
        "code": "task_completed", "module": "tasks", "label": "Задача выполнена",
        "hint": "Вашу задачу перенесли в «Готово»",
        "roles": ALL_ROLES, "in_app": True, "push": False,
    },
    {
        "code": "kb_page_updated", "module": "kb", "label": "Обновлена статья",
        "hint": "В базе знаний изменили или добавили страницу",
        "roles": ALL_ROLES, "in_app": True, "push": False,
    },
    {
        "code": "kb_drink_updated", "module": "kb", "label": "Обновлён справочник",
        "hint": "Правки в справочнике напитков или зерна",
        "roles": ALL_ROLES, "in_app": True, "push": False,
    },
    {
        "code": "announcement_new", "module": "announcements", "label": "Новый анонс",
        "hint": "Опубликован анонс: событие, расписание или общая информация",
        "roles": ALL_ROLES, "in_app": True, "push": True,
    },
    {
        "code": "chat_message", "module": "chat", "label": "Личное сообщение",
        "hint": "Вам написали в личном чате",
        "roles": ALL_ROLES, "in_app": True, "push": True,
    },
    {
        "code": "chat_mention", "module": "chat", "label": "Упоминание в чате",
        "hint": "Вас упомянули в общем чате",
        "roles": ALL_ROLES, "in_app": True, "push": True,
    },
)

_BY_CODE: dict[str, dict[str, Any]] = {event["code"]: event for event in NOTIFICATION_EVENTS}
_MODULES_BY_CODE: dict[str, dict[str, str]] = {item["code"]: item for item in NOTIFICATION_MODULES}


def module_codes() -> tuple[str, ...]:
    """Коды модулей-источников в порядке реестра."""
    return tuple(item["code"] for item in NOTIFICATION_MODULES)


def get_module(code: str) -> dict[str, str] | None:
    return _MODULES_BY_CODE.get(code)


def module_title(code: str) -> str:
    """Название модуля для интерфейса; неизвестный код — сам код."""
    module = _MODULES_BY_CODE.get(code)
    return module["title"] if module else code


def module_view(code: str) -> str:
    """Значение `data-view` пункта меню, к которому относится модуль."""
    module = _MODULES_BY_CODE.get(code)
    return module["view"] if module else ""


def event_codes() -> tuple[str, ...]:
    return tuple(event["code"] for event in NOTIFICATION_EVENTS)


def is_event(code: str) -> bool:
    return code in _BY_CODE


def get_event(code: str) -> dict[str, Any] | None:
    return _BY_CODE.get(code)


def event_defaults(code: str) -> dict[str, bool]:
    """Дефолтные настройки каналов события (fallback без записи в БД)."""
    event = _BY_CODE.get(code)
    if event is None:
        return {"in_app": True, "push": False}
    return {"in_app": bool(event["in_app"]), "push": bool(event["push"])}


def roles_for(code: str) -> tuple[str, ...]:
    event = _BY_CODE.get(code)
    return tuple(event["roles"]) if event else ALL_ROLES


def settings_schema() -> list[dict[str, Any]]:
    """События, сгруппированные по модулям, — для личного кабинета.

    Модули без событий не отдаём: пустая группа в настройках только мешает.
    """
    grouped: list[dict[str, Any]] = []
    for module in NOTIFICATION_MODULES:
        events = [
            {"code": event["code"], "label": event["label"], "hint": event["hint"],
             "in_app": bool(event["in_app"]), "push": bool(event["push"])}
            for event in NOTIFICATION_EVENTS if event["module"] == module["code"]
        ]
        if events:
            grouped.append({**module, "events": events})
    return grouped
