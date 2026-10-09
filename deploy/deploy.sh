#!/usr/bin/env bash
# =============================================================================
#  НЕФТЬ · Coffee Bot — обновление сайта из GitHub
#
#  Запуск на сервере:
#      cd /opt/coffee_bot && ./deploy/deploy.sh
#
#  Что делает:
#    1. забирает свежий код из ветки main (fast-forward, без конфликтов);
#    2. пересобирает и перезапускает сервис — в Docker или через systemd,
#       смотря как сайт развёрнут (режим определяется автоматически);
#    3. дожидается ответа приложения и показывает логи, если оно не поднялось.
#
#  Принудительно выбрать режим:
#      MODE=docker  ./deploy/deploy.sh
#      MODE=systemd ./deploy/deploy.sh
#
#  Данные не трогаются: база и истории лежат в coffee_quality.db и data/.
# =============================================================================

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/coffee_bot}"
SERVICE="${SERVICE:-coffee-bot}"
BRANCH="${BRANCH:-main}"
VENV="${VENV:-$APP_DIR/venv}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8001/healthz}"

# Абсолютный путь к самому скрипту — нужен, чтобы перезапустить себя после
# обновления кода. Считаем его до `cd`, иначе относительный путь потеряется.
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

cd "$APP_DIR"

# --- 1. Код ------------------------------------------------------------------
echo "==> Обновляю код из origin/$BRANCH"

# Запоминаем версию скрипта до обновления. Если слияние принесёт новую версию,
# продолжать работу по старому тексту нельзя: bash читает файл по мере
# выполнения. Ниже мы просто перезапустим себя уже в новой редакции.
SELF_HASH_BEFORE="$(git hash-object -- "$SELF" 2>/dev/null || true)"

git fetch --prune origin
git checkout "$BRANCH"

# Важно: именно `merge origin/main`, а не `pull origin/main`.
# У `git pull` первый аргумент — имя удалённого репозитория, поэтому
# `git pull origin/main` ищет remote с таким названием и падает с
# "does not appear to be a git repository".
if ! git merge --ff-only "origin/$BRANCH"; then
  echo "!! Не удалось обновить код: скорее всего на сервере есть локальные правки."
  echo "   Посмотреть: git status"
  echo "   Сбросить:   git checkout -- ."
  exit 1
fi

# Самовосстановление: если этим слиянием обновился и сам deploy.sh —
# перезапускаемся, чтобы дальше работала новая логика. На втором проходе
# хеши совпадут, поэтому цикла не будет.
SELF_HASH_AFTER="$(git hash-object -- "$SELF" 2>/dev/null || true)"
if [ -n "$SELF_HASH_BEFORE" ] && [ "$SELF_HASH_BEFORE" != "$SELF_HASH_AFTER" ]; then
  echo "==> Обновилась и версия deploy.sh — перезапускаю скрипт"
  exec bash "$SELF" "$@"
fi

# --- 2. Режим запуска --------------------------------------------------------
detect_mode() {
  if [ -n "${MODE:-}" ]; then echo "$MODE"; return; fi
  if [ -f "$APP_DIR/docker-compose.yml" ] && command -v docker >/dev/null 2>&1 \
     && docker compose version >/dev/null 2>&1; then
    echo "docker"
  else
    echo "systemd"
  fi
}

MODE="$(detect_mode)"
echo "==> Режим запуска: $MODE"

if [ "$MODE" = "docker" ]; then
  # Сборка отдельным шагом: пока собирается образ, работающий контейнер
  # продолжает обслуживать сайт. Пересоздание занимает секунды, а не минуты.
  echo "==> Собираю образ (сайт в это время работает)"
  docker compose build
  echo "==> Пересоздаю контейнеры"
  docker compose up -d --remove-orphans
else
  echo "==> Обновляю зависимости (только веб-сервис)"
  "$VENV/bin/pip" install --upgrade pip --quiet
  "$VENV/bin/pip" install -r requirements.txt --quiet

  echo "==> Проверяю, что код компилируется"
  "$VENV/bin/python" -m compileall -q bot run.py

  echo "==> Перезапускаю сервис $SERVICE"
  sudo systemctl restart "$SERVICE"
fi

# --- 3. Проверка, что приложение ожило ---------------------------------------
echo "==> Жду ответа приложения на $HEALTH_URL"
for _ in $(seq 1 45); do
  if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
    echo "==> Готово: сайт работает"
    exit 0
  fi
  sleep 2
done

echo "!! Приложение не ответило за 90 секунд. Последние логи:"
if [ "$MODE" = "docker" ]; then
  docker compose logs --tail=50 || true
else
  sudo journalctl -u "$SERVICE" -n 50 --no-pager || true
fi
exit 1
