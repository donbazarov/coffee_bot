"""Модуль для работы с Google Sheets API"""
import gspread
from google.oauth2.service_account import Credentials
import logging
from datetime import datetime, timedelta, time
from typing import List, Dict, Optional, Tuple
import os
from bot.database.schedule_operations import get_shift_type_by_time_strings

logger = logging.getLogger(__name__)

# ID таблицы из ссылки
SPREADSHEET_ID = "1-NpGX3AfsBiOGHCBlCWD_6lTsCdMFIMiWgbkPVE0z0Q"

# Области доступа для Google Sheets API
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets.readonly',
    'https://www.googleapis.com/auth/drive.readonly'
]

def get_google_client():
    """Получить клиент Google Sheets"""
    try:
        credentials_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'credentials.json')
        if not os.path.exists(credentials_path):
            raise FileNotFoundError(f"Файл credentials.json не найден по пути: {credentials_path}")
        
        creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        logger.error(f"Ошибка при подключении к Google Sheets: {e}")
        raise

def get_worksheet_by_month(client, month_name: str):
    """Получить лист по названию месяца (например, 'Декабрь 24')"""
    try:
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        # Пробуем найти лист с точным названием
        try:
            worksheet = spreadsheet.worksheet(month_name)
            return worksheet
        except gspread.exceptions.WorksheetNotFound:
            # Пробуем найти лист, содержащий название месяца
            for sheet in spreadsheet.worksheets():
                if month_name.lower() in sheet.title.lower():
                    return sheet
            raise ValueError(f"Лист с названием '{month_name}' не найден")
    except Exception as e:
        logger.error(f"Ошибка при получении листа '{month_name}': {e}")
        raise

def parse_month_name(month_name: str) -> Tuple[int, int]:
    """Парсит название месяца (например, 'Декабрь 24') и возвращает (месяц, год)"""
    month_names = {
        'январь': 1, 'февраль': 2, 'март': 3, 'апрель': 4,
        'май': 5, 'июнь': 6, 'июль': 7, 'август': 8,
        'сентябрь': 9, 'октябрь': 10, 'ноябрь': 11, 'декабрь': 12
    }
    
    parts = month_name.lower().strip().split()
    if len(parts) < 2:
        raise ValueError(f"Неверный формат названия месяца: {month_name}")
    
    month_str = parts[0]
    year_str = parts[1]
    
    month = month_names.get(month_str)
    if not month:
        raise ValueError(f"Неизвестный месяц: {month_str}")
    
    # Год может быть в формате '24' или '2024'
    if len(year_str) == 2:
        year = 2000 + int(year_str)
    else:
        year = int(year_str)
    
    return month, year

def determine_shift_type(start_time: str) -> str:
    """Определить тип смены по времени начала"""
    if not start_time or start_time in ['ВЫХ', 'ОТПУСК']:
        return None
    
    try:
        # Парсим время (формат: HH:MM)
        time_parts = start_time.split(':')
        if len(time_parts) != 2:
            return None
        
        hour = int(time_parts[0])
        minute = int(time_parts[1])
        total_minutes = hour * 60 + minute
        
        # Определяем тип смены
        if start_time in ['7:00', '8:30']:
            return 'morning'
        elif start_time in ['14:45', '15:45']:
            return 'evening'
        elif start_time in ['8:00', '10:45', '11:45']:
            return 'hybrid'
        else:
            # Дополнительная логика для других времен
            if total_minutes < 600:  # До 10:00
                return 'morning'
            elif total_minutes >= 870:  # После 14:30
                return 'evening'
            else:
                return 'hybrid'
    except Exception:
        return None

def parse_schedule_from_sheet(month_name: str) -> List[Dict]:
    """Парсит расписание из Google Sheets для указанного месяца"""
    shifts = []
    
    try:
        client = get_google_client()
        worksheet = get_worksheet_by_month(client, month_name)
        
        # Парсим название месяца для получения года
        month, year = parse_month_name(month_name)
        
        # Читаем данные
        # Строка 1: даты (C1:BL1)
        # Строка 2: дни недели (C2:BL2)
        # Строки 4-30: данные сотрудников (A4:A30 - iiko_id, C4:BL30 - смены)
        
        # Получаем iiko_id из столбца A (строки 4-30)
        iiko_ids = worksheet.col_values(1)[3:]  # Пропускаем первые 3 строки (заголовки)
        
        # Получаем даты из строки 1 (столбцы C и далее)
        dates_row = worksheet.row_values(1)[2:]  # Пропускаем столбцы A и B
        
        # Получаем данные для каждой строки сотрудника
        for row_idx, iiko_id in enumerate(iiko_ids, start=4):
            if not iiko_id or not str(iiko_id).strip():
                continue  # Пропускаем пустые строки
            
            iiko_id = str(iiko_id).strip()
            
            # Получаем данные строки (начиная с столбца C)
            row_data = worksheet.row_values(row_idx)[2:]
            
            # Обрабатываем пары столбцов (приход/уход)
            for col_idx in range(0, len(row_data), 2):
                if col_idx >= len(dates_row):
                    break
                
                date_str = dates_row[col_idx] if col_idx < len(dates_row) else None
                if not date_str or not str(date_str).strip().isdigit():
                    continue
                
                try:
                    day = int(str(date_str).strip())
                    shift_date = datetime(year, month, day).date()
                except (ValueError, TypeError):
                    continue
                
                # Получаем время прихода и ухода
                start_time = row_data[col_idx] if col_idx < len(row_data) else None
                end_time = row_data[col_idx + 1] if col_idx + 1 < len(row_data) else None
                
                # Пропускаем выходные и отпуска
                if not start_time or str(start_time).strip() in ['ВЫХ', 'ОТПУСК', '']:
                    continue
                
                if not end_time or str(end_time).strip() in ['ВЫХ', 'ОТПУСК', '']:
                    continue
                
                # Проверяем, что это время (формат HH:MM)
                start_time_str = str(start_time).strip()
                end_time_str = str(end_time).strip()
                
                if ':' not in start_time_str or ':' not in end_time_str:
                    continue
                
                # Нормализуем формат времени (добавляем ведущие нули если нужно)
                start_time_str = _normalize_time_format(start_time_str)
                end_time_str = _normalize_time_format(end_time_str)
                
                logger.info(f"Обрабатываем смену: {start_time_str} - {end_time_str} для {iiko_id}")
                
                # Определяем shift_type_id по времени начала и окончания
                shift_type_obj = get_shift_type_by_time_strings(start_time_str, end_time_str)
                if not shift_type_obj:
                    logger.warning(f"Не найден тип смены для времени {start_time_str} - {end_time_str}")
                    continue
                
                shifts.append({
                    'shift_date': shift_date,
                    'iiko_id': iiko_id,
                    'shift_type_id': shift_type_obj.id
                })
        
        logger.info(f"Успешно распарсено {len(shifts)} смен из листа '{month_name}'")
        return shifts
        
    except Exception as e:
        logger.error(f"Ошибка при парсинге расписания из листа '{month_name}': {e}")
        raise

def _normalize_time_format(time_str: str) -> str:
    """Нормализует формат времени к HH:MM"""
    try:
        parts = time_str.split(':')
        if len(parts) != 2:
            return time_str
            
        hour = parts[0].zfill(2)  # Добавляем ведущий ноль если нужно
        minute = parts[1].zfill(2)
        
        return f"{hour}:{minute}"
    except:
        return time_str

def get_current_month_name() -> str:
    """Получить название текущего месяца в формате таблицы"""
    now = datetime.now()
    month_names = {
        1: 'Январь', 2: 'Февраль', 3: 'Март', 4: 'Апрель',
        5: 'Май', 6: 'Июнь', 7: 'Июль', 8: 'Август',
        9: 'Сентябрь', 10: 'Октябрь', 11: 'Ноябрь', 12: 'Декабрь'
    }
    year = now.year % 100
    return f"{month_names[now.month]} {year}"

def get_next_month_name() -> str:
    """Получить название следующего месяца в формате таблицы"""
    next_month = datetime.now() + timedelta(days=32)
    next_month = next_month.replace(day=1)
    month_names = {
        1: 'Январь', 2: 'Февраль', 3: 'Март', 4: 'Апрель',
        5: 'Май', 6: 'Июнь', 7: 'Июль', 8: 'Август',
        9: 'Сентябрь', 10: 'Октябрь', 11: 'Ноябрь', 12: 'Декабрь'
    }
    year = next_month.year % 100
    return f"{month_names[next_month.month]} {year}"

