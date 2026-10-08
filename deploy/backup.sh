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

# База может лежать в data/ (Docker) или в корне проекта (systemd) —
# ищем в обоих местах.
DB_SOURCE=""
for candidate in "$APP_DIR/data/coffee_quality.db" "$APP_DIR/coffee_quality.db"; do
  if [ -f "$candidate" ]; then
    DB_SOURCE="$candidate"
    break
  fi
done

# Истории гостей и загруженные фото
[ -d "$APP_DIR/data" ] && cp -r "$APP_DIR/data" "$WORK/data"

# Базу кладём поверх — через sqlite3 .backup, чтобы не поймать
# незавершённую транзакцию. В архиве она всегда лежит в data/.
if [ -n "$DB_SOURCE" ]; then
  mkdir -p "$WORK/data"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB_SOURCE" ".backup '$WORK/data/coffee_quality.db'"
  else
    cp "$DB_SOURCE" "$WORK/data/coffee_quality.db"
  fi
  echo "База: $DB_SOURCE"
fi

tar -czf "$DEST/coffee_bot_$STAMP.tar.gz" -C "$WORK" .

# Чистим архивы старше KEEP_DAYS дней
find "$DEST" -name 'coffee_bot_*.tar.gz' -mtime "+$KEEP_DAYS" -delete

echo "Бэкап готов: $DEST/coffee_bot_$STAMP.tar.gz"
