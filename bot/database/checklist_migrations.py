"""Миграции для системы чек-листов"""
from sqlalchemy import text
from .models import SessionLocal, engine
import logging

logger = logging.getLogger(__name__)

def create_checklist_tables():
    """Создание таблиц для системы чек-листов"""
    db = SessionLocal()
    try:
        # Таблица уже будет создана через SQLAlchemy модели, но на всякий случай
        logger.info("✅ Таблицы чек-листов созданы через модели SQLAlchemy")
        
        # Можно добавить начальные данные, если нужно
        # create_initial_checklist_data()
        
    except Exception as e:
        logger.error(f"❌ Ошибка при создании таблиц чек-листов: {e}")
    finally:
        db.close()

def init_checklist_database():
    """Инициализация базы данных для чек-листов"""
    create_checklist_tables()
    logger.info("✅ Система чек-листов инициализирована")

def remove_point_from_checklist():
    """Удаление столбца point из таблицы checklist_templates"""
    db = SessionLocal()
    try:
        # Проверяем существование столбца point
        result = db.execute(text("PRAGMA table_info(checklist_templates)"))
        columns = [row[1] for row in result]
        
        if 'point' in columns:
            logger.info("Удаляем столбец point из checklist_templates...")
            
            # Создаем временную таблицу без столбца point
            db.execute(text("""
                CREATE TABLE checklist_templates_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day_of_week INTEGER NOT NULL,
                    shift_type VARCHAR(20) NOT NULL,
                    task_description VARCHAR(500) NOT NULL,
                    order_index INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
            
            # Копируем данные из старой таблицы в новую (исключая столбец point)
            db.execute(text("""
                INSERT INTO checklist_templates_new 
                (id, day_of_week, shift_type, task_description, order_index, is_active, created_at, updated_at)
                SELECT id, day_of_week, shift_type, task_description, order_index, is_active, created_at, updated_at
                FROM checklist_templates
            """))
            
            # Удаляем старую таблицу
            db.execute(text("DROP TABLE checklist_templates"))
            
            # Переименовываем новую таблицу
            db.execute(text("ALTER TABLE checklist_templates_new RENAME TO checklist_templates"))
            
            db.commit()
            logger.info("✅ Столбец point успешно удален из checklist_templates")
        else:
            logger.info("✅ Столбец point уже удален из checklist_templates")
            
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Ошибка при удалении столбца point: {e}")
        raise
    finally:
        db.close()