#!/usr/bin/env bash
# =============================================================================
#  НЕФТЬ · Coffee Bot — обновление сайта из GitHub
#
#  Запуск на сервере:
#      cd /opt/coffee_bot && ./deploy/deploy.sh
#
#  Что делает:
#    1. забирает свежий код из ветки main (fast-forward, без конфликтов);
#    2. доустанавливает зависимости из requirements.txt;
#    3. перезапускает systemd-сервис;
#    4. показывает статус, а при падении — последние строки лога.
#
#  База и фото историй не трогаются: они лежат в data/ и не отслеживаются git.
# =============================================================================

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/coffee_bot}"
SERVICE="${SERVICE:-coffee-bot}"
BRANCH="${BRANCH:-main}"
VENV="${VENV:-$APP_DIR/venv}"

cd "$APP_DIR"

echo "==> Обновляю код из origin/$BRANCH"
git fetch --prune origin
git checkout "$BRANCH"
git pull --ff-only "origin/$BRANCH"

echo "==> Обновляю зависимости"
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -r requirements.txt --quiet

echo "==> Проверяю, что код компилируется"
"$VENV/bin/python" -m compileall -q bot run.py

echo "==> Перезапускаю сервис $SERVICE"
sudo systemctl restart "$SERVICE"

sleep 3
if sudo systemctl is-active --quiet "$SERVICE"; then
  echo "==> Готово: $SERVICE работает"
  sudo systemctl status "$SERVICE" --no-pager --lines=5
else
  echo "!! Сервис не поднялся, последние строки лога:"
  sudo journalctl -u "$SERVICE" -n 40 --no-pager
  exit 1
fi
