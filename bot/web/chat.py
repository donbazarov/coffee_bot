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
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from sqlalchemy import text
from sqlalchemy.engine import Engine

from bot.database.models import engine
from bot.web import push
from bot.web.auth import COOKIE_NAME, read_session
from bot.web.calendar_service import get_user_preferences
from bot.web.deps import require_csrf, require_user

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


def initialize_schema(db: Engine) -> None:
    """Идемпотентно создаёт таблицу сообщений и индексы."""
    with db.begin() as connection:
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
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_chat_recipient ON chat_messages (recipient_id, id)
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_chat_sender_recipient ON chat_messages (sender_id, recipient_id, id)
        """))


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
        "text": row["text"] or "",
        "image_url": row["image_url"],
        "created_at": row["created_at"],
    }


def store_message(sender_id: int, recipient_id: int | None, text_value: str | None,
                  image_url: str | None) -> dict[str, Any]:
    created = _utcnow_iso()
    with engine.begin() as connection:
        result = connection.execute(text("""
            INSERT INTO chat_messages (sender_id, recipient_id, text, image_url, created_at)
            VALUES (:s, :r, :t, :i, :c)
        """), {"s": sender_id, "r": recipient_id, "t": text_value, "i": image_url, "c": created})
        new_id = int(result.lastrowid)
    return {
        "id": new_id,
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "text": text_value or "",
        "image_url": image_url,
        "created_at": created,
    }


def fetch_messages(channel: str, user_id: int, peer_id: int | None,
                   before_id: int | None, limit: int) -> tuple[list[dict[str, Any]], bool]:
    """Страница сообщений (по возрастанию id) и признак «есть ещё старее»."""
    params: dict[str, Any] = {"limit": limit + 1}
    if channel == "dm":
        where = ("recipient_id IS NOT NULL AND "
                 "((sender_id = :me AND recipient_id = :peer) OR (sender_id = :peer AND recipient_id = :me))")
        params.update({"me": user_id, "peer": peer_id})
    else:
        where = "recipient_id IS NULL"
    if before_id is not None:
        where += " AND id < :before_id"
        params["before_id"] = before_id
    with engine.connect() as connection:
        rows = connection.execute(text(f"""
            SELECT id, sender_id, recipient_id, text, image_url, created_at
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
        if not preview:
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
            kind = "Личное сообщение" if is_dm else ("Упоминание" if user_id in mentioned else "Общий чат")
            url = f"/?view=chat&peer={author_id}" if is_dm else "/?view=chat"
            push.send_to_user(engine, user_id, {
                "title": f"{author_name} · {kind}",
                "body": preview,
                "url": url,
                "tag": f"chat-{message.get('id')}",
            })
    except Exception as error:  # noqa: BLE001 — push не должен ломать доставку
        logger.warning("Чат: push не отправлен: %s", error)


# --- HTTP ------------------------------------------------------------------- #


@chat_router.get("/users")
def chat_users(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """Список сотрудников для вкладки «Личные» (кроме себя)."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT id, name, display_name, role, avatar_rev FROM users
            WHERE is_active = 1 AND role IN ('barista','senior','mentor') AND id != :me
            ORDER BY COALESCE(display_name, name) COLLATE NOCASE
        """), {"me": int(user["id"])}).mappings().all()
    return {"users": [
        {
            "id": int(row["id"]),
            "name": row["name"],
            "display_name": row["display_name"],
            "role": row["role"],
            "avatar_rev": int(row["avatar_rev"] or 0),
        }
        for row in rows
    ]}


@chat_router.get("/messages")
def chat_messages(channel: str = "general", peer_id: int | None = None,
                  before_id: int | None = None, limit: int = DEFAULT_PAGE,
                  user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    if channel not in {"general", "dm"}:
        raise HTTPException(status_code=422, detail="Неизвестный канал")
    if channel == "dm":
        if peer_id is None or int(peer_id) == int(user["id"]):
            raise HTTPException(status_code=422, detail="Некорректный собеседник")
        if not _active_staff(int(peer_id)):
            raise HTTPException(status_code=404, detail="Сотрудник не найден")
    page = max(1, min(int(limit), MAX_PAGE))
    messages, has_more = fetch_messages(channel, int(user["id"]),
                                        int(peer_id) if peer_id is not None else None,
                                        int(before_id) if before_id is not None else None, page)
    return {"messages": messages, "has_more": has_more}


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

    if not text_value and not image_url:
        await manager.send_to_user(int(author["id"]), {"type": "error", "detail": "Пустое сообщение"})
        return

    message = store_message(int(author["id"]), recipient_id, text_value or None, image_url)
    envelope = {"type": "message", "message": message}
    if recipient_id is None:
        await manager.broadcast(envelope)
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


__all__ = ["chat_router", "router", "initialize_schema", "manager", "compress_image", "uploads_dir"]
