"""Локальный запуск веб-сервиса для отладки с телефона (LAN).

Отличается от штатного `run.py` двумя вещами:

1. Слушает `0.0.0.0:8000` — сервис доступен с iPhone/Android в той же Wi-Fi-сети.
2. Для этой сессии отключает `WEB_COOKIE_SECURE`, иначе Safari не отправляет
   Secure-cookie по `http://<LAN-IP>:8000` и вход по коду доступа не держится.

Файл `.env` при этом не меняется: флаг переопределяется уже после импорта
приложения, а host/port задаются напрямую в `uvicorn.run`.

Запуск (из корня проекта, в активированном venv)::

    python neft_run_local.py

Адрес для телефона смотрите в выводе или через `ipconfig` (обычно плюс-минус
`http://192.168.x.x:8000`). Если страница не открывается — разрешите порт в
брандмауэре Windows:

    netsh advfirewall firewall add rule name="neft local 8000" dir=in action=allow protocol=TCP localport=8000

Остановить: Ctrl+C в этом окне, либо найти процесс, слушающий порт 8000,
и остановить его через Task Manager.
"""

import os

import uvicorn

# Импорт приложения первым делом — он сам подтягивает .env
# (bot.config.load_env_file, override=True), поэтому секреты и настройки
# совпадают со штатным запуском.
from bot.web.app import app  # noqa: E402

# Разрешаем сессионную куку по обычному HTTP только для этой сессии.
os.environ["WEB_COOKIE_SECURE"] = "0"

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("NEFT_LOCAL_PORT", "8000")),
        # Один воркер осознанно: SQLite и JSON-файл историй не любят параллельную запись.
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )