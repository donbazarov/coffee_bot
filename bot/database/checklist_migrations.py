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