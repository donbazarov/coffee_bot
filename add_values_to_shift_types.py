from bot.database.models import init_db, User, Schedule, ShiftType, SessionLocal
from bot.config import BotConfig
from bot.database.user_operations import get_user_by_iiko_id, get_user_by_telegram_id, get_user_by_username, create_user
import sqlite3
from datetime import time

conn = sqlite3.connect('coffee_quality.db')
cursor = conn.cursor()

shift_types_data = [
            (time(7, 0), time(15, 0), 'ДЕ', 'утро ДЕ', 'morning'),
            (time(7, 0), time(16, 0), 'УЯ', 'утро УЯ', 'morning'),
            (time(8, 0), time(19, 30), 'ДЕ', 'утропересмен ДЕ', 'hybrid'),
            (time(8, 30), time(15, 0), 'ДЕ', 'утро вых ДЕ', 'morning'),
            (time(8, 30), time(16, 0), 'УЯ', 'утро вых УЯ', 'morning'),
            (time(10, 45), time(22, 30), 'ДЕ', 'пересмен ДЕ', 'hybrid'),
            (time(11, 45), time(23, 30), 'УЯ', 'пересмен УЯ', 'hybrid'),
            (time(14, 45), time(22, 30), 'ДЕ', 'вечер ДЕ', 'evening'),
            (time(15, 45), time(23, 30), 'УЯ', 'вечер УЯ', 'evening'),
        ]
        
cursor.executemany('''
            INSERT INTO shift_types (start_time, end_time, point, name, shift_type)
            VALUES (?, ?, ?, ?, ?)
        ''', shift_types_data)

conn.commit()
conn.close()