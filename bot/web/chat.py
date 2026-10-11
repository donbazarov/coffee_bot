"""Чат команды: общий канал и личные сообщения.

Модуль изолирован: всё живёт в `APIRouter(prefix="/chat")`, схема создаётся
идемпотентно при старте (`initialize_schema`), поэтому `app.py` только
подключает роутер.

Что внутри:
* **Сообщения** — таблица `chat_messages` в той же базе. `recipient_id` = NULL
  означает общий чат, иначе — личное сообщение (пара «отправитель-получатель»).
* **Реальное время** — `ConnectionManager` в памяти процесса (1 воркер): при
  отключении клиента сессия удаляется сразу.
* **Изображения** — Pillow «на лету» из потока байт в WebP (ширина ≤ 1600px,
  quality=75), файлы кладутся в `data/chat/uploads` (env `NEFT_CHAT_UPLOADS_DIR`),
  в базу пишется только путь. Раздаются авторизованным пользователям роутом.
* **Push** — если у получателя нет живого WebSocket, шлём системное
  push-уведомление: для личных сообщений и упоминаний `@ник`. Настраивается
  персонально (`web_user_preferences.chat_notify`: all | dm_mentions | none).
"""

from __future__ import annotations

import io
import json
import logging
import os
import random
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from bot.database.models import engine
from bot.web import push
from bot.web.auth import COOKIE_NAME, read_session
from bot.web.calendar_service import get_user_preferences
from bot.web.deps import require_csrf, require_manager, require_manager_csrf, require_user

logger = logging.getLogger("bot.web.chat")

chat_router = APIRouter(prefix="/chat", tags=["chat"])
# Псевдоним, чтобы имя совпадало с ТЗ.
router = chat_router

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

MAX_TEXT = 2000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_WIDTH = 1600
WEBP_QUALITY = 75
DEFAULT_PAGE = 30
MAX_PAGE = 100
MAX_ROOM_NAME = 60
DEFAULT_ROOM_NAME = "Общий чат"
# Видимость темы: "all" — доступна всем (новые сотрудники попадают автоматически),
# "selected" — только перечисленным участникам.
ROOM_VISIBILITY = ("all", "selected")
MAX_POLL_OPTIONS = 12
MIN_POLL_OPTIONS = 2
MAX_POLL_QUESTION = 200
MAX_POLL_OPTION = 100
STAFF_ROLES = ("barista", "senior", "mentor")

# Путь картинки, который мы сами выдаём в /chat/upload. По нему же валидируем
# `image_url` из WebSocket, чтобы нельзя было подсунуть чужой/внешний URL.
IMAGE_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.webp$")
IMAGE_URL_RE = re.compile(r"^/chat/uploads/[0-9a-f]{32}\.webp$")
MENTION_RE = re.compile(r"@([\w.\-]{1,64})")

_uploads_dir: Path | None = None


def uploads_dir() -> Path:
    """Каталог картинок чата. Создаётся один раз за процесс."""
    global _uploads_dir
    if _uploads_dir is None:
        path = Path(os.getenv("NEFT_CHAT_UPLOADS_DIR") or (_REPO_ROOT / "data" / "chat" / "uploads"))
        path.mkdir(parents=True, exist_ok=True)
        _uploads_dir = path
    return _uploads_dir


def _ensure_column(connection: Any, table: str, column: str, ddl: str) -> None:
    """Идемпотентно добавляет колонку в существующую базу."""
    existing = {row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))}
    if column not in existing:
        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def initialize_schema(db: Engine) -> None:
    """Идемпотентно создаёт таблицы чата и переносит общий чат в тему.

    Раньше общий чат был один (`recipient_id IS NULL`). Теперь тем может быть
    много: у сообщения появляется `room_id`, а старые «общие» сообщения
    переносятся в тему по умолчанию, чтобы история не пропала.
    """
    with db.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_by INTEGER REFERENCES users(id),
                created_at DATETIME NOT NULL,
                order_index INTEGER NOT NULL DEFAULT 0
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL REFERENCES users(id),
                recipient_id INTEGER REFERENCES users(id),
                text TEXT,
                image_url TEXT,
                created_at DATETIME NOT NULL
            )
        """))
        _ensure_column(connection, "chat_messages", "room_id", "INTEGER")
        _ensure_column(connection, "chat_messages", "edited_at", "DATETIME")
        _ensure_column(connection, "chat_rooms", "visibility", "TEXT NOT NULL DEFAULT 'all'")
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_room_members (
                room_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (room_id, user_id)
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL REFERENCES users(id),
                question TEXT NOT NULL,
                anonymous INTEGER NOT NULL DEFAULT 0,
                multiple INTEGER NOT NULL DEFAULT 0,
                allow_change INTEGER NOT NULL DEFAULT 0,
                shuffle INTEGER NOT NULL DEFAULT 0,
                quiz INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_poll_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                order_index INTEGER NOT NULL DEFAULT 0,
                is_correct INTEGER NOT NULL DEFAULT 0
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_poll_votes (
                poll_id INTEGER NOT NULL,
                option_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                PRIMARY KEY (poll_id, option_id, user_id)
            )
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_chat_poll_msg ON chat_polls (message_id)
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_chat_recipient ON chat_messages (recipient_id, id)
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_chat_sender_recipient ON chat_messages (sender_id, recipient_id, id)
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_chat_room ON chat_messages (room_id, id)
        """))

        room_id = connection.execute(
            text("SELECT id FROM chat_rooms ORDER BY order_index, id LIMIT 1")
        ).scalar()
        if room_id is None:
            connection.execute(text("""
                INSERT INTO chat_rooms (name, created_at, order_index) VALUES (:name, :now, 0)
            """), {"name": DEFAULT_ROOM_NAME, "now": _utcnow_iso()})
            room_id = connection.execute(
                text("SELECT id FROM chat_rooms ORDER BY order_index, id LIMIT 1")
            ).scalar()
        # Старые сообщения общего чата уходят в тему по умолчанию.
        connection.execute(text("""
            UPDATE chat_messages SET room_id = :room WHERE recipient_id IS NULL AND room_id IS NULL
        """), {"room": int(room_id)})


def default_room_id() -> int:
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT id FROM chat_rooms ORDER BY order_index, id LIMIT 1")
        ).scalar()
    return int(value) if value is not None else 0


def _room_exists(room_id: int) -> bool:
    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT 1 FROM chat_rooms WHERE id = :id"), {"id": int(room_id)}
        ).first()
    return found is not None


def _room_name(room_id: Any) -> str:
    if room_id is None:
        return DEFAULT_ROOM_NAME
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT name FROM chat_rooms WHERE id = :id"), {"id": int(room_id)}
        ).scalar()
    return str(value) if value else DEFAULT_ROOM_NAME


def _room_member_ids(connection: Any, room_id: int) -> list[int]:
    rows = connection.execute(
        text("SELECT user_id FROM chat_room_members WHERE room_id = :id"), {"id": int(room_id)}
    ).scalars().all()
    return [int(value) for value in rows]


def _room_visibility(connection: Any, room_id: int) -> str:
    value = connection.execute(
        text("SELECT visibility FROM chat_rooms WHERE id = :id"), {"id": int(room_id)}
    ).scalar()
    return str(value or "all")


def _can_access_room(room_id: int, user_id: int) -> bool:
    """Тема доступна всем ("all") или только участникам ("selected")."""
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT visibility FROM chat_rooms WHERE id = :id"), {"id": int(room_id)}
        ).mappings().first()
        if not row:
            return False
        if str(row["visibility"] or "all") != "selected":
            return True
        member = connection.execute(text("""
            SELECT 1 FROM chat_room_members WHERE room_id = :room AND user_id = :user
        """), {"room": int(room_id), "user": int(user_id)}).first()
    return member is not None


def _room_recipients(room_id: int) -> set[int] | None:
    """Кому слать события темы. None — всем; иначе — только участникам."""
    with engine.connect() as connection:
        if _room_visibility(connection, room_id) != "selected":
            return None
        members = set(_room_member_ids(connection, room_id))
    return members


def _names_for(user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    statement = text(
        "SELECT id, COALESCE(display_name, name) AS name FROM users WHERE id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    with engine.connect() as connection:
        rows = connection.execute(statement, {"ids": [int(value) for value in user_ids]}).mappings().all()
    return {int(row["id"]): str(row["name"] or "Сотрудник") for row in rows}


# --- Опросы --------------------------------------------------------------- #


def _normalize_poll(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Проверяет и нормализует опрос из WebSocket. Возвращает (опрос, ошибка)."""
    if not isinstance(raw, dict):
        return None, "Некорректный опрос"
    question = " ".join(str(raw.get("question") or "").split())[:MAX_POLL_QUESTION]
    if not question:
        return None, "Введите вопрос"
    options = []
    for item in raw.get("options") or []:
        value = " ".join(str(item or "").split())[:MAX_POLL_OPTION]
        if value:
            options.append(value)
    if len(options) < MIN_POLL_OPTIONS:
        return None, f"Нужно минимум {MIN_POLL_OPTIONS} варианта ответа"
    options = options[:MAX_POLL_OPTIONS]

    quiz = bool(raw.get("quiz"))
    try:
        correct_index = int(raw.get("correct_index")) if raw.get("correct_index") is not None else None
    except (TypeError, ValueError):
        correct_index = None
    if quiz and (correct_index is None or not 0 <= correct_index < len(options)):
        return None, "Отметьте правильный вариант"

    entries = [
        {"text": value, "is_correct": bool(quiz and index == correct_index)}
        for index, value in enumerate(options)
    ]
    shuffle = bool(raw.get("shuffle"))
    if shuffle:
        random.shuffle(entries)
    return {
        "question": question,
        "options": entries,
        "anonymous": bool(raw.get("anonymous")),
        "multiple": bool(raw.get("multiple")),
        "allow_change": bool(raw.get("allow_change")),
        "shuffle": shuffle,
        "quiz": quiz,
    }, None


def _create_poll(connection: Any, message_id: int, author_id: int,
                 poll: dict[str, Any], created: str) -> int:
    result = connection.execute(text("""
        INSERT INTO chat_polls
            (message_id, author_id, question, anonymous, multiple, allow_change, shuffle, quiz, created_at)
        VALUES (:message, :author, :question, :anonymous, :multiple, :change, :shuffle, :quiz, :created)
    """), {
        "message": message_id, "author": author_id, "question": poll["question"],
        "anonymous": int(poll["anonymous"]), "multiple": int(poll["multiple"]),
        "change": int(poll["allow_change"]), "shuffle": int(poll["shuffle"]),
        "quiz": int(poll["quiz"]), "created": created,
    })
    poll_id = int(result.lastrowid)
    for index, option in enumerate(poll["options"]):
        connection.execute(text("""
            INSERT INTO chat_poll_options (poll_id, text, order_index, is_correct)
            VALUES (:poll, :text, :order, :correct)
        """), {"poll": poll_id, "text": option["text"], "order": index,
               "correct": int(option["is_correct"])})
    return poll_id


def _poll_payload(poll_id: int, viewer_id: int) -> dict[str, Any] | None:
    """Опрос глазами конкретного пользователя (свои голоса, скрытый ответ викторины)."""
    with engine.connect() as connection:
        poll = connection.execute(text("""
            SELECT id, message_id, author_id, question, anonymous, multiple, allow_change, shuffle, quiz
            FROM chat_polls WHERE id = :id
        """), {"id": int(poll_id)}).mappings().first()
        if not poll:
            return None
        options = connection.execute(text("""
            SELECT id, text, is_correct FROM chat_poll_options
            WHERE poll_id = :id ORDER BY order_index, id
        """), {"id": int(poll_id)}).mappings().all()
        votes = connection.execute(text("""
            SELECT option_id, user_id FROM chat_poll_votes WHERE poll_id = :id
        """), {"id": int(poll_id)}).mappings().all()

    counts: dict[int, int] = {}
    voters: dict[int, list[int]] = {}
    for vote in votes:
        option_id = int(vote["option_id"])
        counts[option_id] = counts.get(option_id, 0) + 1
        voters.setdefault(option_id, []).append(int(vote["user_id"]))

    everyone = {value for people in voters.values() for value in people}
    my_votes = [option_id for option_id, people in voters.items() if int(viewer_id) in people]
    anonymous = bool(poll["anonymous"])
    quiz = bool(poll["quiz"])
    reveal = (not quiz) or bool(my_votes) or int(poll["author_id"]) == int(viewer_id)
    names = {} if anonymous else _names_for(everyone)

    serialized = []
    for option in options:
        option_id = int(option["id"])
        serialized.append({
            "id": option_id,
            "text": option["text"],
            "votes": counts.get(option_id, 0),
            "is_correct": bool(option["is_correct"]) if reveal else None,
            "voters": [] if anonymous else [names.get(uid, "Сотрудник") for uid in voters.get(option_id, [])],
        })
    return {
        "id": int(poll["id"]),
        "question": poll["question"],
        "anonymous": anonymous,
        "multiple": bool(poll["multiple"]),
        "allow_change": bool(poll["allow_change"]),
        "shuffle": bool(poll["shuffle"]),
        "quiz": quiz,
        "voted": bool(my_votes),
        "my_votes": my_votes,
        "voters": len(everyone),
        "options": serialized,
    }


# --- Изображения ------------------------------------------------------------ #


def compress_image(data: bytes) -> bytes | None:
    """Сжимает картинку в WebP «на лету». None — если это не изображение."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            if image.width > MAX_WIDTH:
                ratio = MAX_WIDTH / float(image.width)
                height = max(1, round(image.height * ratio))
                image = image.resize((MAX_WIDTH, height), Image.LANCZOS)
            if image.mode in ("RGBA", "LA", "P"):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                image = background
            else:
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=4)
            return buffer.getvalue()
    except Exception as error:  # noqa: BLE001 — битый файл не должен ронять запрос
        logger.warning("Чат: изображение не распознано: %s", error)
        return None


def _utcnow_iso() -> str:
    """ISO-8601 с 'Z': браузер разбирает такую строку как UTC без догадок."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- Хранение --------------------------------------------------------------- #


def _row_to_message(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "sender_id": int(row["sender_id"]),
        "recipient_id": int(row["recipient_id"]) if row["recipient_id"] is not None else None,
        "room_id": int(row["room_id"]) if row["room_id"] is not None else None,
        "text": row["text"] or "",
        "image_url": row["image_url"],
        "created_at": row["created_at"],
        "edited_at": row["edited_at"] if "edited_at" in row.keys() else None,
        "poll_id": int(row["poll_id"]) if "poll_id" in row.keys() and row["poll_id"] is not None else None,
    }


MESSAGE_COLUMNS = ("id", "sender_id", "recipient_id", "room_id", "edited_at", "text", "image_url",
                   "created_at", "(SELECT p.id FROM chat_polls p WHERE p.message_id = chat_messages.id) AS poll_id")


def store_message(sender_id: int, recipient_id: int | None, text_value: str | None,
                  image_url: str | None, room_id: int | None = None,
                  poll: dict[str, Any] | None = None) -> dict[str, Any]:
    created = _utcnow_iso()
    with engine.begin() as connection:
        result = connection.execute(text("""
            INSERT INTO chat_messages (sender_id, recipient_id, room_id, text, image_url, created_at)
            VALUES (:s, :r, :room, :t, :i, :c)
        """), {"s": sender_id, "r": recipient_id, "room": room_id,
               "t": text_value, "i": image_url, "c": created})
        new_id = int(result.lastrowid)
        poll_id = _create_poll(connection, new_id, sender_id, poll, created) if poll else None
    return {
        "id": new_id,
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "room_id": room_id,
        "text": text_value or "",
        "image_url": image_url,
        "created_at": created,
        "edited_at": None,
        "poll_id": poll_id,
    }


def fetch_messages(channel: str, user_id: int, peer_id: int | None, room_id: int | None,
                   before_id: int | None, limit: int) -> tuple[list[dict[str, Any]], bool]:
    """Страница сообщений (по возрастанию id) и признак «есть ещё старее»."""
    params: dict[str, Any] = {"limit": limit + 1}
    if channel == "dm":
        where = ("recipient_id IS NOT NULL AND "
                 "((sender_id = :me AND recipient_id = :peer) OR (sender_id = :peer AND recipient_id = :me))")
        params.update({"me": user_id, "peer": peer_id})
    else:
        where = "recipient_id IS NULL AND room_id = :room"
        params["room"] = room_id
    if before_id is not None:
        where += " AND id < :before_id"
        params["before_id"] = before_id
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT {', '.join(MESSAGE_COLUMNS)}
            FROM chat_messages WHERE {where}
            ORDER BY id DESC LIMIT :limit
        """), params).mappings().all()
    has_more = len(rows) > limit
    messages = [_row_to_message(row) for row in rows[:limit]]
    messages.reverse()
    return messages, has_more


def _active_staff(user_id: int) -> bool:
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT 1 FROM users WHERE id = :id AND is_active = 1
              AND role IN ('barista','senior','mentor')
        """), {"id": int(user_id)}).first()
    return row is not None


def _message_access(row: Any, user_id: int) -> bool:
    """Тема — по доступу к теме, ЛС — только участники переписки."""
    if row["recipient_id"] is None:
        return _can_access_room(int(row["room_id"]), int(user_id))
    return int(user_id) in (int(row["sender_id"]), int(row["recipient_id"]))


def match_mentions(text_value: str, exclude: int | None = None) -> set[int]:
    """Возвращает id активных сотрудников, упомянутых через @.

    Матчим и по telegram_username, и по display_name/name (целиком, первым
    словом и без пробелов), регистронезависимо.
    """
    tokens = {match.group(1).lower() for match in MENTION_RE.finditer(text_value or "")}
    if not tokens:
        return set()
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT id, name, display_name, telegram_username FROM users
            WHERE is_active = 1 AND role IN ('barista','senior','mentor')
        """)).mappings().all()
    found: set[int] = set()
    for row in rows:
        user_id = int(row["id"])
        if exclude is not None and user_id == int(exclude):
            continue
        candidates: set[str] = set()
        if row["telegram_username"]:
            candidates.add(str(row["telegram_username"]).lower())
        for field in ("display_name", "name"):
            value = str(row[field] or "").strip()
            if not value:
                continue
            candidates.add(value.lower())
            candidates.add(value.split()[0].lower())
            candidates.add(re.sub(r"\s+", "", value).lower())
        if candidates & tokens:
            found.add(user_id)
    return found


# --- Менеджер соединений ---------------------------------------------------- #


class ConnectionManager:
    """Живые WebSocket-соединения в памяти процесса (1 воркер uvicorn)."""

    def __init__(self) -> None:
        self._connections: dict[int, set[WebSocket]] = {}
        self._lock = threading.Lock()

    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        with self._lock:
            self._connections.setdefault(int(user_id), set()).add(websocket)

    def disconnect(self, user_id: int, websocket: WebSocket) -> None:
        with self._lock:
            sockets = self._connections.get(int(user_id))
            if not sockets:
                return
            sockets.discard(websocket)
            if not sockets:
                self._connections.pop(int(user_id), None)

    def is_online(self, user_id: int) -> bool:
        with self._lock:
            return bool(self._connections.get(int(user_id)))

    async def send_to_user(self, user_id: int, payload: dict[str, Any]) -> None:
        with self._lock:
            sockets = list(self._connections.get(int(user_id), ()))
        for websocket in sockets:
            try:
                await websocket.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception:  # noqa: BLE001 — мёртвый сокет просто убираем
                self.disconnect(int(user_id), websocket)

    async def send_many(self, user_ids: set[int], payload: dict[str, Any]) -> None:
        for user_id in list(user_ids):
            await self.send_to_user(int(user_id), payload)

    async def broadcast(self, payload: dict[str, Any], exclude: int | None = None) -> None:
        with self._lock:
            items = [(uid, ws) for uid, sockets in self._connections.items() for ws in sockets]
        for user_id, websocket in items:
            if exclude is not None and user_id == int(exclude):
                continue
            try:
                await websocket.send_text(json.dumps(payload, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                self.disconnect(user_id, websocket)


manager = ConnectionManager()


async def _emit(channel_row: Any, event: dict[str, Any]) -> None:
    """Шлёт событие всем, кто видит этот чат: тема — её аудитории, ЛС — двоим."""
    if channel_row["recipient_id"] is None:
        recipients = _room_recipients(int(channel_row["room_id"]))
        if recipients is None:
            await manager.broadcast(event)
        else:
            await manager.send_many(recipients, event)
    else:
        await manager.send_to_user(int(channel_row["sender_id"]), event)
        await manager.send_to_user(int(channel_row["recipient_id"]), event)


# --- Push для офлайн-получателей -------------------------------------------- #


def _chat_notify_mode(user_id: int) -> str:
    try:
        prefs = get_user_preferences(engine, int(user_id))
    except Exception:  # noqa: BLE001 — настройка не должна ломать доставку
        return "dm_mentions"
    mode = str(prefs.get("chat_notify") or "dm_mentions")
    return mode if mode in {"all", "dm_mentions", "none"} else "dm_mentions"


def _chat_push_candidates() -> set[int]:
    """Кто вообще подписан на push (у кого есть устройства)."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT DISTINCT s.user_id FROM web_push_subscriptions s
            JOIN users u ON u.id = s.user_id
            WHERE u.is_active = 1 AND u.role IN ('barista','senior','mentor')
        """)).scalars().all()
    return {int(value) for value in rows}


def _notify_chat_push(author: dict[str, Any], message: dict[str, Any]) -> None:
    """Шлёт push офлайн-получателям: ЛС — всегда, общий чат — упоминаниям и «все»."""
    try:
        author_id = int(author["id"])
        author_name = author.get("display_name") or author.get("name") or "Сотрудник"
        preview = (message.get("text") or "").strip()
        if message.get("poll_id"):
            preview = f"Опрос: {preview}" if preview else "Опрос"
        elif not preview:
            preview = "Фото"
        if len(preview) > 120:
            preview = preview[:119].rstrip() + "…"

        is_dm = message.get("recipient_id") is not None
        mentioned: set[int] = set()
        if is_dm:
            targets: set[int] = {int(message["recipient_id"])}
        else:
            mentioned = match_mentions(message.get("text") or "", exclude=author_id)
            targets = _chat_push_candidates()

        for user_id in targets:
            if user_id == author_id or manager.is_online(user_id):
                continue
            mode = _chat_notify_mode(user_id)
            if mode == "none":
                continue
            if mode == "dm_mentions" and not (is_dm or user_id in mentioned):
                continue
            if not is_dm and user_id not in mentioned and mode != "all":
                continue
            if is_dm:
                title = f"{author_name} · Личное сообщение"
                url = f"/?view=chat&peer={author_id}"
            else:
                title = f"{author_name} · {_room_name(message.get('room_id'))}"
                url = f"/?view=chat&room={message.get('room_id')}"
            push.send_to_user(engine, user_id, {
                "title": title,
                "body": preview,
                "url": url,
                "tag": f"chat-{message.get('id')}",
            })
    except Exception as error:  # noqa: BLE001 — push не должен ломать доставку
        logger.warning("Чат: push не отправлен: %s", error)


# --- HTTP ------------------------------------------------------------------- #


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Некорректный JSON") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Ожидался JSON-объект")
    return payload


@chat_router.get("/users")
def chat_users(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Собеседники для личных чатов: сначала те, с кем писали недавно."""
    me = int(user["id"])
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT u.id, u.name, u.display_name, u.role, u.avatar_rev,
                   (SELECT MAX(m.created_at) FROM chat_messages m
                     WHERE m.recipient_id IS NOT NULL
                       AND ((m.sender_id = u.id AND m.recipient_id = :me)
                            OR (m.sender_id = :me AND m.recipient_id = u.id))) AS last_at,
                   (SELECT COALESCE(NULLIF(TRIM(m.text), ''), 'Фото') FROM chat_messages m
                     WHERE m.recipient_id IS NOT NULL
                       AND ((m.sender_id = u.id AND m.recipient_id = :me)
                            OR (m.sender_id = :me AND m.recipient_id = u.id))
                     ORDER BY m.id DESC LIMIT 1) AS last_text
            FROM users u
            WHERE u.is_active = 1 AND u.role IN ('barista','senior','mentor') AND u.id != :me
        """), {"me": me}).mappings().all()
    users = []
    for row in rows:
        preview = str(row["last_text"] or "").strip().replace("\n", " ")
        if len(preview) > 80:
            preview = preview[:79].rstrip() + "…"
        users.append({
            "id": int(row["id"]),
            "name": row["name"],
            "display_name": row["display_name"],
            "role": row["role"],
            "avatar_rev": int(row["avatar_rev"] or 0),
            "last_at": row["last_at"],
            "last_text": preview,
        })
    active = sorted([u for u in users if u["last_at"]], key=lambda item: item["last_at"], reverse=True)
    empty = sorted([u for u in users if not u["last_at"]],
                   key=lambda item: str(item["display_name"] or item["name"] or "").lower())
    return {"users": active + empty}


def _room_row(row: Any) -> dict[str, Any]:
    preview = str(row["last_text"] or "").strip().replace("\n", " ")
    if len(preview) > 80:
        preview = preview[:79].rstrip() + "…"
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "visibility": str(row["visibility"] or "all"),
        "last_at": row["last_at"],
        "last_text": preview,
        "last_sender": row["last_sender"],
    }


ROOM_ACTIVITY_SQL = """
    (SELECT m.created_at FROM chat_messages m
      WHERE m.room_id = r.id ORDER BY m.id DESC LIMIT 1) AS last_at,
    (SELECT COALESCE(NULLIF(TRIM(m.text), ''),
                     CASE WHEN EXISTS (SELECT 1 FROM chat_polls p WHERE p.message_id = m.id)
                          THEN 'Опрос' ELSE 'Фото' END)
       FROM chat_messages m
      WHERE m.room_id = r.id ORDER BY m.id DESC LIMIT 1) AS last_text,
    (SELECT COALESCE(u.display_name, u.name) FROM chat_messages m
       JOIN users u ON u.id = m.sender_id
      WHERE m.room_id = r.id ORDER BY m.id DESC LIMIT 1) AS last_sender
"""


def _sort_rooms(rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active = sorted([r for r in rooms if r["last_at"]], key=lambda item: item["last_at"], reverse=True)
    empty = sorted([r for r in rooms if not r["last_at"]], key=lambda item: str(item["name"]).lower())
    return active + empty


@chat_router.get("/rooms")
def chat_room_list(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Темы, доступные пользователю: сначала недавние, потом пустые по алфавиту."""
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT r.id, r.name, r.visibility, {ROOM_ACTIVITY_SQL}
            FROM chat_rooms r
            WHERE r.visibility = 'all'
               OR EXISTS (SELECT 1 FROM chat_room_members m
                          WHERE m.room_id = r.id AND m.user_id = :me)
            ORDER BY r.order_index, r.id
        """), {"me": int(user["id"])}).mappings().all()
    return {"rooms": _sort_rooms([_room_row(row) for row in rows])}


@chat_router.get("/rooms/manage")
def chat_room_manage(user: dict[str, Any] = Depends(require_manager)) -> dict[str, Any]:
    """Все темы с участниками — для панели управления."""
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT r.id, r.name, r.visibility, {ROOM_ACTIVITY_SQL}
            FROM chat_rooms r ORDER BY r.order_index, r.id
        """)).mappings().all()
        members = connection.execute(text(
            "SELECT room_id, user_id FROM chat_room_members"
        )).mappings().all()
    by_room: dict[int, list[int]] = {}
    for item in members:
        by_room.setdefault(int(item["room_id"]), []).append(int(item["user_id"]))
    rooms = []
    for row in rows:
        room = _room_row(row)
        room["member_ids"] = by_room.get(int(row["id"]), [])
        rooms.append(room)
    return {"rooms": _sort_rooms(rooms)}


def _normalize_room_fields(payload: dict[str, Any], *, require_name: bool) -> tuple[str | None, str, list[int]]:
    name = str(payload.get("name") or "").strip()
    if require_name and (not name or len(name) > MAX_ROOM_NAME):
        raise HTTPException(status_code=422, detail=f"Название темы — от 1 до {MAX_ROOM_NAME} символов")
    if name and len(name) > MAX_ROOM_NAME:
        raise HTTPException(status_code=422, detail=f"Название темы — от 1 до {MAX_ROOM_NAME} символов")
    visibility = str(payload.get("visibility") or "all")
    if visibility not in ROOM_VISIBILITY:
        raise HTTPException(status_code=422, detail="Неизвестная видимость темы")
    member_ids: list[int] = []
    for value in payload.get("member_ids") or []:
        try:
            candidate = int(value)
        except (TypeError, ValueError):
            continue
        if candidate not in member_ids and _active_staff(candidate):
            member_ids.append(candidate)
    if visibility == "selected" and not member_ids:
        raise HTTPException(status_code=422, detail="Выберите хотя бы одного сотрудника")
    return (name or None), visibility, member_ids


def _save_room_members(connection: Any, room_id: int, visibility: str,
                       member_ids: list[int], author_id: int) -> None:
    connection.execute(text("DELETE FROM chat_room_members WHERE room_id = :id"), {"id": int(room_id)})
    if visibility != "selected":
        return
    if author_id not in member_ids:
        member_ids = [author_id] + member_ids
    for member_id in member_ids:
        connection.execute(text("""
            INSERT OR IGNORE INTO chat_room_members (room_id, user_id) VALUES (:room, :user)
        """), {"room": int(room_id), "user": int(member_id)})


@chat_router.post("/rooms")
async def chat_room_create(request: Request, user: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    payload = await _json_object(request)
    name, visibility, member_ids = _normalize_room_fields(payload, require_name=True)
    with engine.begin() as connection:
        next_order = connection.execute(
            text("SELECT COALESCE(MAX(order_index), -1) + 1 FROM chat_rooms")
        ).scalar_one()
        result = connection.execute(text("""
            INSERT INTO chat_rooms (name, created_by, created_at, order_index, visibility)
            VALUES (:name, :author, :now, :order_index, :visibility)
        """), {"name": name, "author": int(user["id"]), "now": _utcnow_iso(),
               "order_index": int(next_order), "visibility": visibility})
        room_id = int(result.lastrowid)
        _save_room_members(connection, room_id, visibility, member_ids, int(user["id"]))
    return {"ok": True, "room": {"id": room_id, "name": name, "visibility": visibility,
                                  "member_ids": member_ids, "last_at": None, "last_text": "",
                                  "last_sender": None}}


@chat_router.patch("/rooms/{room_id}")
async def chat_room_update(room_id: int, request: Request,
                           user: dict[str, Any] = Depends(require_manager_csrf)) -> dict[str, Any]:
    """Частичное обновление темы: имя, видимость и/или участники."""
    payload = await _json_object(request)
    with engine.begin() as connection:
        exists = connection.execute(
            text("SELECT name, visibility FROM chat_rooms WHERE id = :id"), {"id": int(room_id)}
        ).mappings().first()
        if not exists:
            raise HTTPException(status_code=404, detail="Тема не найдена")

        name = str(payload.get("name") or "").strip()
        if name and len(name) > MAX_ROOM_NAME:
            raise HTTPException(status_code=422, detail=f"Название темы — от 1 до {MAX_ROOM_NAME} символов")
        current_visibility = str(exists["visibility"] or "all")
        visibility = str(payload.get("visibility") or current_visibility)
        if visibility not in ROOM_VISIBILITY:
            raise HTTPException(status_code=422, detail="Неизвестная видимость темы")

        if "member_ids" in payload:
            member_ids: list[int] = []
            for value in payload.get("member_ids") or []:
                try:
                    candidate = int(value)
                except (TypeError, ValueError):
                    continue
                if candidate not in member_ids and _active_staff(candidate):
                    member_ids.append(candidate)
        else:
            member_ids = _room_member_ids(connection, room_id)

        if visibility == "selected" and not member_ids:
            raise HTTPException(status_code=422, detail="Выберите хотя бы одного сотрудника")

        connection.execute(text("""
            UPDATE chat_rooms SET name = :name, visibility = :visibility WHERE id = :id
        """), {"name": name or exists["name"], "visibility": visibility, "id": int(room_id)})
        _save_room_members(connection, room_id, visibility, member_ids, int(user["id"]))
    return {"ok": True, "room": {"id": int(room_id), "name": name or exists["name"],
                                 "visibility": visibility, "member_ids": member_ids}}


@chat_router.get("/messages")
def chat_messages(channel: str = "room", peer_id: int | None = None, room_id: int | None = None,
                  before_id: int | None = None, limit: int = DEFAULT_PAGE,
                  user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """История: личные (`channel=dm&peer_id`) или тема (`channel=room&room_id`)."""
    if channel not in {"room", "dm"}:
        raise HTTPException(status_code=422, detail="Неизвестный канал")
    peer = None
    room = None
    if channel == "dm":
        if peer_id is None or int(peer_id) == int(user["id"]):
            raise HTTPException(status_code=422, detail="Некорректный собеседник")
        if not _active_staff(int(peer_id)):
            raise HTTPException(status_code=404, detail="Сотрудник не найден")
        peer = int(peer_id)
    else:
        room = int(room_id) if room_id is not None else default_room_id()
        if not _room_exists(room):
            raise HTTPException(status_code=404, detail="Тема не найдена")
        if not _can_access_room(room, int(user["id"])):
            raise HTTPException(status_code=403, detail="Нет доступа к теме")
    page = max(1, min(int(limit), MAX_PAGE))
    messages, has_more = fetch_messages(channel, int(user["id"]), peer, room,
                                        int(before_id) if before_id is not None else None, page)
    return {"messages": messages, "has_more": has_more}


@chat_router.patch("/messages/{message_id}")
async def chat_message_edit(message_id: int, request: Request,
                            user: dict[str, Any] = Depends(require_csrf)) -> dict[str, Any]:
    """Правка своего текстового сообщения."""
    payload = await _json_object(request)
    text_value = str(payload.get("text") or "").strip()[:MAX_TEXT]
    if not text_value:
        raise HTTPException(status_code=422, detail="Пустое сообщение")
    with engine.begin() as connection:
        row = connection.execute(text(
            "SELECT id, sender_id, recipient_id, room_id, image_url FROM chat_messages WHERE id = :id"
        ), {"id": int(message_id)}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Сообщение не найдено")
        if int(row["sender_id"]) != int(user["id"]):
            raise HTTPException(status_code=403, detail="Можно править только свои сообщения")
        if row["image_url"]:
            raise HTTPException(status_code=422, detail="Нельзя править сообщение с изображением")
        connection.execute(text("""
            UPDATE chat_messages SET text = :text, edited_at = :now WHERE id = :id
        """), {"text": text_value, "now": _utcnow_iso(), "id": int(message_id)})
        updated = connection.execute(text(f"""
            SELECT {', '.join(MESSAGE_COLUMNS)} FROM chat_messages WHERE id = :id
        """), {"id": int(message_id)}).mappings().first()
    message = _row_to_message(updated)
    await _emit(row, {"type": "message_edit", "message": message})
    return {"ok": True, "message": message}


@chat_router.delete("/messages/{message_id}")
async def chat_message_delete(message_id: int, user: dict[str, Any] = Depends(require_csrf)) -> dict[str, Any]:
    """Удаление: своё сообщение, а senior/mentor — любое."""
    with engine.begin() as connection:
        row = connection.execute(text(
            "SELECT id, sender_id, recipient_id, room_id FROM chat_messages WHERE id = :id"
        ), {"id": int(message_id)}).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="Сообщение не найдено")
        if int(row["sender_id"]) != int(user["id"]) and user["role"] not in {"senior", "mentor"}:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        target = dict(row)
        connection.execute(text("""
            DELETE FROM chat_poll_votes WHERE poll_id IN (SELECT id FROM chat_polls WHERE message_id = :id)
        """), {"id": int(message_id)})
        connection.execute(text("""
            DELETE FROM chat_poll_options WHERE poll_id IN (SELECT id FROM chat_polls WHERE message_id = :id)
        """), {"id": int(message_id)})
        connection.execute(text("DELETE FROM chat_polls WHERE message_id = :id"), {"id": int(message_id)})
        connection.execute(text("DELETE FROM chat_messages WHERE id = :id"), {"id": int(message_id)})
    await _emit(target, {"type": "message_delete", "id": int(message_id)})
    return {"ok": True, "id": int(message_id)}


@chat_router.get("/polls/{poll_id}")
def chat_poll_get(poll_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    poll = _poll_payload(int(poll_id), int(user["id"]))
    if poll is None:
        raise HTTPException(status_code=404, detail="Опрос не найден")
    return {"poll": poll}


@chat_router.post("/polls/{poll_id}/vote")
async def chat_poll_vote(poll_id: int, request: Request,
                         user: dict[str, Any] = Depends(require_csrf)) -> dict[str, Any]:
    payload = await _json_object(request)
    raw_options = payload.get("option_ids")
    if not isinstance(raw_options, list):
        raise HTTPException(status_code=422, detail="Не передан список вариантов")
    chosen: list[int] = []
    for value in raw_options:
        try:
            candidate = int(value)
        except (TypeError, ValueError):
            continue
        if candidate not in chosen:
            chosen.append(candidate)
    with engine.begin() as connection:
        poll = connection.execute(text(
            "SELECT id, message_id, multiple, allow_change FROM chat_polls WHERE id = :id"
        ), {"id": int(poll_id)}).mappings().first()
        if not poll:
            raise HTTPException(status_code=404, detail="Опрос не найден")
        valid = {int(row) for row in connection.execute(
            text("SELECT id FROM chat_poll_options WHERE poll_id = :id"), {"id": int(poll_id)}
        ).scalars().all()}
        chosen = [option for option in chosen if option in valid]
        if not chosen:
            raise HTTPException(status_code=422, detail="Выберите вариант")
        if not poll["multiple"] and len(chosen) > 1:
            raise HTTPException(status_code=422, detail="Можно выбрать только один вариант")
        message_row = connection.execute(text(
            "SELECT id, sender_id, recipient_id, room_id FROM chat_messages WHERE id = :id"
        ), {"id": int(poll["message_id"])}).mappings().first()
        if message_row is None or not _message_access(message_row, int(user["id"])):
            raise HTTPException(status_code=403, detail="Нет доступа к опросу")
        already = connection.execute(text(
            "SELECT 1 FROM chat_poll_votes WHERE poll_id = :poll AND user_id = :user LIMIT 1"
        ), {"poll": int(poll_id), "user": int(user["id"])}).first()
        if already and not poll["allow_change"]:
            raise HTTPException(status_code=403, detail="Изменение ответа не разрешено")
        connection.execute(text(
            "DELETE FROM chat_poll_votes WHERE poll_id = :poll AND user_id = :user"
        ), {"poll": int(poll_id), "user": int(user["id"])})
        for option_id in chosen:
            connection.execute(text("""
                INSERT OR IGNORE INTO chat_poll_votes (poll_id, option_id, user_id, created_at)
                VALUES (:poll, :option, :user, :created)
            """), {"poll": int(poll_id), "option": option_id, "user": int(user["id"]),
                   "created": _utcnow_iso()})
    await _emit(message_row, {"type": "poll_update", "poll_id": int(poll_id)})
    return {"ok": True, "poll": _poll_payload(int(poll_id), int(user["id"]))}


@chat_router.post("/upload")
async def chat_upload(request: Request, user: dict[str, Any] = Depends(require_csrf)) -> dict[str, Any]:
    """Тело запроса — сам файл изображения (без multipart, как в аватарах)."""
    data = await request.body()
    if not data:
        raise HTTPException(status_code=422, detail="Пустой файл")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Изображение больше 8 МБ")
    compressed = compress_image(data)
    if compressed is None:
        raise HTTPException(status_code=422, detail="Не удалось прочитать изображение")
    name = f"{uuid.uuid4().hex}.webp"
    (uploads_dir() / name).write_bytes(compressed)
    return {"url": f"/chat/uploads/{name}", "size": len(compressed)}


@chat_router.get("/uploads/{filename}")
def chat_upload_file(filename: str, user: dict[str, Any] = Depends(require_user)):
    if not IMAGE_FILENAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="Файл не найден")
    path = uploads_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=86400"})


# --- WebSocket -------------------------------------------------------------- #


def _ws_user(websocket: WebSocket) -> dict[str, Any] | None:
    session = read_session(websocket.cookies.get(COOKIE_NAME))
    if not session:
        return None
    with engine.connect() as connection:
        user = connection.execute(text("""
            SELECT id, name, display_name, role, session_epoch FROM users
            WHERE id = :id AND is_active = 1 AND role IN ('barista','senior','mentor')
        """), {"id": session["user_id"]}).mappings().first()
    if not user:
        return None
    if int(user["session_epoch"] or 0) != int(session["epoch"] or 0):
        return None
    return dict(user)


def _origin_ok(websocket: WebSocket) -> bool:
    """Грубая защита от межсайтового подключения (WS не покрыт CSRF-токеном)."""
    origin = websocket.headers.get("origin")
    if not origin:
        return True  # небраузерные клиенты/старые версии могут не слать Origin
    host = websocket.headers.get("host") or ""
    return bool(host) and origin.split("://")[-1] == host


async def _handle_incoming(author: dict[str, Any], payload: dict[str, Any]) -> None:
    text_value = str(payload.get("text") or "").strip()[:MAX_TEXT]

    image_url = payload.get("image_url")
    if image_url is not None:
        image_url = str(image_url)
        if not IMAGE_URL_RE.match(image_url):
            image_url = None

    recipient_id: int | None = None
    raw_recipient = payload.get("recipient_id")
    if raw_recipient is not None:
        try:
            recipient_id = int(raw_recipient)
        except (TypeError, ValueError):
            recipient_id = None
        if recipient_id is not None and (recipient_id == int(author["id"]) or not _active_staff(recipient_id)):
            recipient_id = None

    room_id: int | None = None
    if recipient_id is None:
        raw_room = payload.get("room_id")
        try:
            room_id = int(raw_room) if raw_room is not None else default_room_id()
        except (TypeError, ValueError):
            room_id = default_room_id()
        if not _room_exists(room_id) or not _can_access_room(room_id, int(author["id"])):
            await manager.send_to_user(int(author["id"]), {"type": "error", "detail": "Нет доступа к теме"})
            return

    poll, poll_error = (None, None)
    if payload.get("poll") is not None:
        poll, poll_error = _normalize_poll(payload.get("poll"))
        if poll_error:
            await manager.send_to_user(int(author["id"]), {"type": "error", "detail": poll_error})
            return

    if not text_value and not image_url and not poll:
        await manager.send_to_user(int(author["id"]), {"type": "error", "detail": "Пустое сообщение"})
        return

    message = store_message(int(author["id"]), recipient_id, text_value or None, image_url, room_id, poll)
    envelope = {"type": "message", "message": message}
    if recipient_id is None:
        recipients = _room_recipients(room_id)
        if recipients is None:
            await manager.broadcast(envelope)
        else:
            await manager.send_many(recipients, envelope)
    else:
        await manager.send_to_user(int(author["id"]), envelope)
        await manager.send_to_user(recipient_id, envelope)

    threading.Thread(target=_notify_chat_push, args=(author, message), daemon=True).start()


@chat_router.websocket("/ws")
async def chat_socket(websocket: WebSocket) -> None:
    user = _ws_user(websocket)
    if user is None or not _origin_ok(websocket):
        await websocket.close(code=1008)
        return
    await manager.connect(int(user["id"]), websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except ValueError:
                await manager.send_to_user(int(user["id"]), {"type": "error", "detail": "Некорректный JSON"})
                continue
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type")
            if kind == "ping":
                await manager.send_to_user(int(user["id"]), {"type": "pong"})
            elif kind == "message":
                await _handle_incoming(user, payload)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(int(user["id"]), websocket)


__all__ = ["chat_router", "router", "initialize_schema", "manager", "compress_image",
           "uploads_dir", "default_room_id"]