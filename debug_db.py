import sqlite3
import os

print("🔍 Начинаем проверку базы данных...")

# Проверяем существование файла
db_path = 'coffee_quality.db'
if not os.path.exists(db_path):
    print(f"❌ Файл базы данных '{db_path}' не существует!")
    exit()

print(f"✅ Файл базы данных найден: {db_path}")

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Проверяем существование таблицы
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='drink_reviews'")
    table_exists = cursor.fetchone()
    
    if not table_exists:
        print("❌ Таблица 'drink_reviews' не существует!")
        conn.close()
        exit()
    
    print("✅ Таблица 'drink_reviews' существует")
    
    # Проверяем структуру таблицы
    cursor.execute("PRAGMA table_info(drink_reviews)")
    columns = cursor.fetchall()
    
    print("\n📋 Структура таблицы:")
    for col in columns:
        print(f"  - {col[1]} ({col[2]})")
    
    # Проверяем данные
    cursor.execute("SELECT COUNT(*) FROM drink_reviews")
    count = cursor.fetchone()[0]
    
    print(f"\n📊 Количество записей: {count}")
    
    if count > 0:
        cursor.execute("SELECT * FROM drink_reviews ORDER BY id DESC LIMIT 3")
        records = cursor.fetchall()
        
        print("\n📝 Последние записи:")
        for record in records:
            print(f"  {record}")
    
    conn.close()
    print("\n✅ Проверка завершена")
    
except Exception as e:
    print(f"❌ Ошибка при проверке базы данных: {e}")