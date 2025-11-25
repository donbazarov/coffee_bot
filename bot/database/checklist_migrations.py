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
    
def remove_point_from_templates():
    """Удаляем колонку point из таблицы checklist_templates (если она существует)"""
    db = SessionLocal()
    try:
        # Проверяем, существует ли колонка point
        cursor = db.execute("PRAGMA table_info(checklist_templates)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'point' not in columns:
            logger.info("✅ Колонка point уже удалена из checklist_templates")
            return
        
        logger.info("🔄 Начинаем миграцию: удаляем колонку point из checklist_templates")
        
        # Создаем временную таблицу без колонки point
        db.execute("""
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
        """)
        
        # Копируем данные (исключаем колонку point)
        db.execute("""
            INSERT INTO checklist_templates_new 
            (id, day_of_week, shift_type, task_description, order_index, is_active, created_at, updated_at)
            SELECT id, day_of_week, shift_type, task_description, order_index, is_active, created_at, updated_at
            FROM checklist_templates
        """)
        
        # Удаляем старую таблицу
        db.execute("DROP TABLE checklist_templates")
        
        # Переименовываем новую таблицу
        db.execute("ALTER TABLE checklist_templates_new RENAME TO checklist_templates")
        
        db.commit()
        logger.info("✅ Колонка point успешно удалена из checklist_templates")
        
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Ошибка при миграции: {e}")
        raise
    finally:
        db.close()
        
def remove_point_from_hybrid_assignments():
    """Удаляем колонку point из таблицы hybrid_shift_assignments (если она существует)"""
    db = SessionLocal()
    try:
        # Проверяем, существует ли колонка point
        cursor = db.execute("PRAGMA table_info(hybrid_shift_assignments)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'point' not in columns:
            logger.info("✅ Колонка point уже удалена из hybrid_shift_assignments")
            return
        
        logger.info("🔄 Начинаем миграцию: удаляем колонку point из hybrid_shift_assignments")
        
        # Создаем временную таблицу без колонки point
        db.execute("""
            CREATE TABLE hybrid_shift_assignments_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_of_week INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Копируем данные (исключаем колонку point)
        db.execute("""
            INSERT INTO hybrid_shift_assignments_new 
            (id, day_of_week, created_at)
            SELECT id, day_of_week, created_at
            FROM hybrid_shift_assignments
        """)
        
        # Удаляем старую таблицу
        db.execute("DROP TABLE hybrid_shift_assignments")
        
        # Переименовываем новую таблицу
        db.execute("ALTER TABLE hybrid_shift_assignments_new RENAME TO hybrid_shift_assignments")
        
        db.commit()
        logger.info("✅ Колонка point успешно удалена из hybrid_shift_assignments")
        
    except Exception as e:
        db.rollback()
        logger.error(f"❌ Ошибка при миграции hybrid_shift_assignments: {e}")
        raise
    finally:
        db.close()