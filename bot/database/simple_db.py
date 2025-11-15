import sqlite3
import os
from datetime import datetime
from bot.database.models import init_db as init_models_db
from bot.database.migrations import init_database

def init_db():
    """Инициализация БД (создание таблиц и миграция данных)"""
    # Используем SQLAlchemy для создания таблиц
    init_models_db()
    # Мигрируем структуру и данные
    try:
        init_database()
    except Exception as e:
        print(f"⚠️ Предупреждение при миграции: {e}")
        # Продолжаем работу даже если миграция не удалась

def save_review(review_data):
    conn = sqlite3.connect('coffee_quality.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO drink_reviews 
        (respondent_name, barista_name, point, category, drink_type, balance, bouquet, body, aftertaste, foam, latte_art, photo_file_id, comment)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        review_data['respondent_name'],
        review_data['barista_name'], 
        review_data['point'],
        review_data['category'],
        review_data.get('drink_type'),
        review_data.get('balance'),
        review_data.get('bouquet'),
        review_data.get('body'),
        review_data.get('aftertaste'),
        review_data.get('foam'),
        review_data.get('latte_art'),
        review_data.get('photo_file_id'),  # 🆕 Сохраняем file_id
        review_data.get('comment', '-')
    ))
    
    conn.commit()
    conn.close()