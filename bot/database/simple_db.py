import sqlite3
import os
from datetime import datetime

def init_db():
    conn = sqlite3.connect('coffee_quality.db')
    cursor = conn.cursor()
    
    cursor.execute('''
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
    ''')
    
    conn.commit()
    conn.close()

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