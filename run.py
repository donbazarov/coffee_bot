"""Точка входа веб-сервиса НЕФТЬ.

Сначала читаем .env рядом с проектом, затем поднимаем uvicorn.
Один и тот же файл .env работает и локально, и на сервере под systemd.
"""

import os
from pathlib import Path


def _load_env() -> None:
    """Подхватывает .env, не перезатирая уже заданные переменные окружения."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)


if __name__ == "__main__":
    _load_env()

    import uvicorn

    uvicorn.run(
        "bot.web.app:app",
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("WEB_PORT", "8001")),
        # Один воркер осознанно: SQLite и JSON-файл историй не любят
        # параллельную запись из нескольких процессов.
        workers=1,
        # За nginx доверяем заголовкам X-Forwarded-* только от localhost.
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )