from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
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

# Инициализация БД - используем SQLite
engine = create_engine(BotConfig.database_url, connect_args={"check_same_thread": False} if "sqlite" in BotConfig.database_url else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    Base.metadata.create_all(bind=engine)