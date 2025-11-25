"""Миграции базы данных"""
from bot.database.models import init_db, User, Schedule, ShiftType, SessionLocal, engine, Base, HybridAssignmentTask
from bot.config import BotConfig
from bot.database.user_operations import get_user_by_iiko_id, get_user_by_telegram_id, get_user_by_username, create_user
from .checklist_migrations import init_checklist_database
import sqlite3
from datetime import time
import logging

logger = logging.getLogger(__name__)

def migrate_users_from_config():
    """Миграция пользователей из config.py в БД"""
    db = SessionLocal()
    try:
        # Проверяем, есть ли уже пользователи в БД
        existing_users = db.query(User).count()
        if existing_users > 0:
            print("✅ Пользователи уже мигрированы")
            return
        
        print("🔄 Начинаем миграцию пользователей...")
        
        # Мигрируем бариста
        for barista in BotConfig.baristas:
            # Проверяем, не существует ли уже такой пользователь по Iiko ID
            existing = get_user_by_iiko_id(barista.get('id'))
            if not existing:
                try:
                    create_user(
                        name=barista['name'],
                        iiko_id=barista.get('id'),  # Это Iiko ID, а не Telegram ID
                        role='barista'
                    )
                    print(f"✅ Добавлен бариста: {barista['name']}")
                except Exception as e:
                    print(f"⚠️ Ошибка при добавлении бариста {barista['name']}: {e}")
        
        # Мигрируем наставников (respondents -> mentors)
        for respondent in BotConfig.respondents:
            # Проверяем, не существует ли уже такой пользователь по Iiko ID
            existing = get_user_by_iiko_id(respondent.get('id'))
            if not existing:
                try:
                    create_user(
                        name=respondent['name'],
                        iiko_id=respondent.get('id'),  # Это Iiko ID
                        telegram_username=respondent.get('telegram_username'),
                        role='mentor'  # respondents становятся mentors
                    )
                    print(f"✅ Добавлен наставник: {respondent['name']}")
                except Exception as e:
                    print(f"⚠️ Ошибка при добавлении наставника {respondent['name']}: {e}")
        
        print("✅ Миграция пользователей завершена!")
        
    except Exception as e:
        print(f"❌ Ошибка при миграции: {e}")
        # Не поднимаем исключение, чтобы не сломать запуск бота
    finally:
        db.close()

def migrate_add_iiko_id_column():
    """Добавляет колонку iiko_id в таблицу users, если её нет, и переносит данные из telegram_id"""
    try:
        conn = sqlite3.connect('coffee_quality.db')
        cursor = conn.cursor()
        
        # Проверяем, существует ли колонка iiko_id
        cursor.execute("PRAGMA table_info(users)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'iiko_id' not in columns:
            print("🔄 Добавляем колонку iiko_id в таблицу users...")
            cursor.execute("ALTER TABLE users ADD COLUMN iiko_id INTEGER")
            conn.commit()
            print("✅ Колонка iiko_id добавлена")
            
            # Переносим данные из telegram_id в iiko_id для существующих пользователей
            print("🔄 Переносим данные из telegram_id в iiko_id...")
            cursor.execute("UPDATE users SET iiko_id = telegram_id WHERE iiko_id IS NULL AND telegram_id IS NOT NULL")
            conn.commit()
            print("✅ Данные перенесены")
        else:
            print("✅ Колонка iiko_id уже существует")
            # Проверяем, нужно ли перенести данные
            cursor.execute("SELECT COUNT(*) FROM users WHERE iiko_id IS NULL AND telegram_id IS NOT NULL")
            count = cursor.fetchone()[0]
            if count > 0:
                print(f"🔄 Переносим данные из telegram_id в iiko_id для {count} пользователей...")
                cursor.execute("UPDATE users SET iiko_id = telegram_id WHERE iiko_id IS NULL AND telegram_id IS NOT NULL")
                conn.commit()
                print("✅ Данные перенесены")
        
        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка при миграции колонки iiko_id: {e}")

def migrate_create_shift_types_table():
    """Создает таблицу shift_types и заполняет её данными"""
    try:
        conn = sqlite3.connect('coffee_quality.db')
        cursor = conn.cursor()
        
        # Проверяем, существует ли таблица shift_types
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='shift_types'")
        if cursor.fetchone():
            print("✅ Таблица shift_types уже существует")
            
            # Проверяем, есть ли данные в таблице
            cursor.execute("SELECT COUNT(*) FROM shift_types")
            count = cursor.fetchone()[0]
            if count == 0:
                print("🔄 Таблица пуста, заполняем данными...")
                _fill_shift_types_table(cursor)
            else:
                print(f"✅ В таблице уже есть {count} записей")
                
            conn.close()
            return
        
        print("🔄 Создаем таблицу shift_types...")
        cursor.execute('''
            CREATE TABLE shift_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TEXT NOT NULL,  -- Изменено с TIME на TEXT для SQLite
                end_time TEXT NOT NULL,    -- Изменено с TIME на TEXT для SQLite
                point TEXT NOT NULL,
                name TEXT NOT NULL,
                shift_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Заполняем данными
        _fill_shift_types_table(cursor)
        
        conn.commit()
        conn.close()
        print("✅ Таблица shift_types создана и заполнена данными")
    except Exception as e:
        print(f"⚠️ Ошибка при создании таблицы shift_types: {e}")
        import traceback
        print(f"Детали ошибки: {traceback.format_exc()}")

def _fill_shift_types_table(cursor):
    """Заполняет таблицу shift_types данными (использует строки вместо time)"""
    shift_types_data = [
        # Формат: start_time, end_time, point, name, shift_type
        ('07:00', '15:00', 'ДЕ', 'утро ДЕ', 'morning'),
        ('07:00', '16:00', 'УЯ', 'утро УЯ', 'morning'),
        ('08:00', '19:30', 'ДЕ', 'утропересмен ДЕ', 'hybrid'),
        ('08:30', '15:00', 'ДЕ', 'утро вых ДЕ', 'morning'),
        ('08:30', '16:00', 'УЯ', 'утро вых УЯ', 'morning'),
        ('10:45', '22:30', 'ДЕ', 'пересмен ДЕ', 'hybrid'),
        ('11:45', '23:30', 'УЯ', 'пересмен УЯ', 'hybrid'),
        ('14:45', '22:30', 'ДЕ', 'вечер ДЕ', 'evening'),
        ('15:45', '23:30', 'УЯ', 'вечер УЯ', 'evening'),
    ]
    
    cursor.executemany('''
        INSERT INTO shift_types (start_time, end_time, point, name, shift_type)
        VALUES (?, ?, ?, ?, ?)
    ''', shift_types_data)
    
    print(f"✅ Добавлено {len(shift_types_data)} типов смен")

def migrate_fix_shift_types_data():
    """Исправляет данные в таблице shift_types, если они были добавлены некорректно"""
    try:
        conn = sqlite3.connect('coffee_quality.db')
        cursor = conn.cursor()
        
        # Проверяем, существует ли таблица
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='shift_types'")
        if not cursor.fetchone():
            print("✅ Таблица shift_types не существует, пропускаем исправление")
            conn.close()
            return
        
        # Проверяем структуру таблицы
        cursor.execute("PRAGMA table_info(shift_types)")
        columns = cursor.fetchall()
        print("Структура таблицы shift_types:")
        for col in columns:
            print(f"  {col[1]} ({col[2]})")
        
        # Проверяем, есть ли данные
        cursor.execute("SELECT COUNT(*) FROM shift_types")
        count = cursor.fetchone()[0]
        print(f"Количество записей в shift_types: {count}")
        
        if count == 0:
            print("🔄 Таблица пуста, заполняем данными...")
            _fill_shift_types_table(cursor)
            conn.commit()
        
        # Показываем пример данных для проверки
        cursor.execute("SELECT * FROM shift_types LIMIT 3")
        sample_data = cursor.fetchall()
        print("Пример данных из shift_types:")
        for row in sample_data:
            print(f"  {row}")
        
        conn.close()
        print("✅ Проверка данных shift_types завершена")
    except Exception as e:
        print(f"⚠️ Ошибка при проверке данных shift_types: {e}")
        import traceback
        print(f"Детали ошибки: {traceback.format_exc()}")

def migrate_update_schedule_table():
    """Обновляет таблицу schedule для использования shift_type_id"""
    try:
        conn = sqlite3.connect('coffee_quality.db')
        cursor = conn.cursor()
        
        # Проверяем, существует ли таблица schedule
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schedule'")
        if not cursor.fetchone():
            print("✅ Таблица schedule не существует, будет создана через SQLAlchemy")
            conn.close()
            return
        
        # Проверяем структуру таблицы
        cursor.execute("PRAGMA table_info(schedule)")
        columns = {column[1]: column for column in cursor.fetchall()}
        
        # Если есть старые колонки (point, shift_type, shift_start, shift_end), но нет shift_type_id
        if 'shift_type_id' not in columns and 'point' in columns:
            print("🔄 Мигрируем таблицу schedule на новую структуру...")
            
            # Создаем новую таблицу с правильной структурой
            cursor.execute('''
                CREATE TABLE schedule_new (
                    shift_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shift_date DATE NOT NULL,
                    iiko_id TEXT NOT NULL,
                    shift_type_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (shift_type_id) REFERENCES shift_types(id)
                )
            ''')
            
            # Мигрируем данные из старой таблицы
            # Для каждой смены находим соответствующий shift_type_id по времени
            cursor.execute('SELECT shift_id, shift_date, iiko_id, shift_start, shift_end FROM schedule')
            old_shifts = cursor.fetchall()
            
            migrated_count = 0
            for shift_id, shift_date, iiko_id, shift_start, shift_end in old_shifts:
                # Ищем соответствующий shift_type_id
                cursor.execute('''
                    SELECT id FROM shift_types 
                    WHERE start_time = ? AND end_time = ?
                ''', (shift_start, shift_end))
                result = cursor.fetchone()
                
                if result:
                    shift_type_id = result[0]
                    cursor.execute('''
                        INSERT INTO schedule_new (shift_date, iiko_id, shift_type_id, created_at, updated_at)
                        SELECT shift_date, iiko_id, ?, created_at, updated_at
                        FROM schedule WHERE shift_id = ?
                    ''', (shift_type_id, shift_id))
                    migrated_count += 1
                else:
                    print(f"⚠️ Не найден shift_type для смены {shift_id} ({shift_start} - {shift_end})")
            
            # Удаляем старую таблицу и переименовываем новую
            cursor.execute('DROP TABLE schedule')
            cursor.execute('ALTER TABLE schedule_new RENAME TO schedule')
            
            # Создаем индексы
            cursor.execute('CREATE INDEX idx_shift_date ON schedule(shift_date)')
            cursor.execute('CREATE INDEX idx_iiko_id ON schedule(iiko_id)')
            cursor.execute('CREATE INDEX idx_shift_date_iiko ON schedule(shift_date, iiko_id)')
            cursor.execute('CREATE INDEX idx_shift_type_id ON schedule(shift_type_id)')
            
            conn.commit()
            print(f"✅ Мигрировано {migrated_count} смен на новую структуру")
        elif 'shift_type_id' in columns:
            print("✅ Таблица schedule уже в новой структуре")
        else:
            print("✅ Таблица schedule будет создана через SQLAlchemy")
        
        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка при миграции таблицы schedule: {e}")

def migrate_create_schedule_table():
    """Создает таблицу schedule если её нет (legacy функция для обратной совместимости)"""
    pass  # Теперь используется SQLAlchemy для создания таблиц

def migrate_hybrid_assignments():
    """Миграция для обновления структуры распределений"""
    db = SessionLocal()
    try:
        HybridAssignmentTask.__table__.create(bind=engine, checkfirst=True)
        print(f"✅ Таблица hybrid_assignment_tasks создана")
        
    except Exception as e:
        print(f"⚠️ Ошибка при миграции: {e}")
    finally:
        db.close()

def migrate_schedule_table():
    """Добавляет новые поля в таблицу schedule"""
    conn = sqlite3.connect('coffee_quality.db')
    cursor = conn.cursor()
    
    try:
        # Проверяем существование столбцов
        cursor.execute("PRAGMA table_info(schedule)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'source' not in columns:
            cursor.execute("ALTER TABLE schedule ADD COLUMN source TEXT DEFAULT 'sheets'")
            print("✅ Добавлен столбец 'source'")
        
        if 'version' not in columns:
            cursor.execute("ALTER TABLE schedule ADD COLUMN version INTEGER DEFAULT 1")
            print("✅ Добавлен столбец 'version'")
        
        if 'is_active' not in columns:
            cursor.execute("ALTER TABLE schedule ADD COLUMN is_active BOOLEAN DEFAULT 1")
            print("✅ Добавлен столбец 'is_active'")
        
        conn.commit()
        print("✅ Миграция успешно завершена")
        
    except Exception as e:
        print(f"❌ Ошибка миграции: {e}")
        conn.rollback()
    finally:
        conn.close()

def migrate_secret_santa_table():
    """Создание таблицы для тайного санты 2026 через прямой SQL"""
    try:
        conn = sqlite3.connect('coffee_quality.db')
        cursor = conn.cursor()

        # Проверяем, существует ли таблица
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='secret_santa_2026'")
        table_exists = cursor.fetchone()

        if not table_exists:
            print("🔄 Создаем таблицу secret_santa_2026...")
            cursor.execute('''
                CREATE TABLE secret_santa_2026 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_username TEXT NOT NULL,
                    wishlist TEXT DEFAULT '',
                    is_participant BOOLEAN DEFAULT 0,
                    santa_of TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Создаем индексы
            cursor.execute('CREATE INDEX idx_santa_username ON secret_santa_2026(telegram_username)')
            cursor.execute('CREATE INDEX idx_santa_participant ON secret_santa_2026(is_participant)')
            
            conn.commit()
            print("✅ Таблица secret_santa_2026 создана")
        else:
            print("✅ Таблица secret_santa_2026 уже существует")

        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка при создании таблицы secret_santa_2026: {e}")
        
def init_database():
    """Инициализация БД с миграцией"""
    # Создаем таблицы через SQLAlchemy
    init_db()
    # Мигрируем структуру (добавляем колонку iiko_id если нужно)
    migrate_add_iiko_id_column()
    # Создаем таблицу shift_types и заполняем данными
    migrate_create_shift_types_table()
    # Обновляем таблицу schedule на новую структуру
    migrate_update_schedule_table()
    # Мигрируем пользователей
    migrate_users_from_config()
    # Мигрируем чек-листы
    init_checklist_database()
    # Мигрируем распределения задач для пересменов
    migrate_hybrid_assignments()
    # Обновляем таблицу schedule на новую структуру
    migrate_schedule_table()
    # Создаем таблицу для Санты
    migrate_secret_santa_table()
    try:
        from .checklist_migrations import remove_point_from_hybrid_assignments
        remove_point_from_hybrid_assignments()
    except ImportError as e:
        logger.warning(f"⚠️ Модуль миграции checklist_migrations не доступен: {e}")
    except Exception as e:
        logger.error(f"❌ Ошибка при миграции checklist_migrations: {e}")
    try:
        from .checklist_migrations import remove_point_from_templates
        remove_point_from_templates()
    except ImportError as e:
        logger.warning(f"⚠️ Модуль миграции checklist_migrations не доступен: {e}")
    except Exception as e:
        logger.error(f"❌ Ошибка при миграции checklist_migrations: {e}")
        