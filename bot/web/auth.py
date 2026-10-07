import base64
import hashlib
import hmac
import os
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from bot.config import BotConfig
from bot.database.models import engine

COOKIE_NAME = "coffee_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7
LOGIN_MAX_AGE = 60 * 5
CALENDAR_TOKEN_MAX_AGE = 60 * 60 * 24 * 365 * 5


def _bot_token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN") or BotConfig.token


def _session_key() -> bytes | None:
    secret = os.getenv("WEB_SESSION_SECRET") or _bot_token()
    return hashlib.sha256(secret.encode("utf-8")).digest() if secret else None


def verify_telegram_login(data: dict[str, Any]) -> bool:
    token = _bot_token()
    if not token or not isinstance(data.get("hash"), str):
        return False

    auth_date = data.get("auth_date")
    telegram_id = data.get("id")
    try:
        timestamp = int(auth_date)
        int(telegram_id)
    except (TypeError, ValueError):
        return False

    now = int(time.time())
    if timestamp > now + 30 or now - timestamp > LOGIN_MAX_AGE:
        return False

    fields = {
        str(key): str(value)
        for key, value in data.items()
        if key != "hash" and isinstance(value, (str, int, float))
    }
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hashlib.sha256(token.encode("utf-8")).digest()
    expected_hash = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_hash, data["hash"])


def register_or_find_telegram_user(telegram_data: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    telegram_id = int(telegram_data["id"])
    username = str(telegram_data.get("username") or "").strip().lstrip("@").lower()
    profile_name = " ".join(
        part.strip()
        for part in (str(telegram_data.get("first_name") or ""), str(telegram_data.get("last_name") or ""))
        if part.strip()
    ) or f"Telegram user {telegram_id}"

    try:
        with engine.begin() as connection:
            telegram_user = connection.execute(
                text("SELECT id, name, role, telegram_username, telegram_id, is_active FROM users WHERE telegram_id = :telegram_id"),
                {"telegram_id": telegram_id},
            ).mappings().first()

            username_user = None
            if username:
                username_user = connection.execute(
                    text("""
                        SELECT id, name, role, telegram_username, telegram_id, is_active
                        FROM users
                        WHERE lower(replace(telegram_username, '@', '')) = :username
                    """),
                    {"username": username},
                ).mappings().first()

            if username_user is not None:
                if username_user["telegram_id"] is not None and int(username_user["telegram_id"]) != telegram_id:
                    return None, "conflict"
                if telegram_user is not None and telegram_user["id"] != username_user["id"]:
                    return None, "conflict"
                user = username_user
                if user["telegram_id"] is None:
                    connection.execute(
                        text("UPDATE users SET telegram_id = :telegram_id WHERE id = :user_id AND telegram_id IS NULL"),
                        {"telegram_id": telegram_id, "user_id": user["id"]},
                    )
            elif telegram_user is not None:
                user = telegram_user
                if username and (user["telegram_username"] or "").strip().lstrip("@").lower() != username:
                    connection.execute(
                        text("UPDATE users SET telegram_username = :username WHERE id = :user_id"),
                        {"username": username, "user_id": user["id"]},
                    )
            else:
                connection.execute(
                    text("""
                        INSERT INTO users (name, telegram_username, telegram_id, role, is_active)
                        VALUES (:name, :username, :telegram_id, 'guest', 0)
                    """),
                    {"name": profile_name, "username": username or None, "telegram_id": telegram_id},
                )
                return None, "pending"

            if user["role"] == "guest":
                return None, "pending"
            if not user["is_active"] or user["role"] not in {"barista", "senior", "mentor"}:
                return None, "disabled"
            return {"id": user["id"], "name": user["name"], "role": user["role"]}, "approved"
    except IntegrityError:
        return None, "conflict"


def create_session(user_id: int) -> tuple[str, str]:
    key = _session_key()
    if key is None:
        raise RuntimeError("Telegram bot token is not configured")

    csrf_token = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
    payload = f"{user_id}.{int(time.time()) + SESSION_MAX_AGE}.{csrf_token}"
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}", csrf_token


def read_session(cookie_value: str | None) -> dict[str, Any] | None:
    key = _session_key()
    if not cookie_value or key is None:
        return None

    try:
        encoded, supplied_signature = cookie_value.rsplit(".", 1)
        expected_signature = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_signature, supplied_signature):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        user_id, expires_at, csrf_token = base64.urlsafe_b64decode(padded).decode("utf-8").split(".", 2)
        if int(expires_at) < int(time.time()):
            return None
        return {"user_id": int(user_id), "csrf_token": csrf_token}
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def create_calendar_token(user_id: int) -> str:
    key = _session_key()
    if key is None:
        raise RuntimeError("Telegram bot token is not configured")
    payload = f"{user_id}.{int(time.time()) + CALENDAR_TOKEN_MAX_AGE}.{os.urandom(16).hex()}"
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(key, f"calendar:{encoded}".encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def read_calendar_token(token: str) -> int | None:
    key = _session_key()
    if key is None:
        return None
    try:
        encoded, supplied_signature = token.rsplit(".", 1)
        expected_signature = hmac.new(key, f"calendar:{encoded}".encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_signature, supplied_signature):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        user_id, expires_at, _ = base64.urlsafe_b64decode(padded).decode("utf-8").split(".", 2)
        if int(expires_at) < int(time.time()):
            return None
        return int(user_id)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None