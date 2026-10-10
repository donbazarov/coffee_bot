"""Web Push: доставка анонсов прямо в браузер сотрудника.

Как это устроено:

* **VAPID-ключи** создаются один раз при первом запуске и лежат в базе
  (`web_push_keys`), поэтому раскатка не требует ручной работы. Переопределить
  можно переменной окружения `VAPID_PRIVATE_KEY` (PEM или base64 от PEM).
* **Подписки устройств** — в `web_push_subscriptions`; одну и ту же запись
  обновляем при повторной подписке с того же браузера (`endpoint` уникален).
* **Кому отправлять** решают уведомления сотрудника (`web_announcement_prefs`):
  тот же переключатель категорий, что и счётчик в интерфейсе.
* Мёртвые подписки (`404`/`410` от push-сервиса) удаляем сразу, чтобы не
  копить мусор.

Push-сервисы: Apple Web Push для iOS и FCM для Chrome/Android — с этого сервера
доступны (проверено снаружи). Если конкретная доставка не проходит, ошибка
только логируется: лента и остальное приложение работать не перестают.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger("bot.web.push")

DEFAULT_SUBJECT = "mailto:support@neftcoffee.shop"
TIMEOUT = 10
MAX_BODY = 180

# Ключи читаются один раз за процесс: генерация дорогая, а меняются они только
# при перезапуске с другим окружением.
_cached_keys: tuple[str, str] | None = None
_cached_vapid: Any = None


def _public_b64(private_key: ec.EllipticCurvePrivateKey) -> str:
    """Открытый ключ в виде, который ждёт браузер: base64url без выравнивания."""
    numbers = private_key.public_key().public_numbers()
    raw = b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def initialize_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                user_agent TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_web_push_user
            ON web_push_subscriptions (user_id)
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_push_keys (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                private_pem TEXT NOT NULL,
                public_key TEXT NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))


def _env_private_key() -> str | None:
    raw = (os.getenv("VAPID_PRIVATE_KEY") or "").strip()
    if not raw:
        return None
    if raw.startswith("-----"):
        return raw if raw.endswith("\n") else raw + "\n"
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        logger.warning("VAPID_PRIVATE_KEY не читается: ожидался PEM или base64 от него")
        return None
    return decoded if decoded.endswith("\n") else decoded + "\n"


def keys(engine: Engine) -> tuple[str, str]:
    """(приватный PEM, публичный base64url). Создаёт ключи при первом вызове."""
    global _cached_keys
    if _cached_keys is not None:
        return _cached_keys

    from_env = _env_private_key()
    if from_env:
        private_key = serialization.load_pem_private_key(from_env.encode("utf-8"), password=None)
        _cached_keys = (from_env, _public_b64(private_key))  # type: ignore[arg-type]
        return _cached_keys

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT private_pem, public_key FROM web_push_keys WHERE id = 1")
        ).mappings().first()
    if row:
        _cached_keys = (row["private_pem"], row["public_key"])
        return _cached_keys

    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public = _public_b64(private_key)
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT OR REPLACE INTO web_push_keys (id, private_pem, public_key)
            VALUES (1, :pem, :public)
        """), {"pem": pem, "public": public})
    logger.info("Web Push: созданы VAPID-ключи")
    _cached_keys = (pem, public)
    return _cached_keys


def public_key(engine: Engine) -> str:
    return keys(engine)[1]


def _vapid(engine: Engine) -> Any:
    """Подписывающий объект для pywebpush.

    Важно: `vapid_private_key` в pywebpush — это либо путь к файлу, либо объект
    `py_vapid.Vapid01`; строку он отдаёт в `Vapid.from_string`, а тот декодирует
    base64-DER и на PEM падает с «ASN.1 parsing error». Поэтому собираем объект
    сами из нашего PEM и кэшируем: заодно не парсим ключ на каждую отправку.
    """
    global _cached_vapid
    if _cached_vapid is None:
        try:
            from py_vapid import Vapid01
        except ImportError:
            logger.error("Web Push недоступен: не установлен пакет py-vapid")
            return None
        _cached_vapid = Vapid01.from_pem(keys(engine)[0].encode("utf-8"))
    return _cached_vapid


def subject() -> str:
    return (os.getenv("VAPID_SUBJECT") or "").strip() or DEFAULT_SUBJECT


# --- Подписки --------------------------------------------------------------- #


def save_subscription(engine: Engine, user_id: int, subscription: dict[str, Any], user_agent: str = "") -> bool:
    endpoint = str(subscription.get("endpoint") or "").strip()
    keys_block = subscription.get("keys") or {}
    p256dh = str(keys_block.get("p256dh") or "").strip()
    auth = str(keys_block.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        return False
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO web_push_subscriptions (user_id, endpoint, p256dh, auth, user_agent)
            VALUES (:user_id, :endpoint, :p256dh, :auth, :agent)
            ON CONFLICT(endpoint) DO UPDATE SET
                user_id = :user_id,
                p256dh = :p256dh,
                auth = :auth,
                user_agent = :agent,
                last_seen_at = CURRENT_TIMESTAMP
        """), {"user_id": user_id, "endpoint": endpoint, "p256dh": p256dh,
               "auth": auth, "agent": (user_agent or "").strip()[:200] or None})
    return True


def delete_subscription(engine: Engine, user_id: int, endpoint: str) -> bool:
    with engine.begin() as connection:
        result = connection.execute(text("""
            DELETE FROM web_push_subscriptions WHERE endpoint = :endpoint AND user_id = :user_id
        """), {"endpoint": (endpoint or "").strip(), "user_id": user_id})
    return bool(result.rowcount)


def user_endpoints(engine: Engine, user_id: int) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT endpoint, p256dh, auth FROM web_push_subscriptions WHERE user_id = :user_id
        """), {"user_id": user_id}).mappings().all()
    return [dict(row) for row in rows]


def _prune(engine: Engine, endpoint: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM web_push_subscriptions WHERE endpoint = :endpoint"),
            {"endpoint": endpoint},
        )


def _deliver(engine: Engine, row: dict[str, Any], payload: dict[str, Any]) -> str:
    """Возвращает «ok», «gone» (подписка мертва) или «error»."""
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.error("Web Push недоступен: не установлен пакет pywebpush")
        return "error"

    vapid = _vapid(engine)
    if vapid is None:
        return "error"

    try:
        webpush(
            subscription_info={
                "endpoint": row["endpoint"],
                "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
            },
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=vapid,
            vapid_claims={"sub": subject()},
            timeout=TIMEOUT,
        )
        return "ok"
    except WebPushException as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status in (404, 410):
            return "gone"
        logger.warning("Web Push не доставлен (%s): %s", status, error)
        return "error"
    except Exception as error:  # noqa: BLE001 — сеть не должна ломать приложение
        logger.warning("Web Push: ошибка отправки: %s", error)
        return "error"


def notification_payload(announcement: dict[str, Any]) -> dict[str, Any]:
    """Текст уведомления и ссылка, по которой его откроет клиент."""
    body = " ".join(str(announcement.get("body") or "").split())
    if len(body) > MAX_BODY:
        body = body[: MAX_BODY - 1].rstrip() + "…"
    return {
        "title": str(announcement.get("title") or "НЕФТЬ · Анонс")[:120],
        "body": body,
        "category": announcement.get("category"),
        "url": f"/?view=announcements&announcement={announcement.get('id')}",
        "tag": f"announcement-{announcement.get('id')}",
    }


def send_to_user(engine: Engine, user_id: int, payload: dict[str, Any]) -> int:
    """Отправляет на все устройства сотрудника. Возвращает число успешных."""
    rows = user_endpoints(engine, user_id)
    if not rows:
        return 0
    delivered = 0
    for row in rows:
        result = _deliver(engine, row, payload)
        if result == "ok":
            delivered += 1
        elif result == "gone":
            _prune(engine, row["endpoint"])
    return delivered


def recipients(engine: Engine, category: str, exclude_user_id: int | None = None) -> list[int]:
    """Кому слать: активные сотрудники, у кого категория включена в уведомлениях."""
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT DISTINCT s.user_id
            FROM web_push_subscriptions AS s
            JOIN users AS u ON u.id = s.user_id
            WHERE u.is_active = 1 AND u.role IN ('barista', 'senior', 'mentor')
        """)).scalars().all()
    from bot.web import announcements  # локальный импорт: модули ссылаются друг на друга

    result = []
    for user_id in rows:
        if exclude_user_id is not None and int(user_id) == int(exclude_user_id):
            continue
        if category in announcements.get_prefs(engine, int(user_id)):
            result.append(int(user_id))
    return result


def notify_category(engine: Engine, category: str, announcement: dict[str, Any],
                    exclude_user_id: int | None = None) -> int:
    """Рассылает уведомление о новом анонсе. Ошибки только логируются."""
    try:
        payload = notification_payload(announcement)
        delivered = 0
        for user_id in recipients(engine, category, exclude_user_id):
            delivered += send_to_user(engine, user_id, payload)
        if delivered:
            logger.info("Web Push: анонс %s доставлен на %s устройств", announcement.get("id"), delivered)
        return delivered
    except Exception as error:  # noqa: BLE001 — рассылка не должна ломать публикацию
        logger.warning("Web Push: рассылка не удалась: %s", error)
        return 0
