"""Расчёт часов, планируемой нагрузки и кадровой насыщенности («Расчет часов»).

Модуль отвечает за три вещи:

1. **Производственный календарь.** Рабочие и нерабочие дни по Татарстану.
   Источник — сервис isdayoff.ru (`region=ru-ta`), ответы кэшируются в таблице
   `web_calendar_days`. Наружу сервер ходит не всегда (в проде недоступен даже
   api.telegram.org), поэтому при недоступности сервиса используется встроенный
   офлайн-набор: выходные Сб/Вс, федеральные праздники РФ и региональные
   праздники РТ (Ураза-байрам, Курбан-байрам, 30 августа, 6 ноября).
   Региональные праздники РТ размечаются принудительно, даже когда isdayoff
   ответил: если такой день выпадает на будень, он становится нерабочим.

2. **Нормо-часы сотрудников (x/y/z).** Хранятся прямо в таблице `users`
   (`min_hours` / `opt_hours` / `max_hours`), правит сам сотрудник в профиле
   или наставник в «Команда → Доступ и роли».

3. **Необходимые часы по точкам УЯ и ДЕ.** Считаются из шаблонов смен
   (`web_shift_templates`) с флагами `is_morning` / `is_evening` /
   `is_overlap` / `is_weekend`. Результат — средние длительности смен по
   категориям; итоговые суммы пересчитывает клиент, чтобы ползунок «n%
   пересменов» двигал цифры без обращения к серверу.

Сетевые запросы — только через стандартный `urllib` (как в
`avatars.py`/`telegram_publish.py`): лишних зависимостей проект не тянет.
"""

import calendar as _calendar
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

ROLE_NAMES = {"newbie": "Стажёр", "barista": "Бариста", "senior": "Старший", "mentor": "Наставник"}
WORK_ROLES = ("newbie", "barista", "senior", "mentor")
POINTS = ("УЯ", "ДЕ")

# Нормо-часы: значения по умолчанию и границы валидации.
DEFAULT_MIN_HOURS = 80
DEFAULT_OPT_HOURS = 130
DEFAULT_MAX_HOURS = 170
MAX_HOURS_LIMIT = 744  # 31 день × 24 часа — больше физически не проставить.

# Индикатор насыщенности: доля от оптимальной планки.
SATURATION_DEFICIT = 0.9
SATURATION_OVERLOAD = 1.1

# Прогноз часов на конец месяца считается, только если проставлено достаточно:
# по одному опубликованному дню экстраполяция даёт шум (40 ч × 30 = 1200 ч).
FORECAST_MIN_DAYS = 5
FORECAST_MIN_COVERAGE = 0.15

ISDAYOFF_URL = "https://isdayoff.ru/api/getdata"
ISDAYOFF_REGION = "ru-ta"
ISDAYOFF_TIMEOUT = 6

# Федеральные праздники РФ (месяц, день) — база офлайн-календаря.
RU_FIXED_HOLIDAYS = (
    (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 7), (1, 8),
    (2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4),
)
# Региональные праздники Татарстана с фиксированной датой.
RT_FIXED_HOLIDAYS = (
    (8, 30),  # День Республики Татарстан
    (11, 6),  # День Конституции Республики Татарстан
)
# Религиозные праздники РТ: даты меняются год от года (григорианский пересчёт
# исламского календаря). Таблица для офлайна; при наличии сети isdayoff даёт
# точные значения, а мы лишь принудительно доразмечаем будни.
RT_MOVABLE_HOLIDAYS = {
    2024: ("2024-04-10", "2024-06-16"),  # Ураза-байрам, Курбан-байрам
    2025: ("2025-03-30", "2025-06-07"),
    2026: ("2026-03-20", "2026-05-27"),
    2027: ("2027-03-10", "2027-05-17"),
    2028: ("2028-02-27", "2028-05-05"),
    2029: ("2029-02-15", "2029-04-24"),
    2030: ("2030-02-05", "2030-04-14"),
}


def _isdayoff_enabled() -> bool:
    """Флаг отключения сети: `ISDAYOFF_ENABLED=0` оставляет только офлайн-набор."""
    return (os.getenv("ISDAYOFF_ENABLED", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}


def initialize_schema(engine: Engine) -> None:
    """Таблица-кэш производственного календаря."""
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_calendar_days (
                day TEXT PRIMARY KEY,
                is_working INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'fallback',
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_web_calendar_days_working
            ON web_calendar_days (is_working)
        """))


# --------------------------------------------------------------------------- #
# Часы смен
# --------------------------------------------------------------------------- #

def _minutes(value: Any) -> int | None:
    """«HH:MM» (или time) → минуты от начала суток."""
    text_value = str(value or "").strip()
    if not text_value:
        return None
    parts = text_value.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def shift_hours(start: Any, end: Any) -> float:
    """Длительность смены в часах. Ночная смена (конец раньше начала) — +24 ч."""
    start_minutes = _minutes(start)
    end_minutes = _minutes(end)
    if start_minutes is None or end_minutes is None:
        return 0.0
    if end_minutes <= start_minutes:
        end_minutes += 24 * 60
    return (end_minutes - start_minutes) / 60


# --------------------------------------------------------------------------- #
# Производственный календарь
# --------------------------------------------------------------------------- #

def _month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, _calendar.monthrange(year, month)[1])


def _rt_holidays(year: int) -> set[date]:
    """Региональные праздники РТ: фиксированные плюс религиозные (если известны)."""
    days = {date(year, month, day) for month, day in RT_FIXED_HOLIDAYS}
    for value in RT_MOVABLE_HOLIDAYS.get(year, ()):
        try:
            days.add(date.fromisoformat(value))
        except ValueError:
            continue
    return days


def _offline_month(year: int, month: int) -> dict[date, bool]:
    """Офлайн-календарь: выходные + праздники РФ и РТ + перенос выходных.

    Перенос приближен федеральному правилу: если фиксированный праздник
    выпал на субботу/воскресенье, следующий рабочий день становится выходным.
    """
    first, last = _month_bounds(year, month)
    result: dict[date, bool] = {}
    day = first
    while day <= last:
        weekend = day.weekday() >= 5
        holiday = (day.month, day.day) in RU_FIXED_HOLIDAYS or (day.month, day.day) in RT_FIXED_HOLIDAYS
        result[day] = not (weekend or holiday)
        day += timedelta(days=1)

    fixed_weekend_holidays = [
        day for day, working in result.items()
        if not working and day.weekday() >= 5
        and ((day.month, day.day) in RU_FIXED_HOLIDAYS or (day.month, day.day) in RT_FIXED_HOLIDAYS)
    ]
    for holiday in fixed_weekend_holidays:
        shift_day = holiday + timedelta(days=1)
        while shift_day in result and not result[shift_day]:
            shift_day += timedelta(days=1)
        if shift_day in result:
            result[shift_day] = False
    return result


def _fetch_isdayoff(year: int, month: int) -> dict[date, bool] | None:
    """Календарь месяца из isdayoff.ru. None — сервис недоступен или ответ битый."""
    if not _isdayoff_enabled():
        return None
    query = urllib.parse.urlencode({"year": year, "month": month, "region": ISDAYOFF_REGION})
    request = urllib.request.Request(
        f"{ISDAYOFF_URL}?{query}",
        headers={"User-Agent": "NeftCrewDesk/1.0", "Accept": "text/plain"},
    )
    try:
        with urllib.request.urlopen(request, timeout=ISDAYOFF_TIMEOUT) as response:
            payload = response.read().decode("ascii", "ignore").strip()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    _, last = _month_bounds(year, month)
    if len(payload) != last.day or any(character not in "0124" for character in payload):
        # Коды ошибок («100», «199», «999») и несовпадение длины — доверять нельзя.
        return None
    # 0 — рабочий, 2 — сокращённый рабочий, 1 — нерабочий, 4 — перенесённый.
    return {date(year, month, index + 1): character in "02" for index, character in enumerate(payload)}


def _cached_month(engine: Engine, year: int, month: int) -> dict[date, dict[str, Any]]:
    first, last = _month_bounds(year, month)
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT day, is_working, source FROM web_calendar_days
            WHERE day >= :first AND day <= :last
        """), {"first": first.isoformat(), "last": last.isoformat()}).mappings().all()
    return {
        date.fromisoformat(str(row["day"])[:10]): {
            "is_working": bool(row["is_working"]),
            "source": row["source"],
        }
        for row in rows
    }


def _store_month(engine: Engine, days: dict[date, dict[str, Any]]) -> None:
    if not days:
        return
    with engine.begin() as connection:
        for day, info in days.items():
            connection.execute(text("""
                INSERT INTO web_calendar_days (day, is_working, source, updated_at)
                VALUES (:day, :is_working, :source, CURRENT_TIMESTAMP)
                ON CONFLICT(day) DO UPDATE SET
                    is_working = excluded.is_working,
                    source = excluded.source,
                    updated_at = CURRENT_TIMESTAMP
            """), {
                "day": day.isoformat(),
                "is_working": 1 if info["is_working"] else 0,
                "source": info["source"],
            })


def get_production_calendar(engine: Engine, year: int, month: int, refresh: bool = False) -> dict[str, Any]:
    """Производственный календарь месяца: W рабочих, H нерабочих, N всего."""
    first, last = _month_bounds(year, month)
    cached = {} if refresh else _cached_month(engine, year, month)
    needed = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
    days: dict[date, dict[str, Any]] = {
        day: {"is_working": info["is_working"], "source": info["source"]}
        for day, info in cached.items() if day in set(needed)
    }

    if any(day not in days for day in needed):
        external = _fetch_isdayoff(year, month)
        base_source = "isdayoff" if external else "fallback"
        if external is None:
            external = _offline_month(year, month)
        for day in needed:
            days[day] = {"is_working": bool(external.get(day, day.weekday() < 5)), "source": base_source}

    # Региональные праздники РТ важнее любого источника: наставник ждёт, что
    # Ураза-байрам или 6 ноября в будень окажутся закрытыми.
    for holiday in _rt_holidays(year):
        if holiday in days and holiday.weekday() < 5 and days[holiday]["is_working"]:
            days[holiday] = {"is_working": False, "source": "rt"}

    _store_month(engine, days)

    ordered = [days[day] for day in needed]
    working_days = sum(1 for info in ordered if info["is_working"])
    total_days = len(ordered)
    sources = {info["source"] for info in ordered}
    return {
        "year": year,
        "month": month,
        "working_days": working_days,
        "holidays": total_days - working_days,
        "total_days": total_days,
        "first_weekday": first.weekday(),
        "days": [
            {
                "date": day.isoformat(),
                "weekday": day.weekday(),
                "is_working": info["is_working"],
                "source": info["source"],
            }
            for day, info in zip(needed, ordered)
        ],
        "source": next(iter(sources)) if len(sources) == 1 else "mixed",
    }


# --------------------------------------------------------------------------- #
# Шаблоны смен → средние длительности по точкам
# --------------------------------------------------------------------------- #

def _template_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT point, start_time, end_time, is_morning, is_evening, is_overlap, is_weekend
            FROM web_shift_templates
            ORDER BY order_index, id
        """)).mappings().all()
    return [dict(row) for row in rows]


def _mean_hours(rows: Iterable[dict[str, Any]]) -> float:
    durations = [shift_hours(row["start_time"], row["end_time"]) for row in rows]
    durations = [value for value in durations if value > 0]
    if not durations:
        return 0.0
    return round(sum(durations) / len(durations), 2)


def _point_means(rows: list[dict[str, Any]], point: str) -> dict[str, Any]:
    """Средние длительности смен точки по категориям флагов."""
    templates = [row for row in rows if row["point"] == point]
    morning_weekday = [row for row in templates if row["is_morning"] and not row["is_weekend"]]
    morning_weekend = [row for row in templates if row["is_morning"] and row["is_weekend"]]
    overlap = [row for row in templates if row["is_overlap"]]
    evening = [row for row in templates if row["is_evening"]]

    weekend_hours = _mean_hours(morning_weekend)
    weekend_fallback = False
    if not weekend_hours and morning_weekday:
        # Отдельных выходных-шаблонов нет — берём будничное утро, чтобы карточка
        # не оставалась пустой (в графике выходные часто закрывают тем же утром).
        weekend_hours = _mean_hours(morning_weekday)
        weekend_fallback = True

    return {
        "morning_weekday_hours": _mean_hours(morning_weekday),
        "morning_weekend_hours": weekend_hours,
        "overlap_hours": _mean_hours(overlap),
        "evening_hours": _mean_hours(evening),
        "samples": {
            "morning_weekday": len(morning_weekday),
            "morning_weekend": len(morning_weekend),
            "overlap": len(overlap),
            "evening": len(evening),
        },
        "weekend_fallback": weekend_fallback,
    }


# --------------------------------------------------------------------------- #
# Факт по графику и нормо-часы сотрудников
# --------------------------------------------------------------------------- #

def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _month_schedule(engine: Engine, year: int, month: int, allowed_keys: set[str] | None = None) -> dict[str, Any]:
    """Смены месяца: часы по iiko_id и по дням (для темпа и прогноза).

    `allowed_keys` ограничивает выборку активными сотрудниками, чтобы часы в
    прогнозе складывались из тех же смен, что и «факт» в разрезе людей.
    """
    first, last = _month_bounds(year, month)
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT s.shift_date, s.iiko_id, st.start_time, st.end_time
            FROM schedule AS s
            JOIN shift_types AS st ON st.id = s.shift_type_id
            WHERE s.shift_date >= :first AND s.shift_date <= :last
              AND COALESCE(s.is_active, 1) = 1
        """), {"first": first.isoformat(), "last": last.isoformat()}).mappings().all()
    by_iiko: dict[str, float] = {}
    by_day: dict[date, float] = {}
    for row in rows:
        key = str(row["iiko_id"])
        if allowed_keys is not None and key not in allowed_keys:
            continue
        hours = shift_hours(row["start_time"], row["end_time"])
        by_iiko[key] = by_iiko.get(key, 0.0) + hours
        day = _as_date(row["shift_date"])
        by_day[day] = by_day.get(day, 0.0) + hours
    return {"by_iiko": by_iiko, "by_day": by_day}


def list_workload_users(engine: Engine) -> list[dict[str, Any]]:
    """Активные сотрудники с нормо-часами — источник для «Доступ и роли» и расчёта."""
    placeholders = ", ".join(f":role{index}" for index in range(len(WORK_ROLES)))
    params = {f"role{index}": role for index, role in enumerate(WORK_ROLES)}
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT id, COALESCE(display_name, name) AS name, iiko_id, role, avatar_rev,
                   min_hours, opt_hours, max_hours
            FROM users
            WHERE is_active = 1 AND role IN ({placeholders})
            ORDER BY COALESCE(display_name, name) COLLATE NOCASE
        """), params).mappings().all()
    return [dict(row) for row in rows]


def _level(ratio: float | None) -> str:
    if ratio is None:
        return "unknown"
    if ratio < SATURATION_DEFICIT:
        return "deficit"
    if ratio > SATURATION_OVERLOAD:
        return "overload"
    return "normal"


def _plan_forecast(day_hours: dict[date, float], year: int, month: int) -> dict[str, Any]:
    """Прогноз часов команды на конец месяца по средней нагрузке дня недели.

    График публикуется частями (середина и конец месяца), поэтому «факт» за
    неполный месяц сравнивать с нормо-часами нельзя — получается ложный дефицит.
    Берём проставленный период (1-е … последний день со сменами), считаем средние
    по дням недели и досчитываем оставшиеся дни этими средними. Так фиксированные
    дни работы сотрудников («по вторникам и субботам») учитываются сами собой,
    чего линейный темп по календарным дням не умеет.

    Состояние месяца: `complete` (график дотянут до конца), `partial` (прогноз
    имеет смысл), `empty` (графика нет или данных мало — индикатор уходит на
    потребность точек).
    """
    first, last = _month_bounds(year, month)
    total_days = last.day
    fact = round(sum(day_hours.values()), 1)
    days_with_shifts = sum(1 for hours in day_hours.values() if hours > 0)
    if not days_with_shifts:
        return {
            "state": "empty", "days_with_shifts": 0, "last_shift_day": None,
            "planned_days": 0, "coverage": 0.0, "factor": None,
            "projected_fact": 0.0, "weekday_average": None,
        }

    last_shift_day = max(day_hours)
    planned_days = last_shift_day.day
    coverage = planned_days / total_days
    # День без смен внутри проставленного периода — честный ноль, поэтому
    # средние считаем по всем вхождениям дня недели, а не только по занятым.
    sums: dict[int, float] = {}
    counts: dict[int, int] = {}
    day = first
    while day <= last_shift_day:
        weekday = day.weekday()
        sums[weekday] = sums.get(weekday, 0.0) + day_hours.get(day, 0.0)
        counts[weekday] = counts.get(weekday, 0) + 1
        day += timedelta(days=1)
    averages = {weekday: sums[weekday] / counts[weekday] for weekday in counts}
    weekday_average = {str(weekday): round(value, 2) for weekday, value in sorted(averages.items())}

    base = {
        "days_with_shifts": days_with_shifts,
        "last_shift_day": last_shift_day.day,
        "planned_days": planned_days,
        "coverage": round(coverage, 3),
        "weekday_average": weekday_average,
    }
    if last_shift_day == last:
        return {**base, "state": "complete", "factor": 1.0, "projected_fact": fact}
    if days_with_shifts < FORECAST_MIN_DAYS or coverage < FORECAST_MIN_COVERAGE:
        # Мало данных для прогноза: не выдумываем цифру, месяц считаем «пустым».
        return {**base, "state": "empty", "factor": None, "projected_fact": fact}

    fallback = fact / planned_days if planned_days else 0.0
    projected = fact
    day = last_shift_day + timedelta(days=1)
    while day <= last:
        projected += averages.get(day.weekday(), fallback)
        day += timedelta(days=1)
    projected = round(projected, 1)
    return {
        **base,
        "state": "partial",
        "factor": round(projected / fact, 3) if fact else None,
        "projected_fact": projected,
    }


def _saturation(plan: dict[str, Any], totals: dict[str, Any]) -> dict[str, Any]:
    """Индикатор насыщенности: база зависит от того, насколько проставлен месяц.

    Полный месяц — факт, частичный — прогноз. Для пустого месяца базу
    (`Σопт / потребность точек`) считает клиент: потребность зависит от
    ползунков «n% дней с пересменом».
    """
    state = plan.get("state")
    opt_total = totals.get("opt") or 0
    if state == "empty" or opt_total <= 0:
        return {"ratio": None, "level": "unknown", "basis": "norms", "basis_hours": None}
    partial = state == "partial"
    value = plan.get("projected_fact") if partial else totals.get("fact")
    value = value or 0
    ratio = value / opt_total
    return {
        "ratio": round(ratio, 3),
        "level": _level(ratio),
        "basis": "forecast" if partial else "fact",
        "basis_hours": round(value, 1),
    }


def get_workload_stats(engine: Engine, year: int, month: int, refresh: bool = False) -> dict[str, Any]:
    """Сводка «Расчета часов»: календарь, факт/план команды и средние смены по точкам."""
    production = get_production_calendar(engine, year, month, refresh=refresh)
    users = list_workload_users(engine)
    # Часы считаем только по активным сотрудникам: и «факт» в разрезе людей, и
    # прогноз должны опираться на одни и те же смены.
    employee_keys = {str(user["iiko_id"]) for user in users if user["iiko_id"] is not None}
    schedule = _month_schedule(engine, year, month, employee_keys)
    facts = schedule["by_iiko"]
    plan = _plan_forecast(schedule["by_day"], year, month)
    templates = _template_rows(engine)

    employees: list[dict[str, Any]] = []
    without_iiko = 0
    for user in users:
        key = None if user["iiko_id"] is None else str(user["iiko_id"])
        if key is None:
            without_iiko += 1
        employees.append({
            "id": user["id"],
            "name": user["name"],
            "iiko_id": user["iiko_id"],
            "role": user["role"],
            "role_name": ROLE_NAMES.get(user["role"], user["role"]),
            "avatar_rev": user["avatar_rev"] or 0,
            "min_hours": int(user["min_hours"] or DEFAULT_MIN_HOURS),
            "opt_hours": int(user["opt_hours"] or DEFAULT_OPT_HOURS),
            "max_hours": int(user["max_hours"] or DEFAULT_MAX_HOURS),
            "fact_hours": round(facts.get(key or "", 0.0), 1),
        })

    totals = {
        "fact": round(sum(item["fact_hours"] for item in employees), 1),
        "min": sum(item["min_hours"] for item in employees),
        "opt": sum(item["opt_hours"] for item in employees),
        "max": sum(item["max_hours"] for item in employees),
    }
    return {
        "year": year,
        "month": month,
        "calendar": production,
        "employees": employees,
        "employees_without_iiko": without_iiko,
        "totals": totals,
        "plan": plan,
        "saturation": _saturation(plan, totals),
        "points": {point: _point_means(templates, point) for point in POINTS},
    }


# --------------------------------------------------------------------------- #
# Правка нормо-часов
# --------------------------------------------------------------------------- #

def _coerce_hours(value: Any, label: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label}: ожидалось целое число часов")
    if isinstance(value, str):
        value = value.strip()
    try:
        number = int(float(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}: ожидалось целое число часов") from error
    if number < 0 or number > MAX_HOURS_LIMIT:
        raise ValueError(f"{label}: допустимо от 0 до {MAX_HOURS_LIMIT} часов")
    return number


def update_user_workload(engine: Engine, user_id: int, values: dict[str, Any]) -> dict[str, Any]:
    """Обновляет min/opt/max часы сотрудника с проверкой x ≤ y ≤ z."""
    if not isinstance(values, dict) or not values:
        raise ValueError("Ожидались нормо-часы для сохранения")
    allowed = {"min_hours", "opt_hours", "max_hours"}
    if set(values) - allowed:
        raise ValueError("Неизвестные поля нормо-часов")
    with engine.begin() as connection:
        current = connection.execute(text(
            "SELECT min_hours, opt_hours, max_hours FROM users WHERE id = :id"
        ), {"id": user_id}).mappings().first()
        if not current:
            raise LookupError("Пользователь не найден")
        merged = {
            "min_hours": int(current["min_hours"] or DEFAULT_MIN_HOURS),
            "opt_hours": int(current["opt_hours"] or DEFAULT_OPT_HOURS),
            "max_hours": int(current["max_hours"] or DEFAULT_MAX_HOURS),
        }
        for key, label in (("min_hours", "Минимум"), ("opt_hours", "Оптимум"), ("max_hours", "Максимум")):
            if key in values:
                merged[key] = _coerce_hours(values[key], label)
        if not merged["min_hours"] <= merged["opt_hours"] <= merged["max_hours"]:
            raise ValueError("Нужно, чтобы минимум ≤ оптимум ≤ максимум")
        connection.execute(text("""
            UPDATE users
            SET min_hours = :min_hours, opt_hours = :opt_hours,
                max_hours = :max_hours, updated_at = CURRENT_TIMESTAMP
            WHERE id = :id
        """), {**merged, "id": user_id})
    return {"id": user_id, **merged}


def get_user_workload(engine: Engine, user_id: int) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT min_hours, opt_hours, max_hours FROM users WHERE id = :id"
        ), {"id": user_id}).mappings().first()
    if not row:
        raise LookupError("Пользователь не найден")
    return {
        "min_hours": int(row["min_hours"] or DEFAULT_MIN_HOURS),
        "opt_hours": int(row["opt_hours"] or DEFAULT_OPT_HOURS),
        "max_hours": int(row["max_hours"] or DEFAULT_MAX_HOURS),
    }
