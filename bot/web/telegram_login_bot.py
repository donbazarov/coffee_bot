"""Бот, подтверждающий вход на сайт.

Запуск рядом с сайтом:
    python -m bot.web.telegram_login_bot

Слушает `getUpdates` (long polling) и на команду `/start <токен>` проверяет
пользователя и помечает вход подтверждённым. Пользователю приходит ответ
в приложение Telegram.

Зависимостей нет — только стандартная библиотека, поэтому образ не растёт.

ВАЖНО: одновременно с этим процессом с тем же токеном не должен работать
старый бот (`python -m bot.main`) — Telegram отдаёт обновления только одному
получателю, второй получит ошибку 409.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import urllib.error
import urllib.request

from bot.config import BotConfig, load_env_file
from bot.database.models import engine
from bot.web import telegram_login
from bot.web.auth import register_or_find_telegram_user

logger = logging.getLogger("bot.login")

API_BASE = "https://api.telegram.org/bot{token}/{method}"
LONG_POLL_SECONDS = 30
ERROR_BACKOFF_SECONDS = 5

HELP_TEXT = (
    "Этот бот подтверждает вход в рабочий сайт кофейни НЕФТЬ.\n\n"
    "Откройте сайт, нажмите «Войти через Telegram» — я получу ссылку "
    "и подтвержу вход автоматически."
)

# Что бот отвечает по итогам проверки пользователя
RESPONSES = {
    "approved": "Готово! Возвращайтесь в браузер — вход выполнен.",
    "pending": "Заявка сохранена. Как только администратор выдаст вам роль, войдите снова.",
    "disabled": "Доступ к вашей учётной записи отключён. Обратитесь к администратору.",
    "conflict": "Не удалось однозначно сопоставить аккаунт. Обратитесь к администратору.",
}


def _call(token: str, method: str, params: dict | None = None, timeout: int = 40) -> dict:
    """Вызов метода Bot API. Без сторонних зависимостей — только urllib."""
    url = API_BASE.format(token=token, method=method)
    body = json.dumps(params).encode("utf-8") if params else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _send(token: str, chat_id: int, text: str) -> None:
    try:
        _call(token, "sendMessage", {"chat_id": chat_id, "text": text}, timeout=15)
    except Exception as error:  # noqa: BLE001
        logger.warning("Не удалось отправить сообщение в чат %s: %s", chat_id, error)


def _handle_start(token: str, message: dict, login_token: str) -> None:
    """Обрабатывает /start: либо подсказка, либо подтверждение входа."""
    chat_id = message["chat"]["id"]
    sender = message.get("from") or {}

    if not login_token:
        _send(token, chat_id, HELP_TEXT)
        return

    user, status = register_or_find_telegram_user(sender)
    if status == "approved" and user:
        telegram_login.approve_login_request(engine, login_token, int(user["id"]))
        _send(token, chat_id, RESPONSES["approved"])
        logger.info("Вход подтверждён: %s (user_id=%s)", user["name"], user["id"])
    else:
        telegram_login.deny_login_request(engine, login_token, status)
        _send(token, chat_id, RESPONSES.get(status, RESPONSES["conflict"]))
        logger.info("Вход отклонён (%s), telegram_id=%s", status, sender.get("id"))


def main() -> int:
    load_env_file()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )

    token = BotConfig.token
    if not token:
        logger.error("Не задан TELEGRAM_BOT_TOKEN — боту нечем работать.")
        return 1

    telegram_login.initialize_login_schema(engine)

    try:
        me = _call(token, "getMe", timeout=15)
        logger.info("Бот @%s готов принимать входы", me.get("result", {}).get("username"))
    except urllib.error.HTTPError as error:
        logger.error("Telegram отклонил токен (HTTP %s). Проверьте TELEGRAM_BOT_TOKEN.", error.code)
        return 1
    except Exception as error:  # noqa: BLE001
        logger.error("Не удалось связаться с Telegram: %s", error)
        logger.error("Проверьте, что с этого сервера доступен https://api.telegram.org")
        return 1

    offset = 0
    logger.info("Слушаю обновления (long polling, %s с)", LONG_POLL_SECONDS)

    while True:
        try:
            payload = _call(
                token,
                "getUpdates",
                {"offset": offset, "timeout": LONG_POLL_SECONDS, "allowed_updates": ["message"]},
                timeout=LONG_POLL_SECONDS + 15,
            )
        except urllib.error.HTTPError as error:
            if error.code == 409:
                logger.error(
                    "Конфликт 409: с этим токеном уже работает другой getUpdates. "
                    "Остановите старый бот (python -m bot.main), иначе вход работать не будет."
                )
            elif error.code == 401:
                logger.error("Неверный токен бота (401). Проверьте TELEGRAM_BOT_TOKEN.")
                return 1
            else:
                logger.warning("Telegram вернул HTTP %s — повтор через %s с", error.code, ERROR_BACKOFF_SECONDS)
            time.sleep(ERROR_BACKOFF_SECONDS)
            continue
        except Exception as error:  # noqa: BLE001
            logger.warning("Связь потеряна: %s — повтор через %s с", error, ERROR_BACKOFF_SECONDS)
            time.sleep(ERROR_BACKOFF_SECONDS)
            continue

        for update in payload.get("result", []):
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message")
            if not message:
                continue
            text = (message.get("text") or "").strip()
            if not text.startswith("/start"):
                continue
            parts = text.split(maxsplit=1)
            login_token = parts[1].strip() if len(parts) > 1 else ""
            try:
                _handle_start(token, message, login_token)
            except Exception as error:  # noqa: BLE001
                logger.exception("Ошибка обработки /start: %s", error)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
