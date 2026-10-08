"""Точка входа веб-сервиса НЕФТЬ.

Настройки и секреты читаются из .env в корне проекта (см. bot/config.py),
поэтому один и тот же файл работает и локально, и на сервере под systemd.
"""

import os


if __name__ == "__main__":
    from bot.config import load_env_file

    load_env_file()  # .env нужен до чтения WEB_HOST и WEB_PORT

    import uvicorn

    uvicorn.run(
        "bot.web.app:app",
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("WEB_PORT", "8001")),
        # Один воркер осознанно: SQLite и JSON-файл историй не любят
        # параллельную запись из нескольких процессов.
        workers=1,
        # За прокси (nginx, Docker) доверяем заголовкам X-Forwarded-*.
        # По умолчанию только от localhost; в контейнере задаётся FORWARDED_ALLOW_IPS.
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )