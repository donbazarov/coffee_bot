"""Операции для статистики чек-листов"""
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func, extract, case
from typing import List, Dict, Optional, Tuple
from datetime import date, datetime, timedelta
from collections import defaultdict
import logging

from .models import SessionLocal, User, Schedule, ShiftType, ChecklistTemplate, ChecklistLog
from .checklist_operations import get_tasks_for_shift, get_completed_tasks_for_shift

logger = logging.getLogger(__name__)

def get_individual_stats(start_date: date, end_date: date, user_id: Optional[int] = None) -> List[Dict]:
    """
    Индивидуальная статистика по сотрудникам
    Возвращает список словарей с данными по каждому сотруднику и дню недели
    """
    db = SessionLocal()
    try:
        # Получаем пользователей (активных)
        query = db.query(User).filter(User.is_active == 1)
        if user_id:
            query = query.filter(User.id == user_id)
        users = query.all()
        
        results = []
        
        for user in users:
            if not user.iiko_id:
                continue
                
            # Находим все смены пользователя за период
            shifts = db.query(Schedule).join(ShiftType).filter(
                and_(
                    Schedule.iiko_id == str(user.iiko_id),
                    Schedule.shift_date >= start_date,
                    Schedule.shift_date <= end_date,
                    Schedule.is_active == True
                )
            ).all()
            
            # Группируем смены по дню недели
            shifts_by_weekday = defaultdict(list)
            for shift in shifts:
                weekday = shift.shift_date.weekday()
                shifts_by_weekday[weekday].append(shift)
            
            # Для каждого дня недели считаем статистику
            for weekday, weekday_shifts in shifts_by_weekday.items():
                total_tasks = 0
                completed_tasks = 0
                
                for shift in weekday_shifts:
                    shift_type_obj = shift.shift_type_obj
                    if not shift_type_obj:
                        continue
                    
                    # Получаем задачи для этой смены
                    tasks = get_tasks_for_shift(
                        user.id,
                        shift.shift_date,
                        shift_type_obj.shift_type,
                        shift_type_obj.point
                    )
                    
                    # Получаем выполненные задачи для этой смены
                    completed_task_ids = get_completed_tasks_for_shift(
                        shift.shift_date,
                        shift_type_obj.point
                    )
                    
                    total_tasks += len(tasks)
                    # Считаем выполненные задачи, которые есть в текущем чек-листе
                    completed_in_shift = len([t for t in tasks if t.id in completed_task_ids])
                    completed_tasks += completed_in_shift
                
                completion_percent = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0
                
                results.append({
                    'user_id': user.id,
                    'user_name': user.name,
                    'weekday': weekday,
                    'shift_count': len(weekday_shifts),
                    'total_tasks': total_tasks,
                    'completed_tasks': completed_tasks,
                    'completion_percent': round(completion_percent, 1)
                })
        
        return results
        
    finally:
        db.close()

def get_point_stats(start_date: date, end_date: date, point: Optional[str] = None) -> List[Dict]:
    """
    Статистика по точкам
    Возвращает данные по точкам и дням недели
    """
    db = SessionLocal()
    try:
        # Получаем все смены за период
        query = db.query(Schedule).join(ShiftType).filter(
            and_(
                Schedule.shift_date >= start_date,
                Schedule.shift_date <= end_date,
                Schedule.is_active == True
            )
        )
        
        if point:
            query = query.filter(ShiftType.point == point)
            
        shifts = query.all()
        
        # Группируем смены по точке и дню недели
        shifts_by_point_weekday = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        
        for shift in shifts:
            shift_type_obj = shift.shift_type_obj
            if not shift_type_obj:
                continue
                
            point_name = shift_type_obj.point
            weekday = shift.shift_date.weekday()
            shift_type = shift_type_obj.shift_type
            
            shifts_by_point_weekday[point_name][weekday][shift_type].append(shift)
        
        results = []
        
        for point_name, weekdays_data in shifts_by_point_weekday.items():
            for weekday, shift_types_data in weekdays_data.items():
                morning_stats = _calculate_shift_type_stats(shifts_by_point_weekday[point_name][weekday].get('morning', []))
                evening_stats = _calculate_shift_type_stats(shifts_by_point_weekday[point_name][weekday].get('evening', []))
                hybrid_stats = _calculate_shift_type_stats(shifts_by_point_weekday[point_name][weekday].get('hybrid', []))
                
                results.append({
                    'point': point_name,
                    'weekday': weekday,
                    'morning_avg_completion': morning_stats['avg_completion'],
                    'morning_shift_count': morning_stats['shift_count'],
                    'evening_avg_completion': evening_stats['avg_completion'],
                    'evening_shift_count': evening_stats['shift_count'],
                    'hybrid_avg_completion': hybrid_stats['avg_completion'],
                    'hybrid_shift_count': hybrid_stats['shift_count']
                })
        
        return results
        
    finally:
        db.close()

def _calculate_shift_type_stats(shifts: List[Schedule]) -> Dict:
    """Рассчитать статистику для списка смен одного типа"""
    db = SessionLocal()
    
    if not shifts:
        return {'avg_completion': 0, 'shift_count': 0}
    
    completion_rates = []
    
    for shift in shifts:
        shift_type_obj = shift.shift_type_obj
        if not shift_type_obj:
            continue
            
        # Получаем первого пользователя для этой смены (для получения задач)
        user = db.query(User).filter(User.iiko_id == shift.iiko_id).first()
        if not user:
            continue
            
        tasks = get_tasks_for_shift(
            user.id,
            shift.shift_date,
            shift_type_obj.shift_type,
            shift_type_obj.point
        )
        
        completed_task_ids = get_completed_tasks_for_shift(
            shift.shift_date,
            shift_type_obj.point
        )
        
        total_tasks = len(tasks)
        if total_tasks > 0:
            completed_count = len([t for t in tasks if t.id in completed_task_ids])
            completion_rate = (completed_count / total_tasks) * 100
            completion_rates.append(completion_rate)
    
    avg_completion = sum(completion_rates) / len(completion_rates) if completion_rates else 0
    
    return {
        'avg_completion': round(avg_completion, 1),
        'shift_count': len(shifts)
    }

def get_task_stats(start_date: date, end_date: date, task_id: Optional[int] = None, point: Optional[str] = None) -> List[Dict]:
    """
    Статистика по заданиям
    Возвращает данные по каждому заданию и точке
    """
    db = SessionLocal()
    try:
        # Получаем все шаблоны задач
        query = db.query(ChecklistTemplate).filter(ChecklistTemplate.is_active == 1)
        if task_id:
            query = query.filter(ChecklistTemplate.id == task_id)
        tasks = query.all()
        
        results = []
        
        for task in tasks:
            # Для каждой точки, для которой нужно вывести статистику
            points = [point] if point else ['ДЕ', 'УЯ']
            
            for point_name in points:
                    
                # Находим все смены, где это задание должно было быть
                total_shifts_with_task = 0
                completed_shifts_with_task = 0
                
                # Ищем смены за период в этот день недели и на этой точке
                shifts = db.query(Schedule).join(ShiftType).filter(
                    and_(
                        Schedule.shift_date >= start_date,
                        Schedule.shift_date <= end_date,
                        Schedule.is_active == True,
                        ShiftType.point == point_name,
                        extract('dow', Schedule.shift_date) == task.day_of_week
                    )
                ).all()
                
                for shift in shifts:
                    shift_type_obj = shift.shift_type_obj
                    if not shift_type_obj:
                        continue
                    
                    # Проверяем, входит ли задача в чек-лист этой смены
                    user = db.query(User).filter(User.iiko_id == shift.iiko_id).first()
                    if not user:
                        continue
                    
                    shift_tasks = get_tasks_for_shift(
                        user.id,
                        shift.shift_date,
                        shift_type_obj.shift_type,
                        point_name
                    )
                    
                    # Проверяем, есть ли наша задача в чек-листе
                    task_in_shift = any(t.id == task.id for t in shift_tasks)
                    if not task_in_shift:
                        continue
                    
                    total_shifts_with_task += 1
                    
                    # Проверяем, выполнена ли задача в эту смену
                    completed_task_ids = get_completed_tasks_for_shift(shift.shift_date, point_name)
                    if task.id in completed_task_ids:
                        completed_shifts_with_task += 1
                
                completion_percent = (completed_shifts_with_task / total_shifts_with_task * 100) if total_shifts_with_task > 0 else 0
                
                results.append({
                    'task_id': task.id,
                    'task_description': task.task_description,
                    'point': point_name,
                    'day_of_week': task.day_of_week,
                    'shift_type': task.shift_type,
                    'total_shifts': total_shifts_with_task,
                    'completed_shifts': completed_shifts_with_task,
                    'completion_percent': round(completion_percent, 1)
                })
        
        return results
        
    finally:
        db.close()

def get_detailed_log(target_date: date, point: str) -> List[Dict]:
    """
    Детальный лог выполнения за конкретный день и точку
    Возвращает информацию о каждом задании и его выполнении
    """
    db = SessionLocal()
    try:
        # Получаем все смены на эту дату и точку
        shifts = db.query(Schedule).join(ShiftType).filter(
            and_(
                Schedule.shift_date == target_date,
                ShiftType.point == point,
                Schedule.is_active == True
            )
        ).all()
        
        if not shifts:
            return []
        
        # Собираем все задачи для всех смен этого дня
        all_tasks = {}
        for shift in shifts:
            shift_type_obj = shift.shift_type_obj
            if not shift_type_obj:
                continue
                
            user = db.query(User).filter(User.iiko_id == shift.iiko_id).first()
            if not user:
                continue
                
            tasks = get_tasks_for_shift(
                user.id,
                shift.shift_date,
                shift_type_obj.shift_type,
                point
            )
            for task in tasks:
                all_tasks[task.id] = task
        
        # Получаем информацию о выполнении задач
        completed_tasks = db.query(ChecklistLog).filter(
            and_(
                ChecklistLog.shift_date == target_date,
                ChecklistLog.point == point
            )
        ).all()
        
        # Создаем mapping task_id -> completion info
        completion_info = {}
        for completed in completed_tasks:
            if completed.task_id not in completion_info:
                completion_info[completed.task_id] = []
            completion_info[completed.task_id].append({
                'completed_by': completed.completed_by.name if completed.completed_by else 'Неизвестно',
                'completed_at': completed.completed_at.strftime('%H:%M') if completed.completed_at else 'Неизвестно'
            })
        
        # Формируем результат
        results = []
        for task in all_tasks.values():
            task_completions = completion_info.get(task.id, [])
            completed = len(task_completions) > 0
            
            results.append({
                'task_id': task.id,
                'task_description': task.task_description,
                'completed': completed,
                'completions': task_completions
            })
        
        return results
        
    finally:
        db.close()

def get_weekday_name(weekday: int) -> str:
    """Получить название дня недели по номеру"""
    weekdays = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
    return weekdays[weekday]

def format_stats_period(start_date: date, end_date: date) -> str:
    """Форматировать период для отображения"""
    if start_date == end_date:
        return start_date.strftime('%d.%m.%Y')
    else:
        return f"{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}"
