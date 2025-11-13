# migrate_database.py
import sqlite3
import os

def migrate_database():
    print("🔄 Миграция базы данных...")
    
    # Создаем резервную копию
    if os.path.exists('coffee_quality.db'):
        os.rename('coffee_quality.db', 'coffee_quality.db.backup')
        print("✅ Создана резервная копия базы данных")
    
    # Создаем новую базу с правильной структурой
    conn = sqlite3.connect('coffee_quality.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE drink_reviews (
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
            photo_file_id TEXT,  -- ТОЛЬКО photo_file_id, без photo_path
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    print("✅ База данных мигрирована на новую структуру")
    print("📝 Старая база сохранена как coffee_quality.db.backup")

if __name__ == "__main__":
    migrate_database()