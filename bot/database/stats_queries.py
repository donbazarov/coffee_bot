import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

def get_barista_stats_period(start_date: str = None, end_date: str = None) -> List[Tuple]:
    """
    Получает статистику по бариста за указанный период
    Возвращает: имя бариста, кол-во эспрессо, ср. оценка эспрессо, кол-во фильтра, ср. оценка фильтра, 
                кол-во молочных, ср. оценка молочных, общее кол-во, общая ср. оценка
    """
    conn = sqlite3.connect('coffee_quality.db')
    cursor = conn.cursor()
    
    # Базовый запрос
    query = """
    SELECT 
        barista_name,
        -- Эспрессо
        SUM(CASE WHEN category = 'Эспрессо/Фильтр' AND drink_type = 'Эспрессо' THEN 1 ELSE 0 END) as espresso_count,
        ROUND(AVG(CASE WHEN category = 'Эспрессо/Фильтр' AND drink_type = 'Эспрессо' 
                  THEN (balance + bouquet + body + aftertaste)/4.0 END), 2) as espresso_avg,
        
        -- Фильтр
        SUM(CASE WHEN category = 'Эспрессо/Фильтр' AND drink_type = 'Фильтр' THEN 1 ELSE 0 END) as filter_count,
        ROUND(AVG(CASE WHEN category = 'Эспрессо/Фильтр' AND drink_type = 'Фильтр' 
                  THEN (balance + bouquet + body + aftertaste)/4.0 END), 2) as filter_avg,
        
        -- Молочные напитки
        SUM(CASE WHEN category = 'Молочный напиток' THEN 1 ELSE 0 END) as milk_count,
        ROUND(AVG(CASE WHEN category = 'Молочный напиток' 
                  THEN (balance + bouquet + foam + latte_art)/4.0 END), 2) as milk_avg,
        
        -- Общее
        COUNT(*) as total_count,
        ROUND(AVG(CASE 
                  WHEN category = 'Эспрессо/Фильтр' THEN (balance + bouquet + body + aftertaste)/4.0
                  WHEN category = 'Молочный напиток' THEN (balance + bouquet + foam + latte_art)/4.0
                  END), 2) as total_avg
        
    FROM drink_reviews
    WHERE 1=1
    """
    
    params = []
    
    # Добавляем фильтр по дате если указан
    if start_date:
        query += " AND DATE(created_at) >= DATE(?)"
        params.append(start_date)
    
    if end_date:
        query += " AND DATE(created_at) <= DATE(?)"
        params.append(end_date)
    
    query += " GROUP BY barista_name ORDER BY total_avg DESC"
    
    cursor.execute(query, params)
    results = cursor.fetchall()
    conn.close()
    
    return results

def get_period_stats(period: str = 'month') -> List[Tuple]:
    """
    Получает статистику за указанный период
    period: 'week', 'month', 'year'
    """
    today = datetime.now().date()
    
    if period == 'week':
        start_date = (today - timedelta(days=7)).isoformat()
    elif period == 'month':
        start_date = (today - timedelta(days=30)).isoformat()
    elif period == 'year':
        start_date = (today - timedelta(days=365)).isoformat()
    else:
        start_date = None
    
    return get_barista_stats_period(start_date, today.isoformat())

def get_custom_period_stats(start_date: str, end_date: str) -> List[Tuple]:
    """Статистика за произвольный период"""
    return get_barista_stats_period(start_date, end_date)