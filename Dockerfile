# syntax=docker/dockerfile:1

# =============================================================================
#  НЕФТЬ · Coffee Bot — образ веб-сервиса и бота входа
#
#  Лёгкий: slim-база, только runtime-зависимости сайта, без компиляторов.
#  Секреты (.env) в образ НЕ копируются — передаются
#  в рантайме через --env-file или docker compose.
#
#  Сборка:  docker build -t coffee-bot .
#  Запуск:  docker run -d --name coffee-bot --env-file .env -p 127.0.0.1:8001:8001 \
#                    -v "$PWD/data:/app/data" coffee-bot
#
#  База и истории лежат в /app/data (том), поэтому процесс может писать
#  от непривилегированного пользователя: каталог принадлежит ему.
# =============================================================================
FROM python:3.12-slim

# -B не создаёт .pyc, -u не буферизует логи (важно для docker logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Moscow \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8001 \
    DATABASE_URL=sqlite:////app/data/coffee_quality.db \
    NEFT_DATA_DIR=/app/data/stories

WORKDIR /app

# 1) Зависимости отдельным слоем: переустанавливаются только при их изменении.
#    Все пакеты приходят колёсами, поэтому gcc и build-essential не нужны.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 2) Только код приложения — явные COPY вместо `COPY . .`,
#    чтобы .env физически не мог попасть в образ.
COPY bot/ ./bot/
COPY run.py ./

# 3) Непривилегированный пользователь
RUN useradd --system --create-home --uid 10001 coffee \
 && mkdir -p /app/data \
 && chown -R coffee:coffee /app
USER coffee

# 4) SQLite-база и истории гостей переживают пересборку образа
VOLUME ["/app/data"]
EXPOSE 8001

# Проверка живости: /healthz не требует авторизации и не читает БД.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=3).status == 200 else 1)"

CMD ["python", "run.py"]
