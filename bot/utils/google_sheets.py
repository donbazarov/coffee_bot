"""Модуль для работы с Google Sheets API"""
import gspread
from google.oauth2.service_account import Credentials
from bot.config import BotConfig
import logging
from datetime import datetime, timedelta, time, date
from typing import List, Dict, Optional, Tuple
import os
import re
from bot.database.schedule_operations import (
    get_shift_type_by_time_strings, get_shifts_by_date_range
    )

logger = logging.getLogger(__name__)

# Цвета для точек (RGB в формате 0-1)
POINT_COLORS = {
    'ДЕ': {'red': 0.3, 'green': 0.5, 'blue': 0.91},  # Голубой
    'УЯ': {'red': 0.91, 'green': 0.18, 'blue': 0}    # Красный
}

# ID таблицы из ссылки
SPREADSHEET_ID = "1-NpGX3AfsBiOGHCBlCWD_6lTsCdMFIMiWgbkPVE0z0Q"

# Области доступа для Google Sheets API
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
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
            uppercase_month_name = month_name.upper()
            worksheet = spreadsheet.worksheet(uppercase_month_name)
            logger.info(f"Найден лист с точным названием: {uppercase_month_name}")
            return worksheet
        except gspread.exceptions.WorksheetNotFound:
            logger.warning(f"Лист с названием '{uppercase_month_name}' не найден, пробуем исходный формат...")
        
        try:
            worksheet = spreadsheet.worksheet(month_name)
            logger.info(f"Найден лист с точным названием: {month_name}")
            return worksheet
        except gspread.exceptions.WorksheetNotFound:
            logger.warning(f"Лист с точным названием '{month_name}' не найден, ищем похожие...")
            
            # Пробуем найти лист, содержащий название месяца
            available_sheets = [sheet.title for sheet in spreadsheet.worksheets()]
            logger.info(f"Доступные листы: {available_sheets}")
            
            # Ищем частичное совпадение
            for sheet in spreadsheet.worksheets():
                # Приводим к нижнему регистру для поиска
                if month_name.lower() in sheet.title.lower():
                    logger.info(f"Найден похожий лист: {sheet.title}")
                    return sheet
            
            # Если не нашли, пробуем альтернативные форматы
            alternative_names = [
                month_name.replace(" ", ""),  # "Ноябрь25"
                month_name.replace("25", "2025"),  # "Ноябрь 2025"
                month_name.split()[0]  # "Ноябрь"
            ]
            
            for alt_name in alternative_names:
                try:
                    worksheet = spreadsheet.worksheet(alt_name)
                    logger.info(f"Найден лист с альтернативным названием: {alt_name}")
                    return worksheet
                except gspread.exceptions.WorksheetNotFound:
                    continue
            
            # Если ничего не нашли, выводим ошибку с доступными листами
            available_titles = [sheet.title for sheet in spreadsheet.worksheets()]
            error_msg = f"Лист '{month_name}' не найден. Доступные листы: {', '.join(available_titles)}"
            logger.error(error_msg)
            raise ValueError(error_msg)
            
    except Exception as e:
        logger.error(f"Ошибка при получении листа '{month_name}': {e}")
        raise

def parse_month_name(month_name: str) -> Tuple[int, int]:
    """Парсит название месяца (например, 'Декабрь 24') и возвращает (месяц, год)"""
    month_names = {
        'январь': 1, 'февраль': 2, 'март': 3, 'апрель': 4,
        'май': 5, 'июнь': 6, 'июль': 7, 'август': 8,
        'сентябрь': 9, 'октябрь': 10, 'ноябрь': 11, 'декабрь': 12,
        'january': 1, 'february': 2, 'march': 3, 'april': 4,
        'may': 5, 'june': 6, 'july': 7, 'august': 8,
        'september': 9, 'october': 10, 'november': 11, 'december': 12
    }
    
    # Приводим к нижнему региву и убираем лишние пробелы
    month_name_clean = month_name.lower().strip()
    
    # Пробуем разные варианты разделителей
    parts = re.split(r'[\s_\-]+', month_name_clean)
    if len(parts) < 2:
        raise ValueError(f"Неверный формат названия месяца: {month_name}")
    
    month_str = parts[0]
    year_str = parts[1]
    
    month = month_names.get(month_str)
    if not month:
        # Пробуем найти частичное совпадение
        for name, num in month_names.items():
            if name.startswith(month_str) or month_str.startswith(name):
                month = num
                break
        
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

def parse_schedule_from_sheet(
    month_name: str,
    preserve_swaps: bool = True,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None
) -> List[Dict]:
    """Умный парсинг расписания с сохранением замен"""
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
        
        today = date.today()
        
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
                    
                    if start_date and shift_date < start_date:
                        continue

                    if end_date and shift_date > end_date:
                        continue

                    if shift_date < today:
                        continue
                    
                except (ValueError, TypeError) as e:
                    logger.warning(f"Ошибка парсинга даты '{date_str}': {e}")
                    continue  # Пропускаем некорректные даты
                
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
                
        if preserve_swaps:
            preserved_swaps = preserve_existing_swaps(
                month_name,
                shifts,
                start_date=start_date,
                end_date=end_date
            )
            shifts.extend(preserved_swaps)
        
        logger.info(f"Успешно распарсено {len(shifts)} смен из листа '{month_name}'")
        return shifts
        
    except Exception as e:
        logger.error(f"Ошибка при парсинге расписания из листа '{month_name}': {e}")
        raise

def preserve_existing_swaps(
    month_name: str,
    new_shifts: List[Dict],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None
) -> List[Dict]:
    """Сохраняет существующие замены при парсинге"""
    try:
        # Получаем существующие смены из БД для этого месяца
        month, year = parse_month_name(month_name)
        month_start = date(year, month, 1)
        if month == 12:
            month_end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(year, month + 1, 1) - timedelta(days=1)
        
        existing_shifts = get_shifts_by_date_range(month_start, month_end)
        
        # Фильтруем замены (source='swap')
        swap_shifts = [s for s in existing_shifts if getattr(s, 'source', 'sheets') == 'swap']
        
        preserved = []
        for swap_shift in swap_shifts:
            # Проверяем, не перезаписывается ли замена новым парсингом
            is_overwritten = any(
                s['shift_date'] == swap_shift.shift_date and 
                s['iiko_id'] == swap_shift.iiko_id 
                for s in new_shifts
            )
            
            if not is_overwritten:
                if start_date and swap_shift.shift_date < start_date:
                    continue
                if end_date and swap_shift.shift_date > end_date:
                    continue
                preserved.append({
                    'shift_date': swap_shift.shift_date,
                    'iiko_id': swap_shift.iiko_id,
                    'shift_type_id': swap_shift.shift_type_id,
                    'source': 'swap'  # Помечаем как замену
                })
        
        logger.info(f"Сохранено {len(preserved)} замен при парсинге")
        return preserved
        
    except Exception as e:
        logger.error(f"Ошибка при сохранении замен: {e}")
        return []

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
    now = datetime.now()
    next_month_number = 1 if now.month == 12 else now.month + 1
    next_year = now.year + (1 if now.month == 12 else 0)
    month_names = {
        1: 'Январь', 2: 'Февраль', 3: 'Март', 4: 'Апрель',
        5: 'Май', 6: 'Июнь', 7: 'Июль', 8: 'Август',
        9: 'Сентябрь', 10: 'Октябрь', 11: 'Ноябрь', 12: 'Декабрь'
    }
    year = next_year % 100
    return f"{month_names[next_month_number]} {year}"

def get_sheet_client():
    """Получить клиент для работы с Google Sheets (с правами записи)"""
    try:
        # ИСПОЛЬЗУЕМ ТЕ ЖЕ SCOPES И CREDENTIALS, ЧТО И В get_google_client
        credentials_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'credentials.json')
        if not os.path.exists(credentials_path):
            raise FileNotFoundError(f"Файл credentials.json не найден по пути: {credentials_path}")
        
        creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        logger.error(f"Ошибка при подключении к Google Sheets: {e}")
        raise
    
def get_worksheet(month_name: str):
    """Получить рабочий лист по названию месяца"""
    try:
        client = get_sheet_client()
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        return spreadsheet.worksheet(month_name)
    except Exception as e:
        logger.error(f"Ошибка при получении листа {month_name}: {e}")
        raise
    
def find_cell_coordinates(worksheet, iiko_id: str, target_date: date) -> Optional[Tuple[int, int]]:
    """
    Найти координаты ячейки для конкретного сотрудника и даты
    Используем ту же логику, что и при парсинге: вычисляем столбец по дню месяца
    """
    try:
        logger.info(f"Поиск координат для {iiko_id} на {target_date}")
        
        # Получаем все данные
        all_data = worksheet.get_all_values()
        logger.info(f"Размер данных: {len(all_data)} строк, {len(all_data[0]) if all_data else 0} колонок")
        
        if not all_data or len(all_data) < 4:
            logger.error("Недостаточно данных в таблице")
            return None
        
        # Ищем сотрудника по iiko_id (колонка A, начиная с строки 4)
        target_iiko = str(iiko_id)
        employee_row = None
        
        for row_idx, row in enumerate(all_data[3:], start=4):  # строки с 4 и далее
            if row and row[0] == target_iiko:
                employee_row = row_idx
                logger.info(f"Найден сотрудник в строке {employee_row}")
                break
        
        if not employee_row:
            logger.error(f"Сотрудник {target_iiko} не найден в таблице")
            return None
        
        # ВЫЧИСЛЯЕМ СТОЛБЕЦ ПО ДНЮ МЕСЯЦА (как при парсинге)
        # Данные начинаются с колонки C (индекс 2), каждая дата занимает 2 колонки
        day_of_month = target_date.day
        # Первая дата (1 число) начинается с колонки C (индекс 2)
        start_col_index = 2 + (day_of_month - 1) * 2
        
        # Проверяем, что столбец не выходит за пределы
        if start_col_index >= len(all_data[0]):
            logger.error(f"Вычисленный столбец {start_col_index} выходит за пределы таблицы")
            return None
        
        # Gspread использует 1-индексирование, поэтому +1
        start_col = start_col_index + 1
        
        logger.info(f"Координаты найдены: строка {employee_row}, колонка {start_col} (день {day_of_month})")
        return (employee_row, start_col)
        
    except Exception as e:
        logger.error(f"Ошибка при поиске координат для {iiko_id} на {target_date}: {e}")
        return None
    
def get_month_name(target_date: date) -> str:
    """Получить название месяца для листа Google Sheets"""
    month_names = {
        1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
        5: "Май", 6: "Июнь", 7: "Июль", 8: "Август", 
        9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
    }
    
    month = target_date.month
    year_short = str(target_date.year)[2:]
    return f"{month_names[month]} {year_short}"

def update_shift_in_sheets(iiko_id: str, shift_date: date, start_time: str, end_time: str, point: str) -> bool:
    """
    Обновить смену в Google Sheets
    """
    try:
        month_name = get_month_name(shift_date)
        logger.info(f"Пытаемся получить лист: {month_name}")
        
        client = get_sheet_client()
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        
        # Пробуем найти подходящий лист
        worksheet = None
        available_sheets = [sheet.title for sheet in spreadsheet.worksheets()]
        
        for sheet_title in available_sheets:
            if month_name.lower() in sheet_title.lower():
                worksheet = spreadsheet.worksheet(sheet_title)
                logger.info(f"Используем лист: {sheet_title}")
                break
        
        if not worksheet:
            logger.error(f"Не найден подходящий лист для {month_name}")
            return False
        
        coords = find_cell_coordinates(worksheet, iiko_id, shift_date)
        if not coords:
            logger.error(f"Не найдены координаты для {iiko_id} на {shift_date}")
            return False
        
        row, start_col = coords
        end_col = start_col + 1
        
        # Разъединение ячеек
        try:
            cell_range = f"{gspread.utils.rowcol_to_a1(row, start_col)}:{gspread.utils.rowcol_to_a1(row, end_col)}"
            worksheet.unmerge_cells(cell_range)
            logger.info(f"Разъединены ячейки: {cell_range}")
        except Exception as e:
            logger.info(f"Ячейки не объединены или уже разъединены: {e}")
        
        updates = []
        
        if start_time and end_time:
            start_time_value = start_time
            end_time_value = end_time
            
            updates.extend([
                {
                    'range': f"{gspread.utils.rowcol_to_a1(row, start_col)}",
                    'values': [[start_time_value]]
                },
                {
                    'range': f"{gspread.utils.rowcol_to_a1(row, end_col)}", 
                    'values': [[end_time_value]]
                }
            ])
            
            # Устанавливаем цвет
            color = POINT_COLORS.get(point, POINT_COLORS['ДЕ'])
            format_requests = [
                {
                    'range': f"{gspread.utils.rowcol_to_a1(row, start_col)}:{gspread.utils.rowcol_to_a1(row, end_col)}",
                    'format': {
                        'backgroundColor': color,
                        'numberFormat': {
                            'type': 'TIME',
                            'pattern': 'hh:mm'
                        },
                        'textFormat': {
                            'fontFamily': 'Verdana',
                            'fontSize': 10,
                            'italic': True  # Курсив
                        }
                    }
                }
            ]
            
            # Выполняем форматирование
            worksheet.batch_format(format_requests)
            
            # Устанавливаем формат времени
            #set_time_format_for_cells(worksheet, row, start_col, end_col)
            
        else:
            # Очищаем смену - объединяем ячейки и ставим "ВЫХ" курсивом
            merge_range = f"{gspread.utils.rowcol_to_a1(row, start_col)}:{gspread.utils.rowcol_to_a1(row, end_col)}"
    
            # Объединяем ячейки
            try:
                worksheet.merge_cells(merge_range)
                logger.info(f"Объединены ячейки: {merge_range}")
            except Exception as e:
                logger.warning(f"Не удалось объединить ячейки: {e}")

            # Записываем "ВЫХ" в объединенную ячейку
            updates.append({
                'range': f"{gspread.utils.rowcol_to_a1(row, start_col)}",
                'values': [['ВЫХ']]
            })
    
            # Устанавливаем формат для объединенной ячейки: курсив и светло-серый фон
            format_requests = [
                {
                    'range': merge_range,
                    'format': {
                        'backgroundColor': {'red': 0.85, 'green': 0.85, 'blue': 0.85},  # Светло-серый фон
                        'textFormat': {
                            'fontFamily': 'Verdana',
                            'fontSize': 10,
                            'italic': True  # Курсив
                        },
                        'horizontalAlignment': 'CENTER'  # Выравнивание по центру
                    }
                }
            ]
    
            # Выполняем обновления формата
            worksheet.batch_format(format_requests)
        
        # Выполняем обновления значений
        if updates:
            worksheet.batch_update(updates, value_input_option='USER_ENTERED')
        
        logger.info(f"Успешно обновлена смена в Sheets: {iiko_id} {shift_date}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка при обновлении Sheets для {iiko_id}: {e}")
        return False
        
def set_time_format_for_cells(worksheet, row: int, start_col: int, end_col: int):
    """Установить формат времени для указанных ячеек"""
    try:
        # Создаем запрос на форматирование
        requests = []
        
        # Форматируем обе ячейки (время начала и окончания)
        for col in [start_col, end_col]:
            cell_format = {
                "repeatCell": {
                    "range": {
                        "sheetId": worksheet.id,
                        "startRowIndex": row - 1,  # 0-based indexing
                        "endRowIndex": row,
                        "startColumnIndex": col - 1,
                        "endColumnIndex": col
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "TIME",
                                "pattern": "hh:mm:ss"  # 24-часовой формат без ведущих нулей
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat"
                }
            }
            requests.append(cell_format)
        
        # Выполняем запросы
        if requests:
            worksheet.spreadsheet.batch_update({"requests": requests})
            logger.info(f"Установлен формат времени для ячеек: строка {row}, колонки {start_col}-{end_col}")
            
    except Exception as e:
        logger.error(f"Ошибка при установке формата времени: {e}")

def sync_swap_to_sheets(swap_data: Dict) -> bool:
    """
    Синхронизировать замену в Google Sheets
    swap_data: {
        'from_employee': {'iiko_id': '', 'old_shift': shift_obj, 'new_shift': shift_obj},
        'to_employee': {'iiko_id': '', 'old_shift': shift_obj, 'new_shift': shift_obj}
    }
    """
    try:
        results = []
        
        # Обрабатываем сотрудника, который отдает смену
        from_emp = swap_data['from_employee']
        if from_emp['old_shift']:
            # Очищаем старую смену
            success = update_shift_in_sheets(
                iiko_id=from_emp['iiko_id'],
                shift_date=from_emp['old_shift'].shift_date,
                start_time=None,
                end_time=None,
                point=None
            )
            results.append(success)
        
        # Обрабатываем сотрудника, который принимает смену
        to_emp = swap_data['to_employee'] 
        if to_emp['new_shift'] and to_emp['new_shift'].shift_type_obj:
            success = update_shift_in_sheets(
                iiko_id=to_emp['iiko_id'],
                shift_date=to_emp['new_shift'].shift_date,
                start_time=to_emp['new_shift'].shift_type_obj.start_time.strftime("%H:%M"),
                end_time=to_emp['new_shift'].shift_type_obj.end_time.strftime("%H:%M"),
                point=to_emp['new_shift'].shift_type_obj.point
            )
            results.append(success)
        
        return all(results)
        
    except Exception as e:
        logger.error(f"Ошибка при синхронизации замены: {e}")
        return False
