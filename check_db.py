# check_db.py
import sqlite3

def check_database_structure():
    conn = sqlite3.connect('coffee_quality.db')
    cursor = conn.cursor()
    
    # Проверим структуру таблицы
    cursor.execute("PRAGMA table_info(drink_reviews)")
    columns = cursor.fetchall()
    
    print("Структура таблицы drink_reviews:")
    print("-" * 50)
    for col in columns:
        print(f"Столбец: {col[1]}, Тип: {col[2]}")
    
    # Проверим существующие записи
    cursor.execute("SELECT COUNT(*) FROM drink_reviews")
    count = cursor.fetchone()[0]
    print(f"\nКоличество записей: {count}")
    
    conn.close()

if __name__ == "__main__":
    check_database_structure()