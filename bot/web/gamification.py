"""Геймификация: ветки навыков, опыт и уровни.

Ветки прокачки — «Сенсорика», «Молоко» и «Стандарты» (хард-скиллы): их сумма
двигает общий уровень аккаунта. Ветка «Команда» (чат, мемы, викторины) —
социальная: копит свой опыт параллельно и на бейдж уровня не влияет
(за неё в будущем выдаются награды вроде рамок аватара).

Опыт — иммутабельный лог (append-only): каждое начисление пишется в
`xp_transactions`, а `user_skills` — лишь свёрнутый баланс текущего состояния.
Понижение до стажёра или деактивация аккаунта опыт не обнуляют.

Уровень: `Level = floor(sqrt(XP / 100))`, считается целочисленно
(`math.isqrt` не накапливает ошибку плавающей арифметики): 2500 XP → 5 уровень.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

REPO_ROOT = Path(__file__).resolve().parents[2]

# Базовая выдача при повышении роли до barista/senior: 2500 XP = 5 уровень.
BASE_XP = 2500

# Хард-скиллы двигают общий уровень аккаунта; социальная ветка — нет.
HARD_SKILL_CODES = ("sensory", "milk", "standards")
SOCIAL_SKILL_CODES = ("team",)

# Конвертация оценки наставника в опыт: порог шага 0.5 (ниже 4.0 — 0 XP).
XP_RULES = ((5.0, 50), (4.5, 25), (4.0, 10))

# Каталог фото к оценкам (не в git): аналогично аватарам и загрузкам чата.
_PHOTOS_DIR: Path | None = None
MAX_PHOTO_BYTES = 8 * 1024 * 1024
PHOTO_MAX_SIDE = 1600
PHOTO_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.webp$")
PHOTO_URL_RE = re.compile(r"^/api/evaluations/photos/[0-9a-f]{32}\.webp$")

# Код ачивки: строчная латиница, цифры и подчёркивания. Совпадает с сидами и
# гарантирует, что code пригоден для JSON-payload правил и будущего рендера.
ACHIEVEMENT_CODE_RE = re.compile(r"^[a-z0-9_]{2,64}$")

# Известные типы триггеров ачивок: движок умеет вычислять только их.
ACHIEVEMENT_TRIGGERS = ("branch_xp_total", "evaluation_streak")

# Сиды веток: (код, название, описание). Порядок задаёт `position`.
# INSERT OR IGNORE: список наставник не должен чинить руками.
BRANCH_SEEDS = (
    ("sensory", "Сенсорика", "Аромат, вкус, чистота чашки — сенсорная культура"),
    ("milk", "Молоко", "Пар, текстура, температура и латте-арт"),
    ("standards", "Стандарты", "Рецептуры, гигиена, станция и тайминги"),
    ("team", "Команда", "Социальная ветка: чаты, мемы, викторины, помощь команде"),
)

# Сиды базовых ачивок для тестов движка. `rule_payload` — JSON-правило,
# которое разбирает `check_user_achievements`; `branch_id` — это код ветки
# (`skill_branches.code`), а не числовой id. INSERT OR IGNORE: повторный старт
# ничего не дублирует и не перезаписывает ручные правки наставника.
ACHIEVEMENT_SEEDS = (
    {
        "code": "sensory_geek",
        "name": "Сенсорный гик",
        "description": "2500 XP в ветке «Сенсорика»: вкус, аромат и чистота чашки на уровне чутья",
        "icon_model": "badge_sensory",
        "texture_color": "#c98a3c",
        "rule_payload": {"trigger_type": "branch_xp_total", "branch_id": "sensory", "threshold": 2500},
    },
    {
        "code": "latte_art_master",
        "name": "Мастер латте-арта",
        "description": "Три оценки подряд не ниже 4.5 в ветке «Молоко»",
        "icon_model": "badge_milk",
        "texture_color": "#e8d5b7",
        "rule_payload": {"trigger_type": "evaluation_streak", "branch_id": "milk",
                         "min_score": 4.5, "streak_count": 3},
    },
)


def photos_dir() -> Path:
    """Каталог фото оценок. Создаётся один раз за процесс."""
    global _PHOTOS_DIR
    if _PHOTOS_DIR is None:
        path = Path(os.getenv("NEFT_EVALUATION_PHOTOS_DIR") or (REPO_ROOT / "data" / "evaluations" / "photos"))
        path.mkdir(parents=True, exist_ok=True)
        _PHOTOS_DIR = path
    return _PHOTOS_DIR


def initialize_schema(engine: Engine) -> None:
    """Идемпотентно создаёт таблицы геймификации и наполняет ветки."""
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS skill_branches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT,
                position INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS user_skills (
                user_id INTEGER NOT NULL REFERENCES users(id),
                branch_id INTEGER NOT NULL REFERENCES skill_branches(id),
                xp INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, branch_id)
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS xp_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                branch_id INTEGER NOT NULL REFERENCES skill_branches(id),
                amount INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                source_id INTEGER,
                created_by INTEGER REFERENCES users(id),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evaluator_id INTEGER NOT NULL REFERENCES users(id),
                employee_id INTEGER NOT NULL REFERENCES users(id),
                raw_scores TEXT NOT NULL,
                photo_url TEXT,
                xp_awarded INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_evaluations_employee
            ON evaluations (employee_id, created_at)
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_xp_tx_user
            ON xp_transactions (user_id, branch_id, created_at)
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_user_skills_branch
            ON user_skills (branch_id)
        """))
        for position, (code, name, description) in enumerate(BRANCH_SEEDS):
            connection.execute(text("""
                INSERT OR IGNORE INTO skill_branches
                    (code, name, description, position, is_active)
                VALUES (:code, :name, :description, :position, 1)
            """), {"code": code, "name": name, "description": description,
                   "position": position})

        # Ачивки: справочник достижений и факт их выдачи сотрудникам. Правило
        # (`rule_payload`) храним как TEXT-JSON — SQLite не имеет JSON-типа,
        # как и у `evaluations.raw_scores`.
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT,
                icon_model TEXT,
                texture_color TEXT,
                rule_payload TEXT NOT NULL DEFAULT '{}',
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS user_achievements (
                user_id INTEGER NOT NULL REFERENCES users(id),
                achievement_id INTEGER NOT NULL REFERENCES achievements(id),
                unlocked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, achievement_id)
            )
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_user_achievements_achievement
            ON user_achievements (achievement_id)
        """))
        for seed in ACHIEVEMENT_SEEDS:
            connection.execute(text("""
                INSERT OR IGNORE INTO achievements
                    (code, name, description, icon_model, texture_color, rule_payload, is_active)
                VALUES (:code, :name, :description, :icon_model, :texture_color, :rule_payload, 1)
            """), {
                "code": seed["code"],
                "name": seed["name"],
                "description": seed["description"],
                "icon_model": seed["icon_model"],
                "texture_color": seed["texture_color"],
                "rule_payload": json.dumps(seed["rule_payload"], ensure_ascii=False),
            })


# --- Уровни ------------------------------------------------------------------ #


def level_for_xp(xp: int) -> int:
    """`floor(sqrt(XP / 100))` без плавающей арифметики."""
    return math.isqrt(int(xp or 0) // 100)


def _branch_progress(xp: int) -> dict[str, int | float]:
    """Сводка ветки: уровень, опыт внутри уровня и до следующего."""
    level = level_for_xp(xp)
    level_xp = xp - 100 * level * level
    level_span = 100 * ((level + 1) * (level + 1) - level * level)
    xp_to_next = 100 * (level + 1) * (level + 1) - xp
    progress = round(level_xp / level_span * 100, 1) if level_span else 0.0
    return {
        "level": level,
        "level_xp": level_xp,
        "level_span": level_span,
        "xp_to_next": xp_to_next,
        "progress": progress,
    }


def active_branches(connection: Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(text(
            "SELECT id, code, name, description FROM skill_branches "
            "WHERE is_active = 1 ORDER BY position, id"
        )).mappings().all()
    ]


# --- Начисление опыта -------------------------------------------------------- #


def grant_base_xp(connection: Connection, user_id: int, role: str) -> int:
    """Базовые 2500 XP во все активные ветки при роли barista/senior.

    Начисляется только тем веткам, где опыт меньше базового; каждый раз — одной
    `system`-транзакцией (идемпотентно при повторе). `newbie` и `guest` ничего
    не получают.
    """
    if role not in {"barista", "senior"}:
        return 0
    granted = 0
    current = {
        int(row["branch_id"]): int(row["xp"])
        for row in connection.execute(text(
            "SELECT branch_id, xp FROM user_skills WHERE user_id = :user_id"
        ), {"user_id": user_id}).mappings().all()
    }
    for branch in active_branches(connection):
        balance = current.get(int(branch["id"]), 0)
        delta = BASE_XP - balance
        if delta <= 0:
            continue
        connection.execute(text("""
            INSERT INTO xp_transactions (user_id, branch_id, amount, source_type, created_by)
            VALUES (:user_id, :branch_id, :amount, 'system', NULL)
        """), {"user_id": user_id, "branch_id": branch["id"], "amount": delta})
        connection.execute(text("""
            INSERT OR IGNORE INTO user_skills (user_id, branch_id, xp, updated_at)
            VALUES (:user_id, :branch_id, :xp, CURRENT_TIMESTAMP)
        """), {"user_id": user_id, "branch_id": branch["id"], "xp": balance})
        connection.execute(text("""
            UPDATE user_skills SET xp = xp + :delta, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = :user_id AND branch_id = :branch_id
        """), {"user_id": user_id, "branch_id": branch["id"], "delta": delta})
        granted += delta
    return granted


def backfill_base_xp(engine: Engine) -> int:
    """Одноразовая (идемпотентная) выдача базового XP активным barista/senior.

    Вызывается на старте: у существующей команды уровень появляется сразу,
    а не после первого действия. Стажёры и отключённые записи не трогаются.
    """
    total = 0
    with engine.begin() as connection:
        rows = connection.execute(text("""
            SELECT id, role FROM users WHERE is_active = 1 AND role IN ('barista', 'senior')
        """)).mappings().all()
        for row in rows:
            total += grant_base_xp(connection, int(row["id"]), row["role"])
    return total


# --- Оценки наставника -------------------------------------------------------- #


def xp_for_score(score: float) -> int:
    """5.0 → 50 XP, 4.5 → 25 XP, 4.0 → 10 XP, ниже 4.0 → 0 XP."""
    for threshold, xp in XP_RULES:
        if score >= threshold:
            return xp
    return 0


def apply_evaluation(connection: Connection, evaluator_id: int, employee_id: int,
                     scores: dict[str, float], photo_url: str | None) -> dict[str, Any]:
    """Конвертирует оценки в XP и пишет их в лог + баланс веток.

    Возвращает разбивку начислений по веткам и id сохранённой оценки.
    Запись `evaluations` и начисления идут в одной транзакции — частичных
    результатов не бывает.
    """
    awarded: dict[str, int] = {}
    for branch in active_branches(connection):
        score = scores.get(branch["code"])
        if score is None:
            continue
        awarded[branch["code"]] = xp_for_score(float(score))

    total = sum(awarded.values())
    result = connection.execute(text("""
        INSERT INTO evaluations (evaluator_id, employee_id, raw_scores, photo_url, xp_awarded)
        VALUES (:evaluator_id, :employee_id, :raw_scores, :photo_url, :xp_awarded)
    """), {
        "evaluator_id": evaluator_id,
        "employee_id": employee_id,
        "raw_scores": json.dumps(scores, ensure_ascii=False),
        "photo_url": photo_url,
        "xp_awarded": total,
    })
    evaluation_id = int(result.lastrowid)

    for branch in active_branches(connection):
        xp = awarded.get(branch["code"], 0)
        if xp <= 0:
            continue
        connection.execute(text("""
            INSERT INTO xp_transactions (user_id, branch_id, amount, source_type, source_id, created_by)
            VALUES (:user_id, :branch_id, :amount, 'evaluation', :source_id, :created_by)
        """), {"user_id": employee_id, "branch_id": branch["id"],
               "amount": xp, "source_id": evaluation_id, "created_by": evaluator_id})
        connection.execute(text("""
            UPDATE user_skills SET xp = xp + :amount, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = :user_id AND branch_id = :branch_id
        """), {"user_id": employee_id, "branch_id": branch["id"], "amount": xp})

    # Ачивки проверяем после начисления опыта и записи оценки: их правила видят
    # уже обновлённые `user_skills` и свежую строку в `evaluations`. Транзакция
    # та же, что у оценки, — частичной выдачи не бывает.
    unlocked = check_user_achievements(connection, employee_id, "evaluation")

    return {"ok": True, "id": evaluation_id, "xp_awarded": awarded,
            "total_xp": total, "achievements": unlocked}


# --- Достижения (ачивки) ------------------------------------------------------- #


def _branch_xp_for_code(connection: Connection, user_id: int, branch_code: str) -> int:
    """Опыт пользователя в ветке по её коду (0, если записи ещё нет)."""
    row = connection.execute(text("""
        SELECT us.xp
        FROM user_skills us
        JOIN skill_branches sb ON sb.id = us.branch_id
        WHERE us.user_id = :user_id AND sb.code = :code
    """), {"user_id": user_id, "code": branch_code}).first()
    return int(row[0]) if row else 0


def _evaluation_streak(connection: Connection, user_id: int, branch_code: str,
                       min_score: float) -> int:
    """Длина серии последних оценок, где score ветки не ниже `min_score`.

    Идём от свежих к старым: первая же оценка без ветки, с битым JSON или с
    оценкой ниже порога обрывает серию. Ровно то, что проверяет правило
    `evaluation_streak`, но в виде числа — нужно и для прогресса на витрине.
    """
    rows = connection.execute(text("""
        SELECT raw_scores FROM evaluations
        WHERE employee_id = :user_id
        ORDER BY created_at DESC, id DESC
    """), {"user_id": user_id}).scalars().all()
    streak = 0
    for raw in rows:
        try:
            scores = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            break
        value = scores.get(branch_code) if isinstance(scores, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            break
        if float(value) < min_score:
            break
        streak += 1
    return streak


def _rule_satisfied(connection: Connection, user_id: int, rule: dict[str, Any]) -> bool:
    """Выполнено ли правило ачивки. Неизвестный/битый триггер → False."""
    trigger = rule.get("trigger_type")
    branch_code = rule.get("branch_id")
    if not isinstance(branch_code, str) or not branch_code:
        return False
    if trigger == "branch_xp_total":
        threshold = rule.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            return False
        return _branch_xp_for_code(connection, user_id, branch_code) >= float(threshold)
    if trigger == "evaluation_streak":
        min_score = rule.get("min_score")
        streak_count = rule.get("streak_count")
        if (isinstance(min_score, bool) or not isinstance(min_score, (int, float))
                or isinstance(streak_count, bool) or not isinstance(streak_count, int)
                or streak_count <= 0):
            return False
        return _evaluation_streak(connection, user_id, branch_code, float(min_score)) >= streak_count
    return False


def check_user_achievements(connection: Connection, user_id: int,
                            event_context: str) -> list[dict[str, Any]]:
    """Выдаёт ачивки, правила которых выполнены, и возвращает их список.

    Берём только активные достижения, которых у пользователя ещё нет.
    `event_context` фиксирует источник проверки («evaluation» и т.п.) — задел
    для фильтрации правил по событию; сейчас проверяются все правила.
    """
    rows = connection.execute(text("""
        SELECT id, code, name, description, icon_model, texture_color, rule_payload
        FROM achievements
        WHERE is_active = 1
          AND id NOT IN (
              SELECT achievement_id FROM user_achievements WHERE user_id = :user_id
          )
        ORDER BY id
    """), {"user_id": user_id}).mappings().all()

    unlocked: list[dict[str, Any]] = []
    for row in rows:
        try:
            rule = json.loads(row["rule_payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(rule, dict) or not _rule_satisfied(connection, user_id, rule):
            continue
        connection.execute(text("""
            INSERT OR IGNORE INTO user_achievements (user_id, achievement_id)
            VALUES (:user_id, :achievement_id)
        """), {"user_id": user_id, "achievement_id": row["id"]})
        unlocked.append({
            "id": row["id"],
            "code": row["code"],
            "name": row["name"],
            "description": row["description"],
            "icon_model": row["icon_model"],
            "texture_color": row["texture_color"],
        })
    return unlocked


def _achievement_progress(connection: Connection, user_id: int,
                          rule: dict[str, Any]) -> tuple[int, int] | None:
    """Текущее значение и цель правила — для витрины «ближайших» ачивок."""
    trigger = rule.get("trigger_type")
    branch_code = rule.get("branch_id")
    if not isinstance(branch_code, str) or not branch_code:
        return None
    if trigger == "branch_xp_total":
        threshold = rule.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            return None
        return _branch_xp_for_code(connection, user_id, branch_code), int(threshold)
    if trigger == "evaluation_streak":
        min_score = rule.get("min_score")
        streak_count = rule.get("streak_count")
        if (isinstance(min_score, bool) or not isinstance(min_score, (int, float))
                or isinstance(streak_count, bool) or not isinstance(streak_count, int)
                or streak_count <= 0):
            return None
        return _evaluation_streak(connection, user_id, branch_code, float(min_score)), streak_count
    return None


def _achievement_entry(row: Any) -> dict[str, Any]:
    """Срез достижения для API: без сырого rule_payload."""
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "description": row["description"],
        "icon_model": row["icon_model"],
        "texture_color": row["texture_color"],
    }


def achievements_for_user(connection: Connection, user_id: int) -> dict[str, Any]:
    """Полученные ачивки и заблокированные с прогрессом (для дашборда)."""
    unlocked_rows = connection.execute(text("""
        SELECT a.id, a.code, a.name, a.description, a.icon_model, a.texture_color,
               ua.unlocked_at
        FROM user_achievements ua
        JOIN achievements a ON a.id = ua.achievement_id
        WHERE ua.user_id = :user_id
        ORDER BY ua.unlocked_at DESC, a.id DESC
    """), {"user_id": user_id}).mappings().all()
    unlocked = []
    for row in unlocked_rows:
        entry = _achievement_entry(row)
        entry["unlocked_at"] = row["unlocked_at"]
        unlocked.append(entry)

    locked_rows = connection.execute(text("""
        SELECT id, code, name, description, icon_model, texture_color, rule_payload
        FROM achievements
        WHERE is_active = 1
          AND id NOT IN (
              SELECT achievement_id FROM user_achievements WHERE user_id = :user_id
          )
        ORDER BY id
    """), {"user_id": user_id}).mappings().all()
    next_up = []
    for row in locked_rows:
        entry = _achievement_entry(row)
        try:
            rule = json.loads(row["rule_payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            rule = {}
        progress = _achievement_progress(connection, user_id, rule) if isinstance(rule, dict) else None
        if progress is not None:
            current, target = progress
            entry["progress"] = {
                "current": min(current, target),
                "target": target,
                "percent": round(current / target * 100, 1) if target else 0.0,
            }
        next_up.append(entry)

    return {"achievements": unlocked, "next_achievements": next_up}


def _parse_rule_payload(value: Any) -> dict[str, Any]:
    """Проверяет JSON-правило ачивки. Принимает dict или строку с JSON."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("rule_payload не является корректным JSON") from error
    if not isinstance(value, dict):
        raise ValueError("rule_payload должен быть JSON-объектом")
    trigger = value.get("trigger_type")
    if trigger not in ACHIEVEMENT_TRIGGERS:
        raise ValueError("Неизвестный trigger_type правила")
    branch_id = value.get("branch_id")
    if not isinstance(branch_id, str) or not branch_id:
        raise ValueError("В правиле не указан branch_id (код ветки)")
    if trigger == "branch_xp_total":
        threshold = value.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("Для branch_xp_total нужен числовой threshold")
    elif trigger == "evaluation_streak":
        min_score = value.get("min_score")
        streak_count = value.get("streak_count")
        if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
            raise ValueError("Для evaluation_streak нужен числовой min_score")
        if isinstance(streak_count, bool) or not isinstance(streak_count, int) or streak_count <= 0:
            raise ValueError("Для evaluation_streak нужен streak_count > 0")
    return value


def manage_achievements(engine: Engine) -> list[dict[str, Any]]:
    """Список всех ачивок (включая неактивные) для панели наставника."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT id, code, name, description, icon_model, texture_color,
                   rule_payload, is_active
            FROM achievements
            ORDER BY id
        """)).mappings().all()
    items = []
    for row in rows:
        try:
            rule = json.loads(row["rule_payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            rule = {}
        entry = _achievement_entry(row)
        entry["is_active"] = bool(row["is_active"])
        entry["rule_payload"] = rule
        items.append(entry)
    return items


def create_achievement(engine: Engine, payload: dict[str, Any]) -> dict[str, Any]:
    """Создаёт ачивку с сырым JSON-правилом. Ошибки — `ValueError` для API."""
    code = payload.get("code")
    name = payload.get("name")
    if not isinstance(code, str) or not ACHIEVEMENT_CODE_RE.match(code):
        raise ValueError("Код ачивки: строчные латинские буквы, цифры и подчёркивания")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise ValueError("Название ачивки обязательно (до 100 символов)")
    description = payload.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("Описание должно быть текстом")
    icon_model = payload.get("icon_model")
    texture_color = payload.get("texture_color")
    for label, value in (("icon_model", icon_model), ("texture_color", texture_color)):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{label} должен быть текстом")
    rule = _parse_rule_payload(payload.get("rule_payload"))
    is_active = payload.get("is_active", True)
    if not isinstance(is_active, bool):
        raise ValueError("is_active должен быть boolean")

    with engine.begin() as connection:
        if connection.execute(
            text("SELECT 1 FROM achievements WHERE code = :code"), {"code": code}
        ).first():
            raise ValueError("Ачивка с таким кодом уже существует")
        result = connection.execute(text("""
            INSERT INTO achievements
                (code, name, description, icon_model, texture_color, rule_payload, is_active)
            VALUES (:code, :name, :description, :icon_model, :texture_color, :rule_payload, :is_active)
        """), {
            "code": code,
            "name": name.strip(),
            "description": description,
            "icon_model": icon_model,
            "texture_color": texture_color,
            "rule_payload": json.dumps(rule, ensure_ascii=False),
            "is_active": 1 if is_active else 0,
        })
        achievement_id = int(result.lastrowid)

    entry = {
        "id": achievement_id,
        "code": code,
        "name": name.strip(),
        "description": description,
        "icon_model": icon_model,
        "texture_color": texture_color,
        "is_active": is_active,
        "rule_payload": rule,
    }
    return entry


# --- Дашборд развития ---------------------------------------------------------- #


def dashboard_me(engine: Engine, user_id: int) -> dict[str, Any]:
    """Прогресс сотрудника по веткам; ядро GET /api/dashboard/me."""
    with engine.connect() as connection:
        branches = active_branches(connection)
        role_row = connection.execute(
            text("SELECT role FROM users WHERE id = :user_id"), {"user_id": user_id}
        ).mappings().first()
        balances = {
            int(row[0]): int(row[1])
            for row in connection.execute(text(
                "SELECT branch_id, xp FROM user_skills WHERE user_id = :user_id"
            ), {"user_id": user_id})
        }
        recent_rows = connection.execute(text("""
            SELECT e.id, e.created_at, e.raw_scores, e.xp_awarded, e.photo_url,
                   COALESCE(u.display_name, u.name) AS evaluator
            FROM evaluations e
            JOIN users u ON u.id = e.evaluator_id
            WHERE e.employee_id = :user_id
            ORDER BY e.created_at DESC, e.id DESC LIMIT 5
        """), {"user_id": user_id}).mappings().all()
        # Полученные ачивки и ближайшие заблокированные — считаем в той же
        # сессии, пока открыто соединение.
        achievements = achievements_for_user(connection, user_id)

    user_branches = []
    hard_xp = 0
    social_xp = 0
    for branch in branches:
        xp = balances.get(int(branch["id"]), 0)
        code = branch["code"]
        kind = "social" if code in SOCIAL_SKILL_CODES else "hard"
        user_branches.append({
            "code": code,
            "name": branch["name"],
            "description": branch["description"],
            "kind": kind,
            "xp": xp,
            **_branch_progress(xp),
        })
        if kind == "hard":
            hard_xp += xp
        else:
            social_xp += xp

    branch_names = {branch["code"]: branch["name"] for branch in branches}
    recent = []
    for row in recent_rows:
        try:
            scores = json.loads(row["raw_scores"] or "{}")
        except (json.JSONDecodeError, TypeError):
            scores = {}
        recent.append({
            "id": row["id"],
            "created_at": row["created_at"],
            "scores": scores,
            # Готовые подписи для интерфейса: клиенту не нужно знать коды веток.
            "branches": [
                {"code": code, "name": branch_names.get(code, code), "score": score}
                for code, score in scores.items()
            ],
            "xp_awarded": row["xp_awarded"],
            "photo_url": row["photo_url"],
            "evaluator": row["evaluator"],
        })

    progress = _branch_progress(hard_xp)
    return {
        "role": role_row["role"] if role_row else "",
        "level": progress["level"],
        "account_xp": hard_xp,
        "account_xp_to_next": progress["xp_to_next"],
        "account_level_xp": progress["level_xp"],
        "account_level_span": progress["level_span"],
        "account_progress": progress["progress"],
        "social_xp": social_xp,
        "social_level": level_for_xp(social_xp),
        "branches": user_branches,
        "recent": recent,
        **achievements,
    }


# --- Фото к оценкам ------------------------------------------------------------ #


def store_photo(data: bytes) -> str | None:
    """Сжимает фото в WebP (Pillow). Возвращает URL или None, если не изображение."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            if max(image.size) > PHOTO_MAX_SIDE:
                image.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="WEBP", quality=82, method=4)
    except Exception:  # noqa: BLE001 — битый файл/не изображение → None
        return None
    name = f"{uuid.uuid4().hex}.webp"
    (photos_dir() / name).write_bytes(buffer.getvalue())
    return f"/api/evaluations/photos/{name}"


def photo_path(filename: str) -> Path:
    return photos_dir() / filename