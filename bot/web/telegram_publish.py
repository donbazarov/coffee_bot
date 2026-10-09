"""Публикации в Telegram-каналы: снимок графика — в анонсы, замены — текстом.

Отправляем обычными HTTPS-вызовами Bot API из веб-процесса. Поднимать второго
long-polling на этом же токене нельзя: `getUpdates` уже держит
`bot/web/telegram_login_bot.py`, иначе Telegram вернёт 409.

Каналы задаются в настройках сайта (панель управления); ошибки отправки только
логируются, чтобы не ломать сохранение графика.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from bot.config import BotConfig
from bot.web import schedule_snapshot
from bot.web.calendar_service import get_app_settings

logger = logging.getLogger("bot.web.telegram")

API_BASE = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 20
MAX_MESSAGE = 4000
MAX_CAPTION = 1000


def _token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN") or BotConfig.token


def _multipart(data: dict[str, Any], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----neft{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in data.items():
        if value in (None, ""):
            continue
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    for key, (filename, payload, mime) in files.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode("utf-8") + payload + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _call(method: str, data: dict[str, Any], files: dict[str, tuple[str, bytes, str]] | None = None) -> bool:
    token = _token()
    if not token:
        logger.warning("Telegram: токен не задан — публикация пропущена")
        return False

    if files:
        body, content_type = _multipart(data, files)
    else:
        body = urllib.parse.urlencode({key: value for key, value in data.items() if value is not None}).encode("utf-8")
        content_type = "application/x-www-form-urlencoded"

    request = urllib.request.Request(
        API_BASE.format(token=token, method=method), data=body, headers={"Content-Type": content_type}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        logger.warning("Telegram %s недоступен: %s", method, error)
        return False

    if not payload.get("ok"):
        logger.warning("Telegram %s отклонил запрос: %s", method, payload.get("description"))
        return False
    return True


def send_message(chat_id: str, text: str) -> bool:
    if not chat_id or not text:
        return False
    return _call("sendMessage", {"chat_id": chat_id, "text": text[:MAX_MESSAGE], "disable_web_page_preview": "true"})


def send_photo(chat_id: str, photo_path: Path, caption: str = "") -> bool:
    if not chat_id or not photo_path.is_file():
        return False
    mime = mimetypes.guess_type(photo_path.name)[0] or "image/jpeg"
    return _call(
        "sendPhoto",
        {"chat_id": chat_id, "caption": caption[:MAX_CAPTION]},
        {"photo": (photo_path.name, photo_path.read_bytes(), mime)},
    )


def publish_schedule_events(engine: Engine, result: dict[str, Any], actor: str) -> None:
    """Рассылает уведомления по итогам сохранения графика (замены/публикация)."""
    try:
        settings = get_app_settings(engine)
    except Exception as error:  # noqa: BLE001 — настройки не должны ломать сохранение
        logger.warning("Не удалось прочитать настройки каналов: %s", error)
        return

    if result.get("change_type") == "swap":
        lines = result.get("swap_lines") or []
        chat = settings.get("swap_chat")
        if lines and chat:
            send_message(chat, f"Замены · {actor}\n" + "\n".join(lines))
        return

    if result.get("published"):
        chat = settings.get("announce_chat")
        shots = result.get("snapshots") or []
        for index, shot in enumerate(shots, start=1):
            path = schedule_snapshot.SNAPSHOTS_DIR / os.path.basename(shot["url"])
            caption = shot.get("caption") or f"График смен · {actor}"
            if len(shots) > 1:
                caption = f"{caption} · снимок {index}/{len(shots)}"
            send_photo(chat, path, caption)


__all__ = ["publish_schedule_events", "send_message", "send_photo"]
