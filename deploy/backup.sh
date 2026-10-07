#!/usr/bin/env bash
# =============================================================================
#  НЕФТЬ · Coffee Bot — резервная копия данных
#
#  Копирует SQLite-базу и каталог data/ (истории и фото) в один архив.
#
#  Разовый запуск:
#      cd /opt/coffee_bot && ./deploy/backup.sh
#
#  Ежедневно в 04:00 (crontab -e):
#      0 4 * * * /opt/coffee_bot/deploy/backup.sh >> /var/log/coffee-bot-backup.log 2>&1
# =============================================================================

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/coffee_bot}"
DEST="${DEST:-/var/backups/coffee_bot}"
KEEP_DAYS="${KEEP_DAYS:-30}"
STAMP="$(date +%Y%m%d_%H%M%S)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$DEST"

# Базу снимаем через sqlite3 .backup: так не поймать незавершённую транзакцию.
if command -v sqlite3 >/dev/null 2>&1 && [ -f "$APP_DIR/coffee_quality.db" ]; then
  sqlite3 "$APP_DIR/coffee_quality.db" ".backup '$WORK/coffee_quality.db'"
elif [ -f "$APP_DIR/coffee_quality.db" ]; then
  cp "$APP_DIR/coffee_quality.db" "$WORK/coffee_quality.db"
fi

# Истории гостей и загруженные фото
[ -d "$APP_DIR/data" ] && cp -r "$APP_DIR/data" "$WORK/data"

tar -czf "$DEST/coffee_bot_$STAMP.tar.gz" -C "$WORK" .

# Чистим архивы старше KEEP_DAYS дней
find "$DEST" -name 'coffee_bot_*.tar.gz' -mtime "+$KEEP_DAYS" -delete

echo "Бэкап готов: $DEST/coffee_bot_$STAMP.tar.gz"
