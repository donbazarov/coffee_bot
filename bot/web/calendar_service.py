import calendar
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.engine import Engine

from bot.web.schedule_snapshot import (
    active_employee_ids,
    prune_snapshots,
    render_schedule_snapshots,
)

ROLE_NAMES = {"barista": "Бариста", "senior": "Старший", "mentor": "Наставник"}
SHIFT_TEMPLATE_POINTS = {"УЯ", "ДЕ"}
# Максимум дней в одной публикации: 92 дня — это ровно 12 снимков по 8 дней.
MAX_PUBLISH_DAYS = 92
# Оформление аккаунта: тема и акцентный цвет (по умолчанию мягкий коралл).
DEFAULT_ACCENT = "#f47369"
THEME_VALUES = {"system", "light", "dark"}
# Кого уведомлять о сообщениях чата: все | только ЛС и упоминания | ничего.
CHAT_NOTIFY_VALUES = {"all", "dm_mentions", "none"}
DEFAULT_CHAT_NOTIFY = "dm_mentions"
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
# Каналы Telegram для публикаций. Стартуют из окружения, потом правятся в панели управления.
DEFAULT_ANNOUNCE_CHAT = os.getenv("TELEGRAM_ANNOUNCE_CHAT_ID", "@nefttest1")
DEFAULT_SWAP_CHAT = os.getenv("TELEGRAM_SWAP_CHAT_ID", "@nefttest2")


def app_timezone() -> tzinfo:
    try:
        return ZoneInfo(os.getenv("WEB_TIMEZONE", "Europe/Moscow"))
    except ZoneInfoNotFoundError:
        return timezone.utc


def _ensure_columns(connection: Any, table: str, columns: dict[str, str]) -> None:
    """Добавляет недостающие колонки в существующую таблицу (SQLite).

    `CREATE TABLE IF NOT EXISTS` не меняет уже созданную таблицу, а база живёт
    между релизами, поэтому новые поля нужно докатывать отдельно.
    """
    existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def initialize_calendar_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_shift_change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id INTEGER REFERENCES users(id),
                actor_name TEXT NOT NULL,
                employee_id INTEGER REFERENCES users(id),
                employee_name TEXT NOT NULL,
                shift_date DATE NOT NULL,
                action TEXT NOT NULL,
                old_start_time TEXT,
                old_end_time TEXT,
                old_point TEXT,
                new_start_time TEXT,
                new_end_time TEXT,
                new_point TEXT,
                change_type TEXT NOT NULL DEFAULT 'swap',
                snapshot_path TEXT,
                snapshot_paths TEXT,
                period_start DATE,
                period_end DATE,
                changes_count INTEGER,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        if engine.dialect.name == "sqlite":
            # Существующие базы: докидываем поля типа правки и снимка «Расписания».
            _ensure_columns(connection, "web_shift_change_log", {
                "change_type": "TEXT NOT NULL DEFAULT 'swap'",
                "snapshot_path": "TEXT",
                "snapshot_paths": "TEXT",
                "period_start": "DATE",
                "period_end": "DATE",
                "changes_count": "INTEGER",
            })
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_web_shift_log_date
            ON web_shift_change_log (shift_date, created_at)
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_user_preferences (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                quality_enabled INTEGER NOT NULL DEFAULT 1,
                calendar_enabled INTEGER NOT NULL DEFAULT 1,
                theme TEXT NOT NULL DEFAULT 'system',
                accent TEXT NOT NULL DEFAULT '#f47369',
                chat_notify TEXT NOT NULL DEFAULT 'dm_mentions',
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        if engine.dialect.name == "sqlite":
            # Существующие базы: докидываем оформление и данные аккаунта.
            _ensure_columns(connection, "web_user_preferences", {
                "theme": "TEXT NOT NULL DEFAULT 'system'",
                "accent": "TEXT NOT NULL DEFAULT '#f47369'",
                "chat_notify": "TEXT NOT NULL DEFAULT 'dm_mentions'",
            })
            _ensure_columns(connection, "users", {
                "display_name": "TEXT",
                "avatar_rev": "INTEGER NOT NULL DEFAULT 0",
            })
        # Шаблоны смен — общий набор команды: их ведут наставники, применяют все.
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_shift_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                point TEXT NOT NULL,
                order_index INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_web_shift_templates_user
            ON web_shift_templates (user_id, order_index, id)
        """))
        # Общие настройки сайта (каналы Telegram для публикаций).
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            INSERT OR IGNORE INTO web_app_settings (key, value) VALUES ('announce_chat', :announce)
        """), {"announce": DEFAULT_ANNOUNCE_CHAT})
        connection.execute(text("""
            INSERT OR IGNORE INTO web_app_settings (key, value) VALUES ('swap_chat', :swap)
        """), {"swap": DEFAULT_SWAP_CHAT})


def _validate_template_payload(payload: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    allowed = {"name", "start_time", "end_time", "point"}
    if set(payload) - allowed:
        raise ValueError("Переданы неизвестные поля шаблона")
    values = dict(payload)
    if "name" in values or not partial:
        name = str(values.get("name", "")).strip()
        if not name or len(name) > 60:
            raise ValueError("Название шаблона должно содержать от 1 до 60 символов")
        values["name"] = name
    for field in ("start_time", "end_time"):
        if field in values or not partial:
            try:
                values[field] = time.fromisoformat(str(values.get(field, ""))).strftime("%H:%M")
            except (ValueError, TypeError) as error:
                raise ValueError("Время должно быть в формате ЧЧ:ММ") from error
    if not partial and values.get("start_time") == values.get("end_time"):
        raise ValueError("Время начала и окончания не должны совпадать")
    if "point" in values or not partial:
        if values.get("point") not in SHIFT_TEMPLATE_POINTS:
            raise ValueError("Точка должна быть УЯ или ДЕ")
    return values


def _template_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "start": _time_text(row["start_time"]),
        "end": _time_text(row["end_time"]),
        "point": row["point"],
        "order_index": row["order_index"],
    }


def list_shift_templates(engine: Engine) -> list[dict[str, Any]]:
    """Общий набор шаблонов команды: их ведут наставники, применяют все."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT id, name, start_time, end_time, point, order_index
            FROM web_shift_templates
            ORDER BY order_index, id
        """)).mappings().all()
    return [_template_dict(row) for row in rows]


def create_shift_template(engine: Engine, user_id: int, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Ожидался объект шаблона")
    values = _validate_template_payload(payload)
    with engine.begin() as connection:
        order_index = connection.execute(text(
            "SELECT COALESCE(MAX(order_index), 0) + 1 FROM web_shift_templates"
        )).scalar_one()
        template_id = connection.execute(text("""
            INSERT INTO web_shift_templates (user_id, name, start_time, end_time, point, order_index)
            VALUES (:user_id, :name, :start_time, :end_time, :point, :order_index)
            RETURNING id
        """), {**values, "user_id": user_id, "order_index": order_index}).scalar_one()
        row = connection.execute(text("""
            SELECT id, name, start_time, end_time, point, order_index
            FROM web_shift_templates WHERE id = :id
        """), {"id": template_id}).mappings().one()
    return _template_dict(row)


def update_shift_template(engine: Engine, template_id: int, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Ожидался объект шаблона")
    values = _validate_template_payload(payload, partial=True)
    if not values:
        raise ValueError("Нет полей для обновления")
    with engine.begin() as connection:
        existing = connection.execute(text(
            "SELECT start_time, end_time FROM web_shift_templates WHERE id = :id"
        ), {"id": template_id}).mappings().first()
        if not existing:
            raise LookupError("Шаблон не найден")
        merged_start = values.get("start_time", _time_text(existing["start_time"]))
        merged_end = values.get("end_time", _time_text(existing["end_time"]))
        if merged_start == merged_end:
            raise ValueError("Время начала и окончания не должны совпадать")
        assignments = ", ".join(f"{key} = :{key}" for key in values)
        connection.execute(text(
            f"UPDATE web_shift_templates SET {assignments}, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = :id"
        ), {**values, "id": template_id})
        row = connection.execute(text("""
            SELECT id, name, start_time, end_time, point, order_index
            FROM web_shift_templates WHERE id = :id
        """), {"id": template_id}).mappings().one()
    return _template_dict(row)


def delete_shift_template(engine: Engine, template_id: int) -> bool:
    with engine.begin() as connection:
        result = connection.execute(text(
            "DELETE FROM web_shift_templates WHERE id = :id"
        ), {"id": template_id})
        return result.rowcount > 0


def reorder_shift_templates(engine: Engine, ordered_ids: Any) -> list[dict[str, Any]]:
    """Сохраняет новый порядок общего набора шаблонов (порядок задаёт клиент)."""
    if not isinstance(ordered_ids, list) or not ordered_ids:
        raise ValueError("Ожидался непустой список шаблонов")
    try:
        ids = [int(item) for item in ordered_ids]
    except (TypeError, ValueError) as error:
        raise ValueError("Идентификаторы шаблонов должны быть числами") from error
    if len(set(ids)) != len(ids):
        raise ValueError("Идентификаторы шаблонов повторяются")
    with engine.begin() as connection:
        known = [row[0] for row in connection.execute(text("SELECT id FROM web_shift_templates"))]
        if set(ids) != set(known):
            raise ValueError("Список шаблонов не совпадает с сохранёнными")
        for index, template_id in enumerate(ids):
            connection.execute(text("""
                UPDATE web_shift_templates SET order_index = :index, updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
            """), {"index": index, "id": template_id})
    return list_shift_templates(engine)


def _normalize_chat(value: Any) -> str:
    """Приводит ссылку/юзернейм канала к виду, понятному Bot API (@name или -100…)."""
    text_value = str(value or "").strip()
    if not text_value:
        return ""
    if text_value.startswith("-") and text_value.lstrip("-").isdigit():
        return text_value
    trimmed = text_value.split("?", 1)[0].rstrip("/")
    for prefix in ("https://", "http://"):
        if trimmed.startswith(prefix):
            trimmed = trimmed[len(prefix):]
    if trimmed.startswith("t.me/"):
        trimmed = trimmed[len("t.me/"):]
    trimmed = trimmed.lstrip("@")
    if not trimmed or trimmed.startswith("+"):
        # Приватные инвайт-ссылки (t.me/+hash) Bot API так не понимает.
        return ""
    return f"@{trimmed}"


def get_app_settings(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT key, value FROM web_app_settings")).mappings().all()
    values = {row["key"]: row["value"] for row in rows}
    return {
        "announce_chat": values.get("announce_chat", DEFAULT_ANNOUNCE_CHAT),
        "swap_chat": values.get("swap_chat", DEFAULT_SWAP_CHAT),
    }


def save_app_settings(engine: Engine, values: dict[str, Any]) -> dict[str, str]:
    allowed = {"announce_chat", "swap_chat"}
    if not isinstance(values, dict) or not values:
        raise ValueError("Ожидались настройки для сохранения")
    if set(values) - allowed:
        raise ValueError("Неизвестные настройки")
    normalized = {key: _normalize_chat(value) for key, value in values.items()}
    with engine.begin() as connection:
        for key, value in normalized.items():
            connection.execute(text("""
                INSERT INTO web_app_settings (key, value, updated_at)
                VALUES (:key, :value, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """), {"key": key, "value": value})
    return get_app_settings(engine)


def _time_text(value: Any) -> str:
    return str(value or "")[:5]


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _as_time(value: Any) -> time:
    return value if isinstance(value, time) else time.fromisoformat(_time_text(value))


def _shift_bounds(shift_date: Any, start_time: Any, end_time: Any, zone: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(_as_date(shift_date), _as_time(start_time), tzinfo=zone)
    end_date = _as_date(shift_date)
    end_clock = _as_time(end_time)
    if end_clock <= _as_time(start_time):
        end_date += timedelta(days=1)
    return start, datetime.combine(end_date, end_clock, tzinfo=zone)


def _shift_dict(row: Any, current_user_id: int) -> dict[str, Any]:
    return {
        "id": row["shift_id"],
        "date": str(row["shift_date"])[:10],
        "employee_id": row["user_id"],
        "employee": row["employee_name"] or f"iiko {row['iiko_id']}",
        "iiko_id": row["iiko_id"],
        "start": _time_text(row["start_time"]),
        "end": _time_text(row["end_time"]),
        "point": row["point"],
        "shift_type": row["shift_type"],
        "own": row["user_id"] == current_user_id,
    }


def get_month_calendar(engine: Engine, user: dict[str, Any], year: int, month: int) -> dict[str, Any]:
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    zone = app_timezone()
    now = datetime.now(zone)
    with engine.connect() as connection:
        employee = connection.execute(
            text("SELECT iiko_id FROM users WHERE id = :id AND is_active = 1"), {"id": user["id"]}
        ).mappings().first()
        rows = connection.execute(text("""
            SELECT s.shift_id, s.shift_date, s.iiko_id, st.start_time, st.end_time,
                   st.point, st.shift_type, u.id AS user_id, COALESCE(u.display_name, u.name) AS employee_name
            FROM schedule AS s
            JOIN shift_types AS st ON st.id = s.shift_type_id
            LEFT JOIN users AS u ON CAST(u.iiko_id AS TEXT) = s.iiko_id
            WHERE s.shift_date >= :first_day AND s.shift_date <= :last_day
              AND COALESCE(s.is_active, 1) = 1
            ORDER BY s.shift_date, st.start_time, u.name
        """), {"first_day": first_day.isoformat(), "last_day": last_day.isoformat()}).mappings().all()
        employees = connection.execute(text("""
            SELECT id, COALESCE(display_name, name) AS name, iiko_id, role, avatar_rev FROM users
            WHERE is_active = 1 AND role IN ('barista', 'senior', 'mentor')
            ORDER BY COALESCE(display_name, name) COLLATE NOCASE
        """)).mappings().all()
        current_user_id = user["id"]
        own_iiko_id = str(employee["iiko_id"]) if employee and employee["iiko_id"] is not None else None
        upcoming_rows = []
        if own_iiko_id:
            upcoming_rows = connection.execute(text("""
                SELECT s.shift_id, s.shift_date, s.iiko_id, st.start_time, st.end_time,
                       st.point, st.shift_type, u.id AS user_id, COALESCE(u.display_name, u.name) AS employee_name
                FROM schedule AS s
                JOIN shift_types AS st ON st.id = s.shift_type_id
                LEFT JOIN users AS u ON CAST(u.iiko_id AS TEXT) = s.iiko_id
                WHERE s.iiko_id = :iiko_id AND s.shift_date >= :today
                  AND COALESCE(s.is_active, 1) = 1
                ORDER BY s.shift_date, st.start_time LIMIT 20
                """), {"iiko_id": own_iiko_id, "today": now.date().isoformat()}).mappings().all()

    shifts = [_shift_dict(row, user["id"]) for row in rows]
    own_rows = [row for row in rows if own_iiko_id is not None and str(row["iiko_id"]) == own_iiko_id]
    total_minutes = 0
    worked_minutes = 0
    upcoming = []
    for row in own_rows:
        start, end = _shift_bounds(row["shift_date"], row["start_time"], row["end_time"], zone)
        duration = max(0, int((end - start).total_seconds() // 60))
        total_minutes += duration
        if end <= now:
            worked_minutes += duration
        elif start < now:
            worked_minutes += max(0, int((now - start).total_seconds() // 60))
    for row in upcoming_rows:
        start, end = _shift_bounds(row["shift_date"], row["start_time"], row["end_time"], zone)
        if end > now:
            upcoming.append(_shift_dict(row, current_user_id))

    upcoming.sort(key=lambda shift: (shift["date"], shift["start"]))
    return {
        "year": year,
        "month": month,
        "current_user_id": current_user_id,
        "current_user_role": user.get("role"),
        "first_weekday": first_day.weekday(),
        "days_in_month": last_day.day,
        "shifts": shifts,
        "employees": [
            {"id": row["id"], "name": row["name"], "iiko_id": row["iiko_id"],
             "role": row["role"], "avatar_rev": row["avatar_rev"] or 0}
            for row in employees
        ],
        "month_stats": {
            "total_hours": round(total_minutes / 60, 1),
            "worked_hours": round(worked_minutes / 60, 1),
            "remaining_hours": round(max(0, total_minutes - worked_minutes) / 60, 1),
            "upcoming": upcoming[:5],
        },
    }


def _shift_type_id(connection: Any, start_time: str, end_time: str, point: str) -> int:
    found = connection.execute(text("""
        SELECT id FROM shift_types
        WHERE strftime('%H:%M', start_time) = :start_time
          AND strftime('%H:%M', end_time) = :end_time AND point = :point
        ORDER BY id LIMIT 1
    """), {"start_time": start_time, "end_time": end_time, "point": point}).scalar_one_or_none()
    if found is not None:
        return int(found)
    start_hour = int(start_time[:2])
    shift_kind = "morning" if start_hour < 10 else "evening" if start_hour >= 14 else "hybrid"
    label = {"morning": "Утро", "hybrid": "Пересмена", "evening": "Вечер"}[shift_kind]
    return int(connection.execute(text("""
        INSERT INTO shift_types (start_time, end_time, point, name, shift_type, created_at)
        VALUES (:start_time, :end_time, :point, :name, :shift_kind, CURRENT_TIMESTAMP)
        RETURNING id
    """), {"start_time": start_time, "end_time": end_time, "point": point, "name": f"{label} {point}", "shift_kind": shift_kind}).scalar_one())


def _swap_lines(changes: list[dict[str, Any]]) -> list[str]:
    """Строки изменений для лога и для дублирования в канал замен."""
    lines: list[str] = []
    for cell in changes:
        employee = cell["employee"]["name"]
        if cell["delete"]:
            for old in cell["old_rows"]:
                lines.append(
                    f"{cell['date']} · {employee}: выходной "
                    f"(было {_time_text(old['start_time'])}–{_time_text(old['end_time'])}, {old['point']})"
                )
            continue
        if cell["old_rows"]:
            old = cell["old_rows"][0]
            lines.append(
                f"{cell['date']} · {employee}: {_time_text(old['start_time'])}–{_time_text(old['end_time'])}, "
                f"{old['point']} → {cell['start_time']}–{cell['end_time']}, {cell['point']}"
            )
        else:
            lines.append(f"{cell['date']} · {employee}: {cell['start_time']}–{cell['end_time']}, {cell['point']}")
    return lines


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _date_label(value: Any) -> str:
    return _to_date(value).strftime("%d.%m.%Y")


def _publish_period(start: Any, end: Any) -> tuple[date | None, date | None]:
    """Проверяет диапазон публикации: либо обе даты, либо ни одной."""
    empty = (None, "")
    if start in empty and end in empty:
        return None, None
    if start in empty or end in empty:
        raise ValueError("Нужны обе даты диапазона публикации")
    try:
        first, last = _to_date(start), _to_date(end)
    except (TypeError, ValueError) as error:
        raise ValueError("Не удалось разобрать даты диапазона публикации") from error
    if last < first:
        first, last = last, first
    if (last - first).days + 1 > MAX_PUBLISH_DAYS:
        raise ValueError(f"Диапазон публикации длиннее {MAX_PUBLISH_DAYS} дней")
    return first, last


def apply_schedule_changes(engine: Engine, actor_id: int, operations: list[dict[str, Any]],
                           change_type: str = "swap", publish: bool = True,
                           publish_start: Any = None, publish_end: Any = None) -> dict[str, Any]:
    if change_type not in {"swap", "schedule"}:
        raise ValueError("Неизвестный тип правки")
    if change_type != "schedule":
        publish = True
    # Диапазон публикации задаётся только для «Расписания»; для замен — не используется.
    publish_from, publish_to = _publish_period(publish_start, publish_end) if change_type == "schedule" else (None, None)
    if len(operations) > 500:
        raise ValueError("Превышен лимит в 500 ячеек")
    # Публикация «Расписания» может идти без правок — тогда обязателен диапазон:
    # так наставник просто рассылает снимки уже утверждённого графика.
    if not operations and not (publish and publish_from and publish_to):
        raise ValueError("Нет изменений или превышен лимит в 500 ячеек")

    prepared = []
    seen_cells = set()
    for operation in operations:
        shift_date = date.fromisoformat(str(operation["date"]))
        user_id = int(operation["user_id"])
        cell_key = (user_id, shift_date)
        if cell_key in seen_cells:
            raise ValueError("Изменения содержат повторяющиеся ячейки")
        seen_cells.add(cell_key)
        if operation.get("delete"):
            prepared.append({"date": shift_date.isoformat(), "user_id": user_id, "delete": True})
            continue
        start_time = time.fromisoformat(str(operation["start_time"])).strftime("%H:%M")
        end_time = time.fromisoformat(str(operation["end_time"])).strftime("%H:%M")
        point = str(operation["point"])
        if point not in {"УЯ", "ДЕ"}:
            raise ValueError("Точка должна быть УЯ или ДЕ")
        if start_time == end_time:
            raise ValueError("Время начала и окончания не должны совпадать")
        prepared.append({
            "date": shift_date.isoformat(), "user_id": user_id, "delete": False,
            "start_time": start_time, "end_time": end_time, "point": point,
        })

    with engine.begin() as connection:
        actor = connection.execute(text("SELECT name, role FROM users WHERE id=:id AND is_active=1"), {"id": actor_id}).mappings().first()
        if not actor:
            raise LookupError("Пользователь не найден или отключён")
        if change_type == "schedule" and actor["role"] != "mentor":
            raise PermissionError("Тип «Расписание» доступен только наставникам")

        cells = []
        for operation in prepared:
            employee = connection.execute(text("""
                SELECT id, name, iiko_id FROM users
                WHERE id=:id AND is_active=1 AND role IN ('barista','senior','mentor')
            """), {"id": operation["user_id"]}).mappings().first()
            if not employee or employee["iiko_id"] is None:
                raise LookupError("У бариста не найден активный iiko_id")
            iiko_id = str(employee["iiko_id"])
            old_rows = connection.execute(text("""
                SELECT s.shift_id, st.start_time, st.end_time, st.point
                FROM schedule s JOIN shift_types st ON st.id=s.shift_type_id
                WHERE s.shift_date=:date AND s.iiko_id=:iiko_id AND COALESCE(s.is_active,1)=1
                ORDER BY s.shift_id
            """), {"date": operation["date"], "iiko_id": iiko_id}).mappings().all()
            old = old_rows[0] if old_rows else None
            unchanged = (
                not operation["delete"] and len(old_rows) == 1 and old is not None
                and _time_text(old["start_time"]) == operation["start_time"]
                and _time_text(old["end_time"]) == operation["end_time"]
                and old["point"] == operation["point"]
            )
            cells.append({**operation, "employee": employee, "iiko_id": iiko_id, "old_rows": old_rows, "unchanged": unchanged})

        changes = [cell for cell in cells if not cell["unchanged"]]
        affected_keys = {(cell["date"], cell["iiko_id"]) for cell in changes}
        for cell in changes:
            if cell["delete"]:
                continue
            conflict = connection.execute(text("""
                SELECT 1 FROM schedule WHERE shift_date=:date AND iiko_id=:iiko_id
                    AND COALESCE(is_active,1)=1 LIMIT 1
            """), {"date": cell["date"], "iiko_id": cell["iiko_id"]}).first()
            if conflict and (cell["date"], cell["iiko_id"]) not in affected_keys:
                raise FileExistsError(f"{cell['employee']['name']} уже имеет смену {cell['date']}")

        for cell in changes:
            if change_type == "schedule":
                # «Расписание» не пишет построчный лог — ниже создаётся один снимок.
                continue
            for old in cell["old_rows"]:
                connection.execute(text("""
                    INSERT INTO web_shift_change_log (
                        actor_user_id, actor_name, employee_id, employee_name, shift_date, action,
                        old_start_time, old_end_time, old_point, new_start_time, new_end_time, new_point
                    ) VALUES (
                        :actor_id, :actor_name, :employee_id, :employee_name, :shift_date, :action,
                        :old_start, :old_end, :old_point, :new_start, :new_end, :new_point
                    )
                """), {
                    "actor_id": actor_id, "actor_name": actor["name"],
                    "employee_id": cell["employee"]["id"], "employee_name": cell["employee"]["name"],
                    "shift_date": cell["date"], "action": "deleted" if cell["delete"] else "updated",
                    "old_start": _time_text(old["start_time"]), "old_end": _time_text(old["end_time"]),
                    "old_point": old["point"], "new_start": None if cell["delete"] else cell["start_time"],
                    "new_end": None if cell["delete"] else cell["end_time"],
                    "new_point": None if cell["delete"] else cell["point"],
                })
            if not cell["old_rows"] and not cell["delete"]:
                connection.execute(text("""
                    INSERT INTO web_shift_change_log (
                        actor_user_id, actor_name, employee_id, employee_name, shift_date, action,
                        new_start_time, new_end_time, new_point
                    ) VALUES (:actor_id, :actor_name, :employee_id, :employee_name, :shift_date, 'created',
                              :new_start, :new_end, :new_point)
                """), {
                    "actor_id": actor_id, "actor_name": actor["name"],
                    "employee_id": cell["employee"]["id"], "employee_name": cell["employee"]["name"],
                    "shift_date": cell["date"], "new_start": cell["start_time"],
                    "new_end": cell["end_time"], "new_point": cell["point"],
                })

        for cell in changes:
            if cell["old_rows"]:
                connection.execute(text("""
                    DELETE FROM schedule WHERE shift_date=:date AND iiko_id=:iiko_id
                """), {"date": cell["date"], "iiko_id": cell["iiko_id"]})

        changed_count = 0
        cleared_count = 0
        for cell in changes:
            if cell["delete"]:
                cleared_count += len(cell["old_rows"])
                continue
            shift_type_id = _shift_type_id(connection, cell["start_time"], cell["end_time"], cell["point"])
            connection.execute(text("""
                INSERT INTO schedule (shift_date, iiko_id, shift_type_id, created_at, updated_at, source, version, is_active)
                VALUES (:date, :iiko_id, :shift_type_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'manual', 1, 1)
            """), {"date": cell["date"], "iiko_id": cell["iiko_id"], "shift_type_id": shift_type_id})
            changed_count += 1

        # «Расписание»: с публикацией — снимки всего диапазона; без публикации — тихо, без лога.
        snapshots: list[dict[str, Any]] = []
        period_start: str | None = None
        period_end: str | None = None
        if change_type == "schedule" and publish:
            if publish_from and publish_to:
                window_start, window_end = publish_from, publish_to
            else:
                # Без явного диапазона берём дни, которые были изменены.
                changed_dates = sorted(cell["date"] for cell in changes)
                window_start, window_end = _to_date(changed_dates[0]), _to_date(changed_dates[-1])
            if window_end < window_start:
                window_start, window_end = window_end, window_start
            period_start, period_end = window_start.isoformat(), window_end.isoformat()
            # Снимки — по всем активным бариста, а не только по изменённым строкам.
            employee_ids = active_employee_ids(connection)
            for shot in render_schedule_snapshots(connection, employee_ids, window_start, window_end):
                # В канал анонсов уходит только «График смен» и диапазон дат.
                snapshots.append({
                    **shot,
                    "caption": f"График смен · {_date_label(shot['start'])} – {_date_label(shot['end'])}",
                })
            if snapshots:
                connection.execute(text("""
                    INSERT INTO web_shift_change_log (
                        actor_user_id, actor_name, employee_id, employee_name, shift_date, action,
                        change_type, snapshot_path, snapshot_paths, period_start, period_end, changes_count
                    ) VALUES (
                        :actor_id, :actor_name, NULL, :employee_name, :period_start, 'published',
                        'schedule', :snapshot, :snapshot_list, :period_start, :period_end, :changes_count
                    )
                """), {
                    "actor_id": actor_id, "actor_name": actor["name"],
                    "employee_name": f"Расписание · {_date_label(period_start)} – {_date_label(period_end)}",
                    "period_start": period_start, "period_end": period_end,
                    "snapshot": snapshots[0]["url"],
                    "snapshot_list": json.dumps([shot["url"] for shot in snapshots], ensure_ascii=False),
                    "changes_count": len(changes),
                })
        if change_type == "schedule":
            result = {
                "changed": changed_count, "cleared": cleared_count, "change_type": "schedule",
                "logged": 1 if snapshots else 0,
                "published": bool(snapshots), "snapshots": snapshots,
                "period_start": period_start, "period_end": period_end,
                "changes_count": len(changes),
            }
        else:
            result = {
                "changed": changed_count, "cleared": cleared_count, "change_type": "swap",
                "logged": len(changes), "swap_lines": _swap_lines(changes),
            }

    if result.get("published"):
        prune_snapshots(engine)
    return result


def get_shift_history(engine: Engine, year: int, month: int, limit: int = 100) -> list[dict[str, Any]]:
    first_day = date(year, month, 1).isoformat()
    last_day = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT id, actor_name, employee_name, shift_date, action,
                   old_start_time, old_end_time, old_point, new_start_time, new_end_time, new_point,
                   change_type, snapshot_path, snapshot_paths, period_start, period_end, changes_count, created_at
            FROM web_shift_change_log
            WHERE shift_date >= :first_day AND shift_date <= :last_day
            ORDER BY created_at DESC, id DESC LIMIT :limit
        """), {"first_day": first_day, "last_day": last_day, "limit": limit}).mappings().all()
    return [dict(row) for row in rows]


def get_user_preferences(engine: Engine, user_id: int) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT quality_enabled, calendar_enabled, theme, accent, chat_notify
            FROM web_user_preferences WHERE user_id=:user_id
        """), {"user_id": user_id}).mappings().first()
    theme = row["theme"] if row and row["theme"] in THEME_VALUES else "system"
    accent = row["accent"] if row and _HEX_COLOR.match(row["accent"] or "") else DEFAULT_ACCENT
    chat_notify = row["chat_notify"] if row and row["chat_notify"] in CHAT_NOTIFY_VALUES else DEFAULT_CHAT_NOTIFY
    return {
        "quality_enabled": bool(row["quality_enabled"]) if row else True,
        "calendar_enabled": bool(row["calendar_enabled"]) if row else True,
        "theme": theme,
        "accent": accent,
        "chat_notify": chat_notify,
    }


def save_user_preferences(engine: Engine, user_id: int, values: dict[str, Any]) -> dict[str, Any]:
    current = get_user_preferences(engine, user_id)
    updated = {**current, **values}
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO web_user_preferences
                (user_id, quality_enabled, calendar_enabled, theme, accent, chat_notify, updated_at)
            VALUES (:user_id, :quality, :calendar, :theme, :accent, :chat_notify, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET quality_enabled=excluded.quality_enabled,
                calendar_enabled=excluded.calendar_enabled, theme=excluded.theme,
                accent=excluded.accent, chat_notify=excluded.chat_notify,
                updated_at=CURRENT_TIMESTAMP
        """), {"user_id": user_id, "quality": int(updated["quality_enabled"]),
               "calendar": int(updated["calendar_enabled"]),
               "theme": updated["theme"], "accent": updated["accent"],
               "chat_notify": updated.get("chat_notify", DEFAULT_CHAT_NOTIFY)})
    return updated


def build_calendar_feed(engine: Engine, user_id: int, user_timezone: ZoneInfo) -> str:
    with engine.connect() as connection:
        user = connection.execute(text("""
            SELECT id, name, display_name, iiko_id FROM users WHERE id = :user_id AND is_active = 1
              AND role IN ('barista','senior','mentor')
        """), {"user_id": user_id}).mappings().first()
        if not user or user["iiko_id"] is None:
            return "\r\n".join([
                "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Coffee Quality//Shift Calendar//RU",
                "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
                "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
                "X-PUBLISHED-TTL:PT1H",
                "END:VCALENDAR",
            ]) + "\r\n"
        shifts = connection.execute(text("""
            SELECT s.shift_id, s.shift_date, st.start_time, st.end_time, st.point
            FROM schedule s JOIN shift_types st ON st.id = s.shift_type_id
            WHERE s.iiko_id = :iiko_id AND COALESCE(s.is_active,1) = 1
            ORDER BY s.shift_date, st.start_time
        """), {"iiko_id": str(user["iiko_id"])}).mappings().all()
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Coffee Quality//Shift Calendar//RU",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        # Подсказка клиенту, как часто перечитывать фид (RFC 7986 + X-PUBLISHED-TTL).
        # Отдельная синхронизация не нужна: фид собирается из БД на каждый запрос.
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
        f"X-WR-CALNAME:Смены — {user['display_name'] or user['name']}",
    ]
    now = datetime.now(user_timezone).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for shift in shifts:
        start, end = _shift_bounds(shift["shift_date"], shift["start_time"], shift["end_time"], user_timezone)
        start_utc = start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        end_utc = end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:coffee-shift-{shift['shift_id']}@coffee-quality.local",
            f"DTSTAMP:{now}",
            f"DTSTART:{start_utc}",
            f"DTEND:{end_utc}",
            f"SUMMARY:Смена {shift['point']} — НЕФТЬ",
            f"LOCATION:{shift['point']}",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"