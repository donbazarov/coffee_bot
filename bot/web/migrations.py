import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from bot.database.models import engine

logger = logging.getLogger(__name__)
MIGRATION_NAME = "20261007_move_legacy_telegram_id_to_iiko_id"


def _create_backup(database_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = database_path.with_name(f"{database_path.stem}.backup_{timestamp}{database_path.suffix}")
    with closing(sqlite3.connect(database_path)) as source, closing(sqlite3.connect(backup_path)) as backup:
        source.backup(backup)
    return backup_path


def migrate_legacy_telegram_ids(database_engine: Engine = engine) -> bool:
    if database_engine.dialect.name != "sqlite":
        raise RuntimeError("The legacy Telegram ID migration currently supports SQLite only")

    database_path = Path(database_engine.url.database or "").resolve()
    if not database_path.is_file():
        raise RuntimeError(f"SQLite database does not exist: {database_path}")

    with database_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS web_schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        already_applied = connection.execute(
            text("SELECT 1 FROM web_schema_migrations WHERE name = :name"),
            {"name": MIGRATION_NAME},
        ).first()
        if already_applied:
            connection.commit()
            return False

        conflicting_pairs = connection.execute(text("""
            SELECT COUNT(*) FROM users
            WHERE telegram_id IS NOT NULL AND iiko_id IS NOT NULL AND telegram_id != iiko_id
        """)).scalar_one()
        colliding_iiko_ids = connection.execute(text("""
            SELECT COUNT(*)
            FROM users AS source
            JOIN users AS target ON target.iiko_id = source.telegram_id AND target.id != source.id
            WHERE source.telegram_id IS NOT NULL AND source.iiko_id IS NULL
        """)).scalar_one()
        if conflicting_pairs or colliding_iiko_ids:
            connection.rollback()
            raise RuntimeError(
                "Legacy ID migration stopped: conflicting iiko_id/telegram_id values need manual review"
            )

        backup_path = _create_backup(database_path)
        moved_count = connection.execute(text("""
            UPDATE users SET iiko_id = telegram_id
            WHERE telegram_id IS NOT NULL AND iiko_id IS NULL
        """)).rowcount
        cleared_count = connection.execute(text("""
            UPDATE users SET telegram_id = NULL
            WHERE telegram_id IS NOT NULL AND iiko_id = telegram_id
        """)).rowcount
        connection.execute(
            text("INSERT INTO web_schema_migrations (name) VALUES (:name)"),
            {"name": MIGRATION_NAME},
        )
        connection.commit()

    logger.info(
        "Legacy ID migration applied: moved %s iiko IDs, cleared %s legacy values; backup: %s",
        moved_count,
        cleared_count,
        backup_path,
    )
    return True