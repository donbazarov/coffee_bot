"""Аналитика команды и качества для дашборда наставника.

Модуль — тонкий слой агрегаций поверх уже существующих данных: оценок
(`evaluations`, `raw_scores` — TEXT-JSON), справочников базы знаний
(`menu_drinks`) и графика смен (`schedule` + `shift_types`). Ничего не пишет —
только читает.

Три сводки, по одной на блок дашборда «Аналитика команды»:

1. **`team_overview`** — тепловая карта навыков: средний балл по веткам
   «Сенсорика», «Молоко», «Стандарты» для каждого активного сотрудника,
   дата последней оценки, число оценок и «коэффициент охвата вниманием
   наставников» (сколько оценок в среднем приходит на оцениваемого бариста).

2. **`drinks_quality`** — зоны риска: рейтинг напитков по среднему баллу,
   самые проседающие параметры (по убыванию проблемности) и чистота по точкам.

3. **`shifts_report`** — выработка часов: факт по графику в разрезе точек УЯ/ДЕ
   и связка с качеством оценок за тот же месяц.

JSON `raw_scores` парсится в Python, а не через SQLite-JSON1: так модуль
работает на любой сборке SQLite и придерживается того же подхода, что и
`gamification.py`.
"""

from __future__ import annotations

import calendar as _calendar
import json
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from bot.web import gamification, workload_service

# Ветки хард-скиллов образуют колонки тепловой карты (социальная «Команда» — нет).
HARD_BRANCH_CODES = gamification.HARD_SKILL_CODES

# Окно агрегации по умолчанию и его границы (дни).
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 365

# Целевое число оценок на одного сотрудника за 30 дней. Коэффициент охвата —
# отношение фактического среднего к цели: ниже — недосмотр, выше — перебор.
ATTENTION_TARGET = 4
ATTENTION_DEFICIT = 0.9
ATTENTION_OVERLOAD = 1.3

# Пороги «риска» напитка/параметра (по средней оценке).
RISK_MEDIUM = 4.0
RISK_LOW = 4.5


def _clamp_days(days: Any) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        value = DEFAULT_WINDOW_DAYS
    return max(1, min(value, MAX_WINDOW_DAYS))


def _parse_scores(raw: Any) -> dict[str, Any]:
    """Разбирает TEXT-JSON `raw_scores` в словарь (битый JSON → пустой)."""
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _number(value: Any) -> float | None:
    """float для числа, иначе None (bool не считается числом)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _risk_level(average: float | None) -> str:
    if average is None:
        return "unknown"
    if average < RISK_MEDIUM:
        return "high"
    if average < RISK_LOW:
        return "medium"
    return "low"


def _attention_level(index: float) -> str:
    if index <= 0:
        return "unknown"
    if index < ATTENTION_DEFICIT:
        return "deficit"
    if index > ATTENTION_OVERLOAD:
        return "overload"
    return "normal"


def _param_labels() -> dict[str, tuple[str, str | None]]:
    """Карта `код параметра → (подпись, ветка)` из спецификаций gamification."""
    labels: dict[str, tuple[str, str | None]] = {}
    specs = list(gamification.DRINK_PARAMS.values())
    specs.append(gamification.DEFAULT_DRINK_PARAMS)
    specs.append(gamification.CLEANLINESS_PARAMS)
    for spec in specs:
        for code, label, branch, _weight in spec:
            labels[code] = (label, branch)
    return labels


# --- Тепловая карта навыков команды ------------------------------------------ #


def team_overview(engine: Engine, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Сводка по сотрудникам: средние по веткам, активность и охват вниманием."""
    days = _clamp_days(days)
    window = f"-{days} days"
    branch_placeholders = ", ".join(f":b{index}" for index in range(len(HARD_BRANCH_CODES)))
    branch_params = {f"b{index}": code for index, code in enumerate(HARD_BRANCH_CODES)}
    role_placeholders = ", ".join(f":r{index}" for index in range(len(workload_service.WORK_ROLES)))
    role_params = {f"r{index}": role for index, role in enumerate(workload_service.WORK_ROLES)}

    with engine.connect() as connection:
        branch_rows = connection.execute(text(f"""
            SELECT code, name FROM skill_branches
            WHERE is_active = 1 AND code IN ({branch_placeholders})
            ORDER BY position, id
        """), branch_params).mappings().all()
        employees = connection.execute(text(f"""
            SELECT id, COALESCE(display_name, name) AS name, role
            FROM users
            WHERE is_active = 1 AND role IN ({role_placeholders})
            ORDER BY COALESCE(display_name, name) COLLATE NOCASE
        """), role_params).mappings().all()
        eval_rows = connection.execute(text("""
            SELECT employee_id, created_at, raw_scores
            FROM evaluations
            WHERE created_at >= datetime('now', :window)
            ORDER BY created_at
        """), {"window": window}).mappings().all()

    branches = [{"code": row["code"], "name": row["name"]} for row in branch_rows]
    branch_codes = {row["code"] for row in branch_rows}

    # employee_id → накопители по веткам и общий балл.
    stats: dict[int, dict[str, Any]] = {}
    for row in eval_rows:
        scores = _parse_scores(row["raw_scores"])
        entry = stats.setdefault(int(row["employee_id"]), {
            "branch_sum": {}, "branch_count": {},
            "overall_sum": 0.0, "overall_count": 0,
            "evaluations": 0, "last": None,
        })
        entry["evaluations"] += 1
        if row["created_at"]:
            entry["last"] = row["created_at"]
        for code in branch_codes:
            score = _number(scores.get(code))
            if score is not None:
                entry["branch_sum"][code] = entry["branch_sum"].get(code, 0.0) + score
                entry["branch_count"][code] = entry["branch_count"].get(code, 0) + 1
        overall = _number(scores.get("overall"))
        if overall is not None:
            entry["overall_sum"] += overall
            entry["overall_count"] += 1

    employees_out: list[dict[str, Any]] = []
    evaluated = 0
    total_evals = 0
    for employee in employees:
        data = stats.get(int(employee["id"]))
        averages: dict[str, float | None] = {}
        for code in branch_codes:
            count = data["branch_count"].get(code) if data else None
            averages[code] = round(data["branch_sum"][code] / count, 2) if count else None
        evaluations = int(data["evaluations"]) if data else 0
        overall = round(data["overall_sum"] / data["overall_count"], 2) if data and data["overall_count"] else None
        if evaluations:
            evaluated += 1
            total_evals += evaluations
        employees_out.append({
            "id": int(employee["id"]),
            "name": employee["name"],
            "role": employee["role"],
            "role_name": workload_service.ROLE_NAMES.get(employee["role"], employee["role"]),
            "evaluations": evaluations,
            "last_evaluated_at": data["last"] if data else None,
            "overall": overall,
            "averages": averages,
        })

    # Меньше всего внимания — вверх: интерфейсу проще подсветить отстающих.
    employees_out.sort(key=lambda item: (item["evaluations"], item["name"].lower()))

    total = len(employees_out)
    coverage = round(evaluated / total, 3) if total else 0.0
    target = max(1, round(ATTENTION_TARGET * days / 30))
    average_evals = round(total_evals / evaluated, 2) if evaluated else 0.0
    attention_index = round(average_evals / target, 3) if target else 0.0

    return {
        "window_days": days,
        "branches": branches,
        "team": {
            "total": total,
            "evaluated": evaluated,
            "evaluations": len(eval_rows),
            "coverage": coverage,
            "attention": {
                "index": attention_index,
                "level": _attention_level(attention_index),
                "target": target,
                "average": average_evals,
            },
        },
        "employees": employees_out,
    }

# --- Зоны риска по напиткам и чистота по точкам ------------------------------ #


def drinks_quality(engine: Engine, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Рейтинг напитков по среднему баллу, проблемные параметры и чистота точек."""
    days = _clamp_days(days)
    window = f"-{days} days"

    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT e.shift_point, e.evaluation_type, e.drink_id, e.drink_category,
                   e.raw_scores, d.title AS drink_title, d.category AS drink_category_ref
            FROM evaluations e
            LEFT JOIN menu_drinks d ON d.id = e.drink_id
            WHERE e.created_at >= datetime('now', :window)
        """), {"window": window}).mappings().all()

    labels = _param_labels()
    drinks: dict[int, dict[str, Any]] = {}
    params: dict[str, list[float]] = {}
    cleanliness: dict[str, list[float]] = {}

    for row in rows:
        scores = _parse_scores(row["raw_scores"])
        overall = _number(scores.get("overall"))

        if row["drink_id"] is not None:
            entry = drinks.setdefault(int(row["drink_id"]), {
                "id": int(row["drink_id"]),
                "title": row["drink_title"] or "—",
                "category": row["drink_category_ref"] or row["drink_category"] or "—",
                "evaluations": 0, "sum": 0.0, "count": 0,
            })
            entry["evaluations"] += 1
            if overall is not None:
                entry["sum"] += overall
                entry["count"] += 1

        if row["evaluation_type"] == "cleanliness" and row["shift_point"] and overall is not None:
            bucket = cleanliness.setdefault(row["shift_point"], [0.0, 0])
            bucket[0] += overall
            bucket[1] += 1

        raw_params = scores.get("params")
        if isinstance(raw_params, dict):
            for code, value in raw_params.items():
                score = _number(value)
                if score is None:
                    continue
                bucket = params.setdefault(code, [0.0, 0])
                bucket[0] += score
                bucket[1] += 1

    drink_list: list[dict[str, Any]] = []
    for entry in drinks.values():
        average = round(entry["sum"] / entry["count"], 2) if entry["count"] else None
        drink_list.append({
            "id": entry["id"],
            "title": entry["title"],
            "category": entry["category"],
            "evaluations": entry["evaluations"],
            "average": average,
            "risk": _risk_level(average),
        })
    # Проблемные — вверх: без оценки в конец, дальше по возрастанию балла.
    drink_list.sort(key=lambda item: (item["average"] is None, item["average"] if item["average"] is not None else 0.0))

    problem_params: list[dict[str, Any]] = []
    for code, (total_score, count) in params.items():
        if not count:
            continue
        label, branch = labels.get(code, (code, None))
        problem_params.append({
            "code": code,
            "label": label,
            "branch": branch,
            "average": round(total_score / count, 2),
            "evaluations": count,
        })
    problem_params.sort(key=lambda item: item["average"])

    cleanliness_list = [
        {"point": point, "average": round(total_score / count, 2), "evaluations": count}
        for point, (total_score, count) in cleanliness.items()
    ]
    cleanliness_list.sort(key=lambda item: item["point"])

    return {
        "window_days": days,
        "drinks": drink_list,
        "problem_params": problem_params[:10],
        "cleanliness_by_point": cleanliness_list,
    }


# --- Выработка часов и нагрузка на смене ------------------------------------- #


def shifts_report(engine: Engine, year: int, month: int) -> dict[str, Any]:
    """Факт часов по точкам УЯ/ДЕ за месяц, план и связка с качеством оценок.

    Отдельных нормо-часов «по точке» в схеме нет — нормы хранятся на сотруднике
    (`users.min/opt/max_hours`). Поэтому план команды распределяется между
    точками пропорционально их доле в фактической выработке; это отмечено в
    поле `note`, чтобы наставник не принял распределение за готовую норму.
    """
    stats = workload_service.get_workload_stats(engine, year, month)
    first = date(year, month, 1)
    last = date(year, month, _calendar.monthrange(year, month)[1])

    with engine.connect() as connection:
        fact_rows = connection.execute(text("""
            SELECT st.point AS point, st.start_time, st.end_time
            FROM schedule s JOIN shift_types st ON st.id = s.shift_type_id
            WHERE s.shift_date >= :first AND s.shift_date <= :last
              AND COALESCE(s.is_active, 1) = 1
        """), {"first": first.isoformat(), "last": last.isoformat()}).mappings().all()
        quality_rows = connection.execute(text("""
            SELECT shift_point, evaluation_type, raw_scores
            FROM evaluations
            WHERE shift_point IS NOT NULL
              AND strftime('%Y', created_at) = :year
              AND strftime('%m', created_at) = :month
        """), {"year": f"{year:04d}", "month": f"{month:02d}"}).mappings().all()

    fact: dict[str, dict[str, float]] = {}
    for row in fact_rows:
        point = row["point"] or "—"
        entry = fact.setdefault(point, {"hours": 0.0, "shifts": 0})
        entry["hours"] += workload_service.shift_hours(row["start_time"], row["end_time"])
        entry["shifts"] += 1

    quality: dict[str, dict[str, float]] = {}
    for row in quality_rows:
        scores = _parse_scores(row["raw_scores"])
        overall = _number(scores.get("overall"))
        entry = quality.setdefault(row["shift_point"], {
            "evaluations": 0, "overall_sum": 0.0, "overall_count": 0,
            "drink_sum": 0.0, "drink_count": 0, "clean_sum": 0.0, "clean_count": 0,
        })
        entry["evaluations"] += 1
        if overall is None:
            continue
        entry["overall_sum"] += overall
        entry["overall_count"] += 1
        if row["evaluation_type"] == "cleanliness":
            entry["clean_sum"] += overall
            entry["clean_count"] += 1
        else:
            entry["drink_sum"] += overall
            entry["drink_count"] += 1

    opt_total = float(stats["totals"].get("opt") or 0)
    fact_total = sum(entry["hours"] for entry in fact.values())

    points_out: list[dict[str, Any]] = []
    for point in workload_service.POINTS:
        entry = fact.get(point, {"hours": 0.0, "shifts": 0})
        share = (entry["hours"] / fact_total) if fact_total else 0.0
        plan_hours = round(opt_total * share, 1) if fact_total else 0.0
        hours = round(entry["hours"], 1)
        q = quality.get(point)
        points_out.append({
            "point": point,
            "fact_hours": hours,
            "plan_hours": plan_hours,
            "delta_hours": round(hours - plan_hours, 1),
            "share": round(share, 3),
            "shift_count": int(entry["shifts"]),
            "avg_shift_hours": round(entry["hours"] / entry["shifts"], 2) if entry["shifts"] else None,
            "quality": {
                "evaluations": int(q["evaluations"]) if q else 0,
                "average": round(q["overall_sum"] / q["overall_count"], 2) if q and q["overall_count"] else None,
                "drinks_average": round(q["drink_sum"] / q["drink_count"], 2) if q and q["drink_count"] else None,
                "cleanliness_average": round(q["clean_sum"] / q["clean_count"], 2) if q and q["clean_count"] else None,
            },
        })

    return {
        "year": year,
        "month": month,
        "note": "План в разрезе точек распределён пропорционально фактической выработке: отдельные нормо-часы по точкам не хранятся.",
        "points": points_out,
        "team": {
            "fact": stats["totals"].get("fact"),
            "min": stats["totals"].get("min"),
            "opt": stats["totals"].get("opt"),
            "max": stats["totals"].get("max"),
            "plan_state": stats["plan"].get("state"),
            "projected_fact": stats["plan"].get("projected_fact"),
            "saturation": stats["saturation"],
        },
    }


