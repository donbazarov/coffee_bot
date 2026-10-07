import calendar
import os
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.engine import Engine

ROLE_NAMES = {"barista": "Бариста", "senior": "Старший", "mentor": "Наставник"}


def app_timezone() -> tzinfo:
    try:
        return ZoneInfo(os.getenv("WEB_TIMEZONE", "Europe/Moscow"))
    except ZoneInfoNotFoundError:
        return timezone.utc


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
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_web_shift_log_date
            ON web_shift_change_log (shift_date, created_at)
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_user_preferences (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                quality_enabled INTEGER NOT NULL DEFAULT 1,
                calendar_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))


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
                   st.point, st.shift_type, u.id AS user_id, u.name AS employee_name
            FROM schedule AS s
            JOIN shift_types AS st ON st.id = s.shift_type_id
            LEFT JOIN users AS u ON CAST(u.iiko_id AS TEXT) = s.iiko_id
            WHERE s.shift_date >= :first_day AND s.shift_date <= :last_day
              AND COALESCE(s.is_active, 1) = 1
            ORDER BY s.shift_date, st.start_time, u.name
        """), {"first_day": first_day.isoformat(), "last_day": last_day.isoformat()}).mappings().all()
        employees = connection.execute(text("""
            SELECT id, name, iiko_id, role FROM users
            WHERE is_active = 1 AND role IN ('barista', 'senior', 'mentor')
            ORDER BY name COLLATE NOCASE
        """)).mappings().all()
        current_user_id = user["id"]
        own_iiko_id = str(employee["iiko_id"]) if employee and employee["iiko_id"] is not None else None
        upcoming_rows = []
        if own_iiko_id:
            upcoming_rows = connection.execute(text("""
                SELECT s.shift_id, s.shift_date, s.iiko_id, st.start_time, st.end_time,
                       st.point, st.shift_type, u.id AS user_id, u.name AS employee_name
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
        "first_weekday": first_day.weekday(),
        "days_in_month": last_day.day,
        "shifts": shifts,
        "employees": [
            {"id": row["id"], "name": row["name"], "iiko_id": row["iiko_id"], "role": row["role"]}
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


def apply_schedule_changes(engine: Engine, actor_id: int, operations: list[dict[str, Any]]) -> dict[str, int]:
    if not operations or len(operations) > 500:
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
        actor = connection.execute(text("SELECT name FROM users WHERE id=:id AND is_active=1"), {"id": actor_id}).mappings().first()
        if not actor:
            raise LookupError("Пользователь не найден или отключён")

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
    return {"changed": changed_count, "cleared": cleared_count, "logged": len(changes)}


def get_shift_history(engine: Engine, year: int, month: int, limit: int = 100) -> list[dict[str, Any]]:
    first_day = date(year, month, 1).isoformat()
    last_day = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT id, actor_name, employee_name, shift_date, action,
                   old_start_time, old_end_time, old_point, new_start_time, new_end_time, new_point, created_at
            FROM web_shift_change_log
            WHERE shift_date >= :first_day AND shift_date <= :last_day
            ORDER BY created_at DESC, id DESC LIMIT :limit
        """), {"first_day": first_day, "last_day": last_day, "limit": limit}).mappings().all()
    return [dict(row) for row in rows]


def get_user_preferences(engine: Engine, user_id: int) -> dict[str, bool]:
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT quality_enabled, calendar_enabled FROM web_user_preferences WHERE user_id=:user_id
        """), {"user_id": user_id}).mappings().first()
    return {"quality_enabled": bool(row["quality_enabled"]) if row else True,
            "calendar_enabled": bool(row["calendar_enabled"]) if row else True}


def save_user_preferences(engine: Engine, user_id: int, values: dict[str, bool]) -> dict[str, bool]:
    current = get_user_preferences(engine, user_id)
    updated = {**current, **values}
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO web_user_preferences (user_id, quality_enabled, calendar_enabled, updated_at)
            VALUES (:user_id, :quality, :calendar, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET quality_enabled=excluded.quality_enabled,
                calendar_enabled=excluded.calendar_enabled, updated_at=CURRENT_TIMESTAMP
        """), {"user_id": user_id, "quality": int(updated["quality_enabled"]), "calendar": int(updated["calendar_enabled"])})
    return updated


def build_calendar_feed(engine: Engine, user_id: int, user_timezone: ZoneInfo) -> str:
    with engine.connect() as connection:
        user = connection.execute(text("""
            SELECT id, name, iiko_id FROM users WHERE id = :user_id AND is_active = 1
              AND role IN ('barista','senior','mentor')
        """), {"user_id": user_id}).mappings().first()
        if not user or user["iiko_id"] is None:
            return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Coffee Quality//Shift Calendar//RU\r\nEND:VCALENDAR\r\n"
        shifts = connection.execute(text("""
            SELECT s.shift_id, s.shift_date, st.start_time, st.end_time, st.point
            FROM schedule s JOIN shift_types st ON st.id = s.shift_type_id
            WHERE s.iiko_id = :iiko_id AND COALESCE(s.is_active,1) = 1
            ORDER BY s.shift_date, st.start_time
        """), {"iiko_id": str(user["iiko_id"])}).mappings().all()
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Coffee Quality//Shift Calendar//RU", "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:Смены — {user['name']}"]
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