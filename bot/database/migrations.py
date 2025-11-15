"""Миграции базы данных"""
from bot.database.models import init_db, User, SessionLocal
from bot.config import BotConfig
from bot.database.user_operations import get_user_by_iiko_id, get_user_by_telegram_id, get_user_by_username, create_user
import sqlite3

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

def init_database():
    """Инициализация БД с миграцией"""
    # Создаем таблицы
    init_db()
    # Мигрируем структуру (добавляем колонку iiko_id если нужно)
    migrate_add_iiko_id_column()
    # Мигрируем пользователей
    migrate_users_from_config()

