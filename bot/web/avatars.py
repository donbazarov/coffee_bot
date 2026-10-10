"""Аватары сотрудников.

Файлы живут локально в каталоге данных (`data/avatars`), не в git и не в образе,
чтобы не раздувать репозиторий и не терять фото между деплоями.

Фото из Telegram скачивается один раз при первом входе (Bot API
`getUserProfilePhotos` → `getFile` → файл), затем нормализуется Pillow:
EXIF-разворот, квадратный кроп, 256×256, JPEG. Пользователь может загрузить
своё фото или сбросить его.

Скачивание идёт обычными HTTPS-вызовами из веб-процесса: второй long-polling на
этом токене поднимать нельзя — Telegram вернёт 409 для `telegram_login_bot`.
"""

from __future__ import annotations

import io
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps
from sqlalchemy import text
from sqlalchemy.engine import Engine

from bot.config import BotConfig

logger = logging.getLogger("bot.web.avatars")

API_BASE = "https://api.telegram.org/bot{token}/{method}"
FILE_BASE = "https://api.telegram.org/file/bot{token}/{path}"
TIMEOUT = 20
SIZE = 256
_ROOT = Path(__file__).resolve().parent.parent.parent

# Отметка о попытке скачивания в пределах процесса: чтобы для сотрудников без
# фото не дёргать Telegram на каждой загрузке страницы.
_attempted: set[int] = set()


def _token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN") or BotConfig.token


_avatars_dir: Path | None = None


def avatars_dir() -> Path:
    """Каталог аватаров. Создаётся один раз за процесс."""
    global _avatars_dir
    if _avatars_dir is None:
        path = Path(os.getenv("NEFT_AVATARS_DIR") or (_ROOT / "data" / "avatars"))
        path.mkdir(parents=True, exist_ok=True)
        _avatars_dir = path
    return _avatars_dir


def avatar_path(user_id: int) -> Path:
    return avatars_dir() / f"{int(user_id)}.jpg"


def has_avatar(user_id: int) -> bool:
    return avatar_path(user_id).is_file()


def _normalize(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        side = min(image.size)
        left = (image.width - side) // 2
        top = (image.height - side) // 2
        image = image.crop((left, top, left + side, top + side)).resize((SIZE, SIZE), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=86, optimize=True)
        return buffer.getvalue()


def store_avatar(user_id: int, data: bytes) -> bool:
    """Нормализует и сохраняет аватар. Возвращает False, если это не картинка."""
    try:
        normalized = _normalize(data)
    except Exception as error:  # noqa: BLE001 — битый файл не должен ронять запрос
        logger.warning("Аватар пользователя %s не распознан: %s", user_id, error)
        return False
    path = avatar_path(user_id)
    temporary = path.with_suffix(".jpg.tmp")
    temporary.write_bytes(normalized)
    temporary.replace(path)
    return True


def delete_avatar(user_id: int) -> None:
    try:
        avatar_path(user_id).unlink()
    except FileNotFoundError:
        pass


def _api_call(method: str, params: dict[str, Any]) -> Any:
    token = _token()
    if not token:
        return None
    url = f"{API_BASE.format(token=token, method=method)}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        logger.warning("Telegram %s недоступен: %s", method, error)
        return None
    if not payload.get("ok"):
        logger.warning("Telegram %s отклонил запрос: %s", method, payload.get("description"))
        return None
    return payload.get("result")


def fetch_telegram_photo(telegram_id: int) -> bytes | None:
    if not BotConfig.telegram_outbound_enabled:
        return None
    photos = _api_call("getUserProfilePhotos", {"user_id": telegram_id, "limit": 1})
    if not photos or not photos.get("photos"):
        return None
    file_id = photos["photos"][0][-1]["file_id"]
    info = _api_call("getFile", {"file_id": file_id})
    file_path = (info or {}).get("file_path")
    token = _token()
    if not file_path or not token:
        return None
    try:
        with urllib.request.urlopen(FILE_BASE.format(token=token, path=file_path), timeout=TIMEOUT) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        logger.warning("Не удалось скачать фото из Telegram: %s", error)
        return None


def ensure_telegram_avatar(engine: Engine, user_id: int, telegram_id: int | None) -> None:
    """Скачивает фото из Telegram один раз. Идемпотентно; вызывать в фоне.

    `avatar_rev` увеличивается только при успешном сохранении файла, поэтому
    «есть файл» ⟺ `avatar_rev > 0` — это использует фронтенд как признак аватара.
    """
    if not BotConfig.telegram_outbound_enabled or not telegram_id or user_id in _attempted:
        return
    _attempted.add(user_id)
    try:
        if has_avatar(user_id):
            return
        data = fetch_telegram_photo(int(telegram_id))
        if not data or not store_avatar(user_id, data):
            return
        with engine.begin() as connection:
            connection.execute(text("UPDATE users SET avatar_rev = avatar_rev + 1 WHERE id = :id"), {"id": user_id})
        logger.info("Аватар пользователя %s получен из Telegram", user_id)
    except Exception as error:  # noqa: BLE001 — фоновый поток не должен ничего ронять
        logger.warning("Аватар пользователя %s не получен: %s", user_id, error)
