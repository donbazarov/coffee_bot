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

# Сиды веток: (код, название, описание). Порядок задаёт `position`.
# INSERT OR IGNORE: список наставник не должен чинить руками.
BRANCH_SEEDS = (
    ("sensory", "Сенсорика", "Аромат, вкус, чистота чашки — сенсорная культура"),
    ("milk", "Молоко", "Пар, текстура, температура и латте-арт"),
    ("standards", "Стандарты", "Рецептуры, гигиена, станция и тайминги"),
    ("team", "Команда", "Социальная ветка: чаты, мемы, викторины, помощь команде"),
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

    return {"ok": True, "id": evaluation_id, "xp_awarded": awarded, "total_xp": total}


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