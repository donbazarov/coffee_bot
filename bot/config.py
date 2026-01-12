import os
from dataclasses import dataclass

@dataclass
class BotConfig:
    # MAIN TOKEN
    token: str = "8531765653:AAEWDaM2crEA1ZMLoNFRLFxC-48CAxwMKOE"
    # TEST TOKEN
    #token: str = "8239602085:AAEGTNVaNh9OX8Em4PxNsdZn3fii1QRLqfk"
    
    # ИСПОЛЬЗУЕМ SQLITE вместо PostgreSQL
    database_url: str = "sqlite:///coffee_quality.db"
    
    # Списки пользователей
    baristas = [
        {'name': 'Настя', 'id': 222559, 'role': 'barista'},
        {'name': 'Богдана', 'id': 901953, 'role': 'barista'},
        {'name': 'Польза', 'id': 400481, 'role': 'barista'},
        {'name': 'Катя', 'id': 901927, 'role': 'barista'},
        {'name': 'Паша', 'id': 20441, 'role': 'barista'},
        {'name': 'Аида', 'id': 400487, 'role': 'barista'},
        {'name': 'Ева', 'id': 70622, 'role': 'barista'},
        {'name': 'Мердан', 'id': 222556, 'role': 'barista'},
        {'name': 'Стас', 'id': 909333, 'role': 'barista'},
        {'name': 'Камила', 'id': 222668, 'role': 'barista'},
    ]
    
    respondents = [
        {'name': 'Ди', 'id': 222557, 'role': 'respondent', 'telegram_username': 'drodonit'},
        {'name': 'Дон', 'id': 90944, 'role': 'respondent', 'telegram_username': 'don22487'},
        {'name': 'Софа', 'id': 400482, 'role': 'respondent', 'telegram_username': 'SophiaLavkraft'}
    ]
    
    points = ['ДЕ', 'УЯ']