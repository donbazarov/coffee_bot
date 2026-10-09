"""ORM-модели и инициализация схемы базы.

Веб-слой читает и пишет данные сырым SQL, но ORM-модели остаются источником
схемы: `init_db()` создаёт недостающие таблицы на пустой базе. Оставлены
только нужные веб-сервису таблицы — `users`, `shift_types`, `schedule`.

Модели и код старого Telegram-бота удалены как legacy.
"""

from sqlalchemy import Boolean, create_engine, Column, Integer, String, DateTime, Date, Time, Index, ForeignKey, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime
from pathlib import Path

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
    display_name = Column(String(100))  # отображаемое имя (в графике и интерфейсе)
    avatar_rev = Column(Integer, default=0, nullable=False)  # версия аватара; 0 = файла нет
    role = Column(String(50), nullable=False)  # 'barista', 'senior', 'mentor'
    is_active = Column(Integer, default=1)  # 1 - активен, 0 - неактивен
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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


# Инициализация БД - используем SQLite
# timeout=15 — ждать освобождения блокировки, а не падать сразу:
# в проде с одной и той же базой работают два процесса (сайт и бот для входа).
if "sqlite" in BotConfig.database_url:
    _connect_args = {"check_same_thread": False, "timeout": 15}
else:
    _connect_args = {}

engine = create_engine(BotConfig.database_url, connect_args=_connect_args)

# Оценки качества. ORM-модель удалена как legacy — функционал будет переписан
# с нуля, — но веб-дашборд пока читает эту таблицу сырым SQL. Поэтому схема
# создаётся явным DDL: иначе на пустой базе дашборд падал бы с "no such table".
DRINK_REVIEWS_DDL = """
CREATE TABLE IF NOT EXISTS drink_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    respondent_name TEXT NOT NULL,
    barista_name TEXT NOT NULL,
    point TEXT NOT NULL,
    category TEXT NOT NULL,
    drink_type TEXT,
    balance INTEGER,
    bouquet INTEGER,
    body INTEGER,
    aftertaste INTEGER,
    foam INTEGER,
    latte_art INTEGER,
    photo_file_id TEXT,
    comment TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def init_db():
    """Создаёт недостающие таблицы.

    Для SQLite заодно создаёт каталог и сам файл базы. Вызывается на старте
    веб-приложения: миграции читают таблицу `users`, поэтому на пустой базе
    схема должна существовать заранее, иначе старт падает с
    "no such table: users".
    """
    if engine.dialect.name == "sqlite" and engine.url.database:
        Path(engine.url.database).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text(DRINK_REVIEWS_DDL))
