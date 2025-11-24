from sqlalchemy import Boolean, create_engine, Column, Integer, String, DateTime, Text, Date, Time, Index, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from bot.config import BotConfig

Base = declarative_base()

class User(Base):
    """Модель пользователя с ролями"""
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    iiko_id = Column(Integer, unique=True)  # Внутренний ID из Iiko
    telegram_id = Column(Integer, unique=True)  # ID в Telegram
    telegram_username = Column(String(100), unique=True)
    role = Column(String(50), nullable=False)  # 'barista', 'senior', 'mentor'
    is_active = Column(Integer, default=1)  # 1 - активен, 0 - неактивен
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class DrinkReview(Base):
    __tablename__ = 'drink_reviews'
    
    id = Column(Integer, primary_key=True)
    respondent_name = Column(String(100), nullable=False)
    barista_name = Column(String(100), nullable=False)
    point = Column(String(50), nullable=False)
    category = Column(String(50), nullable=False)
    drink_type = Column(String(50))
    balance = Column(Integer)
    bouquet = Column(Integer)
    body = Column(Integer)
    aftertaste = Column(Integer)
    foam = Column(Integer)
    latte_art = Column(Integer)
    photo_path = Column(String(255))
    comment = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

class ShiftType(Base):
    """Модель типов смен"""
    __tablename__ = 'shift_types'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    start_time = Column(Time, nullable=False)  # время прихода
    end_time = Column(Time, nullable=False)  # время ухода
    point = Column(String(10), nullable=False)  # 'УЯ' или 'ДЕ'
    name = Column(String(100), nullable=False)  # название смены (например, "утро ДЕ")
    shift_type = Column(String(20), nullable=False)  # 'morning', 'hybrid', 'evening'
    created_at = Column(DateTime, default=datetime.utcnow)

class Schedule(Base):
    """Модель расписания смен"""
    __tablename__ = 'schedule'
    
    shift_id = Column(Integer, primary_key=True, autoincrement=True)
    shift_date = Column(Date, nullable=False)  # формат: YYYY-MM-DD
    iiko_id = Column(String(50), nullable=False)  # ID из iiko (может быть строкой)
    shift_type_id = Column(Integer, ForeignKey('shift_types.id'), nullable=False)  # ID типа смены
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    source = Column(String(20), default='sheets')  # 'sheets', 'swap', 'manual'
    version = Column(Integer, default=1)
    is_active = Column(Boolean, default=True)
    
    # Связь с типом смены
    shift_type_obj = relationship("ShiftType", lazy="joined")
    
    # Индексы для быстрого поиска
    __table_args__ = (
        Index('idx_shift_date', 'shift_date'),
        Index('idx_iiko_id', 'iiko_id'),
        Index('idx_shift_date_iiko', 'shift_date', 'iiko_id'),
        Index('idx_shift_type_id', 'shift_type_id'),
    )

class ChecklistTemplate(Base):
    """Шаблоны заданий для чек-листов"""
    __tablename__ = 'checklist_templates'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    point = Column(String(10), nullable=False)  # 'УЯ' или 'ДЕ'
    day_of_week = Column(Integer, nullable=False)  # 0-6 (пн-вс)
    shift_type = Column(String(20), nullable=False)  # 'morning', 'evening' (для пересмена не создаем отдельные)
    task_description = Column(String(500), nullable=False)
    order_index = Column(Integer, default=0)  # порядок отображения
    is_active = Column(Integer, default=1)  # 1 - активен, 0 - неактивен
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class HybridAssignmentTask(Base):
    """Связь между распределением и задачами (многие ко многим)"""
    __tablename__ = 'hybrid_assignment_tasks'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    assignment_id = Column(Integer, ForeignKey('hybrid_shift_assignments.id'), nullable=False)
    task_id = Column(Integer, ForeignKey('checklist_templates.id'), nullable=False)
    shift_type = Column(String(20), nullable=False)  # 'morning' или 'evening'
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Связи
    assignment = relationship("HybridShiftAssignment", back_populates="assigned_tasks")
    task = relationship("ChecklistTemplate")
    
    __table_args__ = (
        Index('idx_assignment_task', 'assignment_id', 'task_id'),
    )

class HybridShiftAssignment(Base):
    """Распределение задач для пересменов"""
    __tablename__ = 'hybrid_shift_assignments'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    point = Column(String(10), nullable=False)  # 'УЯ' или 'ДЕ'
    day_of_week = Column(Integer, nullable=False)  # 0-6 (пн-вс)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Убираем старые связи и добавляем новую
    assigned_tasks = relationship("HybridAssignmentTask", back_populates="assignment", cascade="all, delete-orphan")

class ChecklistLog(Base):
    """Лог выполнения чек-листов"""
    __tablename__ = 'checklist_logs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    task_id = Column(Integer, ForeignKey('checklist_templates.id'), nullable=False)
    shift_date = Column(Date, nullable=False)
    shift_type = Column(String(20), nullable=False)  # 'morning', 'hybrid', 'evening'
    point = Column(String(10), nullable=False)
    completed_at = Column(DateTime, default=datetime.utcnow)
    completed_by_user_id = Column(Integer, ForeignKey('users.id'))  # кто выполнил (для синхронизации)
    
    # Связи
    user = relationship("User", foreign_keys=[user_id])
    task = relationship("ChecklistTemplate")
    completed_by = relationship("User", foreign_keys=[completed_by_user_id])
    
    # Индексы
    __table_args__ = (
        Index('idx_checklist_log_date_user', 'shift_date', 'user_id'),
        Index('idx_checklist_log_date_point', 'shift_date', 'point'),
        Index('idx_checklist_log_task', 'task_id'),
    )


# Инициализация БД - используем SQLite
engine = create_engine(BotConfig.database_url, connect_args={"check_same_thread": False} if "sqlite" in BotConfig.database_url else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    Base.metadata.create_all(bind=engine)