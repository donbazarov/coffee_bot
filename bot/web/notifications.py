"""Единый центр уведомлений: лента, настройки пользователя и доставка.

Модель:

* `notifications` — одна строка на получателя и событие: и лента в приложении,
  и лог для push (что уже отправлено). `target_url` — deep-link вида
  `/?view=qc`, `entity_type`/`entity_id` — сущность, к которой относится
  событие (по ним лента помечается прочитанной при открытии карточки).
* `user_notification_settings` — персональные тумблеры «В приложении» и «Push»
  на каждое событие. Записи нет — берём дефолт из `notification_registry`.
* Доставка централизована в `notify()`: сначала пишем ленту (если канал
  включён), потом шлём Web Push через `push.send_to_user()` (если включён и
  получатель — не автор действия). Ошибки push только логируются: центр
  уведомлений и остальное приложение работать не перестают.

Точки отправки из модулей вызывают `notify_async()` (в фоновом потоке), чтобы
сеть не тормозила ответ на действие пользователя.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from bot.web import notification_registry as registry, push

logger = logging.getLogger("bot.web.notifications")

MAX_TITLE = 120
MAX_BODY = 240
MAX_URL = 300
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
STAFF_ROLES = registry.ALL_ROLES


def initialize_schema(engine: Engine) -> None:
    """Идемпотентно создаёт таблицы центра и индекс подсчёта непрочитанных."""
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                module TEXT NOT NULL,
                event_type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                target_url TEXT NOT NULL,
                entity_type TEXT,
                entity_id INTEGER,
                is_read BOOLEAN NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_notifications_user_unread
            ON notifications(user_id, is_read, module)
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_notifications_entity
            ON notifications(user_id, entity_type, entity_id)
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS user_notification_settings (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                in_app_enabled BOOLEAN NOT NULL DEFAULT 1,
                push_enabled BOOLEAN NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, event_type)
            )
        """))


# --- Служебные -------------------------------------------------------------- #


def _iso(value: Any) -> str:
    """SQLite хранит UTC как «YYYY-MM-DD HH:MM:SS» — отдаём ISO с Z для браузера."""
    stamp = str(value or "").strip()
    return stamp.replace(" ", "T") + "Z" if stamp else ""


def _clean_targets(user_ids: Iterable[Any], actor_id: int | None = None) -> list[int]:
    """Уникальные id получателей без автора действия и мусора."""
    targets: list[int] = []
    for raw in user_ids or ():
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in targets:
            continue
        if actor_id is not None and value == int(actor_id):
            continue
        targets.append(value)
    return targets


def _push_tag(event_type: str, entity_type: str | None, entity_id: int | None) -> str:
    """Тег push: события по одной сущности схлопываются в одно уведомление."""
    base: Any = entity_id if entity_id is not None else (entity_type or "event")
    return f"{event_type}-{base}"


def _settings_for(engine: Engine, user_ids: list[int], event_type: str) -> dict[int, dict[str, bool]]:
    """Настройки каналов для получателей: запись из БД или дефолт реестра."""
    default = registry.event_defaults(event_type)
    result = {user_id: dict(default) for user_id in user_ids}
    placeholders = ", ".join(f":u{index}" for index in range(len(user_ids)))
    params: dict[str, Any] = {f"u{index}": value for index, value in enumerate(user_ids)}
    params["event_type"] = event_type
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT user_id, in_app_enabled, push_enabled
            FROM user_notification_settings
            WHERE event_type = :event_type AND user_id IN ({placeholders})
        """), params).mappings().all()
    for row in rows:
        result[int(row["user_id"])] = {
            "in_app": bool(row["in_app_enabled"]),
            "push": bool(row["push_enabled"]),
        }
    return result


def _insert_rows(engine: Engine, user_ids: list[int], event_type: str, module: str,
                 title: str, body: str, target_url: str,
                 entity_type: str | None, entity_id: int | None) -> int:
    """Пишет строки ленты одним executemany. Возвращает число записей."""
    if not user_ids:
        return 0
    rows = [
        {"user_id": user_id, "module": module, "event_type": event_type, "title": title,
         "body": body or None, "target_url": target_url, "entity_type": entity_type,
         "entity_id": entity_id}
        for user_id in user_ids
    ]
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO notifications
                (user_id, module, event_type, title, body, target_url, entity_type, entity_id)
            VALUES (:user_id, :module, :event_type, :title, :body, :target_url,
                    :entity_type, :entity_id)
        """), rows)
    return len(rows)


def _deliver_push(engine: Engine, user_ids: list[int], event_type: str, title: str,
                  body: str, target_url: str, entity_type: str | None,
                  entity_id: int | None) -> int:
    """Шлёт Web Push получателям; ошибки сети не поднимаются выше."""
    payload = {
        "title": title,
        "body": body or "",
        "url": target_url,
        "tag": _push_tag(event_type, entity_type, entity_id),
    }
    delivered = 0
    for user_id in user_ids:
        try:
            delivered += push.send_to_user(engine, user_id, payload)
        except Exception as error:  # noqa: BLE001 — push не должен ломать приложение
            logger.warning("Уведомления: push пользователю %s не отправлен: %s", user_id, error)
    return delivered


# --- Отправка --------------------------------------------------------------- #


def active_user_ids(engine: Engine, roles: Iterable[str] | None = None,
                    exclude_user_id: int | None = None) -> list[int]:
    """Активные сотрудники (для событий «всей команде», вроде анонса)."""
    allowed = tuple(roles or STAFF_ROLES)
    placeholders = ", ".join(f":r{index}" for index in range(len(allowed)))
    params: dict[str, Any] = {f"r{index}": role for index, role in enumerate(allowed)}
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT id FROM users WHERE is_active = 1 AND role IN ({placeholders})
        """), params).scalars().all()
    result = [int(value) for value in rows]
    if exclude_user_id is not None:
        result = [value for value in result if value != int(exclude_user_id)]
    return result


def notify(engine: Engine, user_ids: Iterable[int], event_type: str, title: str,
           body: str = "", target_url: str = "/", entity_type: str | None = None,
           entity_id: int | None = None, actor_id: int | None = None,
           channels: Iterable[str] = ("in_app", "push")) -> int:
    """Центральная точка отправки: лента + push по настройкам получателей.

    Возвращает число записанных в ленту уведомлений. Неизвестный `event_type`
    не роняет действие пользователя: логируем и отправляем как анонс — так
    опечатка в новом вызове видна в логах, но не ломает фичу. Параметр
    `channels` ограничивает каналы (чат пишет ленту сам, а push шлёт своей
    логикой с учётом режима уведомлений чата).
    """
    allowed = {str(value) for value in channels}
    event = registry.get_event(event_type)
    if event is None:
        logger.warning("Уведомления: неизвестное событие %r", event_type)
    module = event["module"] if event else "announcements"
    clean_title = " ".join(str(title or "").split())[:MAX_TITLE] or registry.module_title(module)
    clean_body = " ".join(str(body or "").split())[:MAX_BODY]
    clean_url = str(target_url or "/").strip()[:MAX_URL] or "/"
    if entity_id is not None:
        try:
            entity_id = int(entity_id)
        except (TypeError, ValueError):
            entity_id = None

    targets = _clean_targets(user_ids, actor_id)
    if not targets:
        return 0
    settings = _settings_for(engine, targets, event_type)
    in_app = [user_id for user_id in targets
              if "in_app" in allowed and settings[user_id]["in_app"]]
    # Автору действия push не шлём, но строку ленты он получает (если канал включён).
    push_users = [user_id for user_id in targets
                  if "push" in allowed and settings[user_id]["push"]
                  and (actor_id is None or user_id != int(actor_id))]
    inserted = _insert_rows(engine, in_app, event_type, module, clean_title,
                            clean_body, clean_url, entity_type, entity_id)
    if push_users:
        _deliver_push(engine, push_users, event_type, clean_title, clean_body,
                      clean_url, entity_type, entity_id)
    return inserted


def notify_audience(engine: Engine, event_type: str, title: str, body: str = "",
                    target_url: str = "/", entity_type: str | None = None,
                    entity_id: int | None = None, actor_id: int | None = None,
                    roles: Iterable[str] | None = None,
                    channels: Iterable[str] = ("in_app", "push")) -> int:
    """Событие всей команде: сам собирает активных получателей (кроме автора)."""
    return notify(engine, active_user_ids(engine, roles, exclude_user_id=actor_id),
                  event_type, title, body, target_url, entity_type, entity_id, actor_id,
                  channels)


def notify_async(engine: Engine, user_ids: Iterable[int], event_type: str, title: str,
                 body: str = "", target_url: str = "/", entity_type: str | None = None,
                 entity_id: int | None = None, actor_id: int | None = None,
                 channels: Iterable[str] = ("in_app", "push")) -> None:
    """То же, что `notify()`, но в фоновом потоке — ответ не ждёт сеть push."""
    targets = list(user_ids or ())
    if not targets:
        return
    defined_channels = tuple(channels)
    def _run() -> None:
        try:
            notify(engine, targets, event_type, title, body, target_url,
                   entity_type, entity_id, actor_id, defined_channels)
        except Exception as error:  # noqa: BLE001 — уведомления не ломают действие
            logger.warning("Уведомления: отправка %r не удалась: %s", event_type, error)
    try:
        threading.Thread(target=_run, daemon=True).start()
    except RuntimeError:  # интерпретатор на выгрузке — отправляем синхронно
        _run()



def notify_audience_async(engine: Engine, event_type: str, title: str, body: str = "",
                          target_url: str = "/", entity_type: str | None = None,
                          entity_id: int | None = None, actor_id: int | None = None,
                          roles: Iterable[str] | None = None,
                          channels: Iterable[str] = ("in_app", "push")) -> None:
    """Фоновый вариант `notify_audience()` для рассылок по всей команде."""
    defined_channels = tuple(channels)
    def _run() -> None:
        try:
            notify_audience(engine, event_type, title, body, target_url,
                            entity_type, entity_id, actor_id, roles, defined_channels)
        except Exception as error:  # noqa: BLE001
            logger.warning("Уведомления: рассылка %r не удалась: %s", event_type, error)
    try:
        threading.Thread(target=_run, daemon=True).start()
    except RuntimeError:
        _run()


# --- Чтение ленты ----------------------------------------------------------- #


def _item_payload(row: Any) -> dict[str, Any]:
    """Строка ленты для клиента: ISO-дата и читаемая подпись модуля."""
    return {
        "id": int(row["id"]),
        "module": row["module"],
        "module_title": registry.module_title(row["module"]),
        "event_type": row["event_type"],
        "title": row["title"],
        "body": row["body"] or "",
        "target_url": row["target_url"] or "/",
        "entity_type": row["entity_type"] or "",
        "entity_id": int(row["entity_id"]) if row["entity_id"] is not None else None,
        "is_read": bool(row["is_read"]),
        "created_at": _iso(row["created_at"]),
    }


def unread_summary(engine: Engine, user_id: int) -> dict[str, Any]:
    """Число непрочитанных: суммарно и по модулям (для бейджей в сайдбаре).

    Модули без непрочитанного в ответе отсутствуют — клиент читает отсутствие
    ключа как ноль и гасит бейдж.
    """
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT module, COUNT(*) AS total FROM notifications
            WHERE user_id = :user_id AND is_read = 0
            GROUP BY module
        """), {"user_id": user_id}).mappings().all()
    by_module = {row["module"]: int(row["total"]) for row in rows}
    return {"total": sum(by_module.values()), "by_module": by_module}


def list_for_user(engine: Engine, user_id: int, limit: int = DEFAULT_LIMIT,
                  offset: int = 0, module: str | None = None,
                  unread_only: bool = False) -> dict[str, Any]:
    """Страница ленты текущего сотрудника (свежие — первыми)."""
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    where = ["user_id = :user_id"]
    params: dict[str, Any] = {"user_id": user_id, "limit": limit, "offset": offset}
    if module and registry.get_module(module):
        where.append("module = :module")
        params["module"] = module
    if unread_only:
        where.append("is_read = 0")
    clause = " AND ".join(where)
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT id, module, event_type, title, body, target_url,
                   entity_type, entity_id, is_read, created_at
            FROM notifications WHERE {clause}
            ORDER BY id DESC LIMIT :limit OFFSET :offset
        """), params).mappings().all()
        total = int(connection.execute(
            text(f"SELECT COUNT(*) FROM notifications WHERE {clause}"), params
        ).scalar_one())
    items = [_item_payload(row) for row in rows]
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
        "unread": unread_summary(engine, user_id),
    }


def mark_read(engine: Engine, user_id: int, ids: Iterable[int] | None = None, *,
              everything: bool = False, entity_type: str | None = None,
              entity_id: int | None = None, module: str | None = None) -> int:
    """Помечает прочитанным: список id, сущность, модуль или всё сразу."""
    with engine.begin() as connection:
        if ids:
            values = [int(value) for value in ids if str(value).strip().lstrip("-").isdigit()]
            values = [value for value in values if value > 0]
            if not values:
                return 0
            placeholders = ", ".join(f":id{index}" for index in range(len(values)))
            params: dict[str, Any] = {"user_id": user_id}
            params.update({f"id{index}": value for index, value in enumerate(values)})
            result = connection.execute(text(f"""
                UPDATE notifications SET is_read = 1
                WHERE user_id = :user_id AND is_read = 0 AND id IN ({placeholders})
            """), params)
            return int(result.rowcount or 0)
        if entity_type:
            params = {"user_id": user_id, "entity_type": str(entity_type)}
            suffix = ""
            if entity_id is not None:
                suffix = " AND entity_id = :entity_id"
                params["entity_id"] = int(entity_id)
            result = connection.execute(text(f"""
                UPDATE notifications SET is_read = 1
                WHERE user_id = :user_id AND is_read = 0 AND entity_type = :entity_type{suffix}
            """), params)
            return int(result.rowcount or 0)
        # Открытие раздела целиком: гасим непрочитанное конкретного модуля.
        if module:
            if not registry.get_module(module):
                return 0
            result = connection.execute(text("""
                UPDATE notifications SET is_read = 1
                WHERE user_id = :user_id AND is_read = 0 AND module = :module
            """), {"user_id": user_id, "module": module})
            return int(result.rowcount or 0)
        # Остался режим «Прочитать всё»: остальные случаи обработаны выше.
        if not everything:
            return 0
        result = connection.execute(
            text("UPDATE notifications SET is_read = 1 WHERE user_id = :user_id AND is_read = 0"),
            {"user_id": user_id},
        )
    return int(result.rowcount or 0)


# --- Настройки пользователя ------------------------------------------------- #



def _stored_settings(engine: Engine, user_id: int) -> dict[str, dict[str, bool]]:
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT event_type, in_app_enabled, push_enabled
            FROM user_notification_settings WHERE user_id = :user_id
        """), {"user_id": user_id}).mappings().all()
    return {
        row["event_type"]: {"in_app": bool(row["in_app_enabled"]),
                            "push": bool(row["push_enabled"])}
        for row in rows
    }


def get_settings(engine: Engine, user_id: int) -> dict[str, dict[str, bool]]:
    """Настройки по всем событиям: сохранённое значение или дефолт реестра."""
    stored = _stored_settings(engine, user_id)
    return {
        code: stored.get(code, registry.event_defaults(code))
        for code in registry.event_codes()
    }


def settings_view(engine: Engine, user_id: int) -> list[dict[str, Any]]:
    """Схема реестра + текущие значения — для секции в личном кабинете."""
    current = get_settings(engine, user_id)
    grouped: list[dict[str, Any]] = []
    for module in registry.settings_schema():
        events = [
            {**event, **current.get(event["code"], registry.event_defaults(event["code"]))}
            for event in module["events"]
        ]
        grouped.append({**module, "events": events})
    return grouped


def set_settings(engine: Engine, user_id: int,
                 updates: dict[str, Any] | Iterable[dict[str, Any]]) -> dict[str, dict[str, bool]]:
    """Сохраняет тумблеры «В приложении»/«Push» по переданным событиям.

    Принимает либо словарь `{event_type: {in_app, push}}`, либо список
    `[{"event_type": ..., "in_app": ..., "push": ...}]`. Незнакомые события и
    поля игнорируются — интерфейс может прислать частичное обновление.
    """
    if isinstance(updates, dict):
        normalized = dict(updates)
    else:
        normalized = {}
        for item in updates or ():
            if isinstance(item, dict) and item.get("event_type"):
                normalized[str(item["event_type"])] = item
    rows: list[dict[str, Any]] = []
    current = get_settings(engine, user_id)
    for code, raw in normalized.items():
        if not registry.is_event(code) or not isinstance(raw, dict) or not raw:
            continue
        # Частичное обновление: незаданный тумблер сохраняем как есть.
        values = dict(current.get(code) or registry.event_defaults(code))
        if isinstance(raw.get("in_app"), bool):
            values["in_app"] = raw["in_app"]
        if isinstance(raw.get("push"), bool):
            values["push"] = raw["push"]
        rows.append({
            "user_id": user_id,
            "event_type": code,
            "in_app": 1 if values["in_app"] else 0,
            "push": 1 if values["push"] else 0,
        })
    if not rows:
        return get_settings(engine, user_id)
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO user_notification_settings (user_id, event_type, in_app_enabled, push_enabled)
            VALUES (:user_id, :event_type, :in_app, :push)
            ON CONFLICT(user_id, event_type) DO UPDATE SET
                in_app_enabled = excluded.in_app_enabled,
                push_enabled = excluded.push_enabled
        """), rows)
    return get_settings(engine, user_id)


def reset_settings(engine: Engine, user_id: int) -> dict[str, dict[str, bool]]:
    """Возвращает дефолты реестра: удаляем персональные оверрайды."""
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM user_notification_settings WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
    return get_settings(engine, user_id)
