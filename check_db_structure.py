import sqlite3

def check_db_structure():
    conn = sqlite3.connect('coffee_quality.db')
    cursor = conn.cursor()
    
    # Проверим структуру таблицы
    cursor.execute("PRAGMA table_info(drink_reviews)")
    columns = cursor.fetchall()
    
    print("Структура таблицы drink_reviews:")
    for column in columns:
        print(f"- {column[1]} ({column[2]})")
    
    # Проверим существующие записи
    cursor.execute("SELECT * FROM drink_reviews")
    records = cursor.fetchall()
    
    print(f"\nВсего записей: {len(records)}")
    for record in records:
        print(record)
    
    conn.close()

if __name__ == "__main__":
    check_db_structure()