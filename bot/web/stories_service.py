"""НЕФТЬ · Истории гостей — хранилище и доменная логика.

Модуль перенесён из отдельного прототипа `stories/server.py` (stdlib HTTP-сервер)
в основной сайт. Публичный сервер больше не нужен: страница историй живёт в
FastAPI-приложении, а модерация — в личном кабинете наставника.

Хранилище:
    data/stories/db.json    — все истории (текст, ссылка на фото, флаг публикации)
    data/stories/uploads/*  — загруженные фотографии (по одной на историю)

Каталог можно вынести на постоянный диск: `NEFT_DATA_DIR=/var/data/stories`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# Каталог с данными можно вынести на постоянный диск хостинга:
#   NEFT_DATA_DIR=/var/data/stories
DATA_DIR = Path(
    os.environ.get("NEFT_DATA_DIR") or (REPO_ROOT / "data" / "stories")
).expanduser().resolve()
UPLOADS_DIR = DATA_DIR / "uploads"
DB_FILE = DATA_DIR / "db.json"

MAX_TEXT_LENGTH = 600
MAX_NAME_LENGTH = 40
MAX_PHOTO_BYTES = 5 * 1024 * 1024
ALLOWED_PHOTO_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

DEFAULT_AUTHOR = "Гость НЕФТИ"

# Кука «памяти гостя»: по ней узнаём устройство и не даём отправить несколько
# историй с одного телефона. Только httpOnly — из JavaScript недоступна.
GUEST_COOKIE = "neft_guest"
GUEST_COOKIE_PATH = "/stories"
GUEST_COOKIE_MAX_AGE = 2 * 365 * 24 * 60 * 60  # 2 года

DATA_URL_RE = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)$")

# Фото отдаются под /stories/uploads/. Прототип отдавал их с корня (/uploads/),
# поэтому данные из старого data/db.json нужно один раз переписать.
UPLOAD_URL_PREFIX = "/stories/uploads/"
LEGACY_UPLOAD_PREFIX = "/uploads/"

_lock = threading.Lock()


class StoriesError(Exception):
    """Ошибка домена историй. `status` уходит в HTTP-код ответа."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# Хранилище
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(db: dict[str, Any]) -> None:
    """Пишем во временный файл и подменяем — чтобы не потерять базу при сбое."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DB_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(db, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(DB_FILE)


def load_db() -> dict[str, Any]:
    """Читает базу. При первом запуске создаёт её."""
    with _lock:
        if not DB_FILE.exists():
            db: dict[str, Any] = {"settings": {}, "stories": []}
            _atomic_write(db)
            return db

        try:
            with DB_FILE.open("r", encoding="utf-8") as handle:
                db = json.load(handle)
        except (json.JSONDecodeError, OSError):
            # Повреждённый файл не теряем: откладываем в сторону и начинаем заново.
            backup = DB_FILE.with_suffix(f".broken-{int(datetime.now().timestamp())}.json")
            DB_FILE.replace(backup)
            db = {"settings": {}, "stories": []}
            _atomic_write(db)
            return db

        if not isinstance(db, dict):
            db = {"settings": {}, "stories": []}
        db.setdefault("settings", {})
        db.setdefault("stories", [])
        if not isinstance(db["stories"], list):
            db["stories"] = []
        return db


def save_db(db: dict[str, Any]) -> None:
    with _lock:
        _atomic_write(db)


# --------------------------------------------------------------------------- #
# Представления
# --------------------------------------------------------------------------- #

def public_story(story: dict[str, Any]) -> dict[str, Any]:
    """История в том виде, в котором её видит посетитель сайта."""
    return {
        "id": story["id"],
        "name": story.get("name") or DEFAULT_AUTHOR,
        "text": story["text"],
        "photo": story.get("photo"),
        "createdAt": story["createdAt"],
    }


def author_story(story: dict[str, Any]) -> dict[str, Any]:
    """История для её автора — он может её отредактировать."""
    return {
        "id": story["id"],
        "name": story.get("name") or DEFAULT_AUTHOR,
        "text": story["text"],
        "photo": story.get("photo"),
        "published": bool(story.get("published")),
        "createdAt": story["createdAt"],
    }


def moderation_story(story: dict[str, Any]) -> dict[str, Any]:
    """Полная история для панели модерации."""
    return {
        "id": story["id"],
        "name": story.get("name") or DEFAULT_AUTHOR,
        "text": story["text"],
        "photo": story.get("photo"),
        "published": bool(story.get("published")),
        "createdAt": story["createdAt"],
        "updatedAt": story.get("updatedAt") or story["createdAt"],
    }


# --------------------------------------------------------------------------- #
# Валидация
# --------------------------------------------------------------------------- #

def validate_text(raw: object) -> tuple[str | None, str | None]:
    """Возвращает (текст, ошибка)."""
    if not isinstance(raw, str):
        return None, "Текст истории обязателен"
    text = raw.strip()
    if not text:
        return None, "История не может быть пустой"
    if len(text) > MAX_TEXT_LENGTH:
        return None, f"Слишком длинная история: максимум {MAX_TEXT_LENGTH} символов"
    return text, None


def validate_name(raw: object) -> tuple[str | None, str | None]:
    """Возвращает (имя, ошибка)."""
    if not isinstance(raw, str):
        return None, "Укажите имя"
    name = " ".join(raw.split())  # схлопываем лишние пробелы и переводы строк
    if not name:
        return None, "Укажите имя"
    if len(name) > MAX_NAME_LENGTH:
        return None, f"Имя слишком длинное: максимум {MAX_NAME_LENGTH} символов"
    return name, None


def guest_hash(token: str) -> str:
    """Храним хеш токена, а не сам токен: утечка db.json не даст доступ к чужим историям."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_guest_token() -> str:
    return secrets.token_urlsafe(24)


# --------------------------------------------------------------------------- #
# Фотографии
# --------------------------------------------------------------------------- #

def save_photo(data_url: object) -> tuple[str | None, str | None]:
    """Декодирует data-URL и кладёт картинку в uploads. Возвращает (путь, ошибка)."""
    if data_url in (None, ""):
        return None, None
    if not isinstance(data_url, str):
        return None, "Некорректный файл фотографии"

    match = DATA_URL_RE.match(data_url.strip())
    if not match:
        return None, "Некорректный формат фотографии"

    mime = match.group("mime").lower()
    if mime not in ALLOWED_PHOTO_TYPES:
        return None, "Поддерживаются только изображения JPG, PNG, WEBP или GIF"

    try:
        payload = base64.b64decode(match.group("data"), validate=True)
    except (ValueError, TypeError):
        return None, "Не удалось прочитать фотографию"

    if len(payload) > MAX_PHOTO_BYTES:
        return None, f"Фотография слишком большая: максимум {MAX_PHOTO_BYTES // (1024 * 1024)} МБ"

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ALLOWED_PHOTO_TYPES[mime]}"
    (UPLOADS_DIR / filename).write_bytes(payload)
    return f"{UPLOAD_URL_PREFIX}{filename}", None


def delete_photo(photo_path: str | None) -> None:
    """Удаляет файл фотографии из uploads (если он существует)."""
    if not photo_path or not photo_path.startswith(UPLOAD_URL_PREFIX):
        return
    target = UPLOADS_DIR / os.path.basename(photo_path)
    if target.is_file():
        target.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Сценарии
# --------------------------------------------------------------------------- #

def list_published() -> list[dict[str, Any]]:
    """Только опубликованные истории — для карусели."""
    db = load_db()
    stories = [story for story in db["stories"] if story.get("published")]
    stories.sort(key=lambda story: story.get("createdAt", ""), reverse=True)
    return [public_story(story) for story in stories]


def list_all() -> list[dict[str, Any]]:
    """Все истории для панели модерации."""
    db = load_db()
    stories = sorted(db["stories"], key=lambda story: story.get("createdAt", ""), reverse=True)
    return [moderation_story(story) for story in stories]


def _find_by_guest(db: dict[str, Any], token: str) -> dict[str, Any] | None:
    if not token:
        return None
    hashed = guest_hash(token)
    return next((story for story in db["stories"] if story.get("guestHash") == hashed), None)


def find_guest_story(token: str) -> dict[str, Any] | None:
    return _find_by_guest(load_db(), token)


def create_story(body: dict[str, Any], token: str) -> tuple[dict[str, Any], str]:
    """Приём новой истории. Публикуется только после модерации.

    Возвращает (сохранённая история, токен гостя). Токен совпадает с переданным,
    если он уже был.
    """
    name, error = validate_name(body.get("name"))
    if error:
        raise StoriesError(400, error)

    text, error = validate_text(body.get("text"))
    if error:
        raise StoriesError(400, error)

    photo, error = save_photo(body.get("photo"))
    if error:
        raise StoriesError(400, error)

    db = load_db()

    # Одно устройство — одна история: вместо второй предлагаем править уже отправленную.
    if _find_by_guest(db, token) is not None:
        delete_photo(photo)
        raise StoriesError(409, "С этого устройства история уже отправлена — можно её изменить")

    token = token or new_guest_token()
    story = {
        "id": uuid.uuid4().hex,
        "name": name,
        "text": text,
        "photo": photo,
        "published": False,
        "guestHash": guest_hash(token),
        "createdAt": _now_iso(),
        "updatedAt": None,
    }
    db["stories"].insert(0, story)
    save_db(db)
    return story, token


def update_my_story(body: dict[str, Any], token: str) -> dict[str, Any]:
    """Правка своей истории. После изменения она снова уходит на модерацию."""
    db = load_db()
    story = _find_by_guest(db, token)
    if story is None:
        raise StoriesError(404, "История не найдена — возможно, устройство не сохранено")

    _apply_fields(story, body)
    story["published"] = False
    story["updatedAt"] = _now_iso()
    save_db(db)
    return author_story(story)


def update_moderation_story(story_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Правка истории модератором: текст, имя, фото и флаг публикации."""
    db = load_db()
    story = next((item for item in db["stories"] if item.get("id") == story_id), None)
    if story is None:
        raise StoriesError(404, "История не найдена")

    _apply_fields(story, body)
    if "published" in body:
        story["published"] = bool(body.get("published"))
    story["updatedAt"] = _now_iso()
    save_db(db)
    return moderation_story(story)


def delete_story(story_id: str) -> bool:
    """Удаляет историю вместе с фотографией."""
    db = load_db()
    story = next((item for item in db["stories"] if item.get("id") == story_id), None)
    if story is None:
        return False
    delete_photo(story.get("photo"))
    db["stories"] = [item for item in db["stories"] if item.get("id") != story_id]
    save_db(db)
    return True


def _apply_fields(story: dict[str, Any], body: dict[str, Any]) -> None:
    """Общая часть правки: имя, текст, фото.

    `photo`: null — удалить, "" — оставить как есть, data-URL — заменить.
    """
    if "name" in body:
        name, error = validate_name(body.get("name"))
        if error:
            raise StoriesError(400, error)
        story["name"] = name

    if "text" in body:
        text, error = validate_text(body.get("text"))
        if error:
            raise StoriesError(400, error)
        story["text"] = text

    if "photo" in body:
        new_photo = body.get("photo")
        if new_photo is None:
            delete_photo(story.get("photo"))
            story["photo"] = None
        elif new_photo != "":
            saved, error = save_photo(new_photo)
            if error:
                raise StoriesError(400, error)
            delete_photo(story.get("photo"))
            story["photo"] = saved


def migrate_from_prototype() -> dict[str, int]:
    """Разовая миграция данных отдельного прототипа «НЕФТЬ · Истории гостей».

    Что делает:
      * переписывает пути к фото `/uploads/x` -> `/stories/uploads/x`
        (прототип отдавал их с корня сайта, теперь они живут под /stories);
      * убирает из настроек неиспользуемый ключ модерации: доступ к панели
        закрыт обычной сессией сайта и ролью наставника.

    Идемпотентна: при повторном запуске ничего не меняет.
    """
    db = load_db()
    moved_photos = 0
    for story in db["stories"]:
        photo = story.get("photo")
        if isinstance(photo, str) and photo.startswith(LEGACY_UPLOAD_PREFIX):
            story["photo"] = UPLOAD_URL_PREFIX + photo[len(LEGACY_UPLOAD_PREFIX):]
            moved_photos += 1

    settings = db.get("settings")
    removed_key = 0
    if isinstance(settings, dict) and settings.pop("moderation_key", None):
        removed_key = 1

    if moved_photos or removed_key:
        save_db(db)
    return {"photos": moved_photos, "removed_key": removed_key}


def stats() -> dict[str, int]:
    """Сводка для панели модерации."""
    db = load_db()
    stories = db["stories"]
    published = sum(1 for story in stories if story.get("published"))
    return {
        "total": len(stories),
        "published": published,
        "pending": len(stories) - published,
    }


def read_upload(filename: str) -> tuple[Path, str] | None:
    """Безопасно достаёт файл из uploads. Возвращает (путь, mime) или None."""
    safe_name = os.path.basename(filename or "")
    if not safe_name or safe_name != filename:
        return None
    target = (UPLOADS_DIR / safe_name).resolve()
    try:
        target.relative_to(UPLOADS_DIR.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(target.suffix.lower(), "application/octet-stream")
    return target, mime


# Разделяемые темы оформления не нужны: история — публичная страница,
# поэтому статика подключается из bot/web/static/stories/.
__all__ = [
    "DATA_DIR",
    "UPLOADS_DIR",
    "GUEST_COOKIE",
    "GUEST_COOKIE_PATH",
    "GUEST_COOKIE_MAX_AGE",
    "MAX_NAME_LENGTH",
    "MAX_TEXT_LENGTH",
    "MAX_PHOTO_BYTES",
    "StoriesError",
    "create_story",
    "delete_story",
    "find_guest_story",
    "list_all",
    "list_published",
    "author_story",
    "migrate_from_prototype",
    "read_upload",
    "stats",
    "update_moderation_story",
    "update_my_story",
]
