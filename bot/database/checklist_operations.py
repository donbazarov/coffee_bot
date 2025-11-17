"""Операции для работы с чек-листами"""
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from .models import SessionLocal, ChecklistTemplate, HybridShiftAssignment, ChecklistLog, User, Schedule, ShiftType
from typing import Optional, List, Dict
from datetime import date, datetime, time, timedelta
import logging

logger = logging.getLogger(__name__)

def create_checklist_template(point: str, day_of_week: int, shift_type: str, task_description: str, order_index: int = 0) -> ChecklistTemplate:
    """Создать шаблон задания для чек-листа"""
    db = SessionLocal()
    try:
        template = ChecklistTemplate(
            point=point,
            day_of_week=day_of_week,
            shift_type=shift_type,
            task_description=task_description,
            order_index=order_index
        )
        db.add(template)
        db.commit()
        db.refresh(template)
        logger.info(f"Создан шаблон чек-листа ID {template.id} для {point} {shift_type} день {day_of_week}")
        return template
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при создании шаблона чек-листа: {e}")
        raise
    finally:
        db.close()

def get_checklist_templates(point: Optional[str] = None, day_of_week: Optional[int] = None, 
                          shift_type: Optional[str] = None) -> List[ChecklistTemplate]:
    """Получить шаблоны чек-листов с фильтрами"""
    db = SessionLocal()
    try:
        query = db.query(ChecklistTemplate).filter(ChecklistTemplate.is_active == 1)
        
        if point:
            query = query.filter(ChecklistTemplate.point == point)
        if day_of_week is not None:
            query = query.filter(ChecklistTemplate.day_of_week == day_of_week)
        if shift_type:
            query = query.filter(ChecklistTemplate.shift_type == shift_type)
        
        return query.order_by(ChecklistTemplate.order_index).all()
    finally:
        db.close()

def update_checklist_template(template_id: int, **kwargs) -> Optional[ChecklistTemplate]:
    """Обновить шаблон чек-листа"""
    db = SessionLocal()
    try:
        template = db.query(ChecklistTemplate).filter(ChecklistTemplate.id == template_id).first()
        if not template:
            return None
        
        for key, value in kwargs.items():
            if hasattr(template, key):
                setattr(template, key, value)
        
        template.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(template)
        logger.info(f"Шаблон чек-листа ID {template_id} обновлен")
        return template
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при обновлении шаблона чек-листа: {e}")
        raise
    finally:
        db.close()

def delete_checklist_template(template_id: int) -> bool:
    """Удалить шаблон чек-листа (мягкое удаление)"""
    db = SessionLocal()
    try:
        template = db.query(ChecklistTemplate).filter(ChecklistTemplate.id == template_id).first()
        if not template:
            return False
        
        template.is_active = 0
        template.updated_at = datetime.utcnow()
        db.commit()
        logger.info(f"Шаблон чек-листа ID {template_id} деактивирован")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при удалении шаблона чек-листа: {e}")
        raise
    finally:
        db.close()

def get_current_shift_for_user(user_id: int) -> Optional[Dict]:
    """Определить текущую смену пользователя с учетом временного окна (+-1 час)"""
    db = SessionLocal()
    try:
        now = datetime.now()
        today = now.date()
        current_time = now.time()
        
        # Получаем пользователя
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.iiko_id:
            return None
        
        # Ищем смены на сегодня для пользователя
        shifts = db.query(Schedule).join(ShiftType).filter(
            and_(
                Schedule.iiko_id == str(user.iiko_id),
                Schedule.shift_date == today
            )
        ).all()
        
        for shift in shifts:
            shift_type = shift.shift_type_obj
            if not shift_type:
                continue
            
            # Расширяем временное окно на +-1 час
            start_time = shift_type.start_time
            end_time = shift_type.end_time
            
            # Создаем расширенные временные окна
            start_window = (datetime.combine(today, start_time) - timedelta(hours=1)).time()
            end_window = (datetime.combine(today, end_time) + timedelta(hours=1)).time()
            
            if start_window <= current_time <= end_window:
                return {
                    'shift': shift,
                    'shift_type': shift_type,
                    'day_of_week': today.weekday(),
                    'point': shift_type.point
                }
        
        return None
    finally:
        db.close()

def check_hybrid_shift_exists(point: str, shift_date: date) -> bool:
    """Проверить, есть ли пересмен на точке в указанную дату"""
    db = SessionLocal()
    try:
        day_of_week = shift_date.weekday()
        
        # Ищем смены типа 'hybrid' на эту дату и точку
        hybrid_shifts = db.query(Schedule).join(ShiftType).filter(
            and_(
                Schedule.shift_date == shift_date,
                ShiftType.point == point,
                ShiftType.shift_type == 'hybrid'
            )
        ).count()
        
        return hybrid_shifts > 0
    finally:
        db.close()

def get_tasks_for_shift(user_id: int, shift_date: date, shift_type: str, point: str) -> List[ChecklistTemplate]:
    """Получить задачи для конкретной смены с учетом пересменов"""
    db = SessionLocal()
    try:
        day_of_week = shift_date.weekday()
        has_hybrid = check_hybrid_shift_exists(point, shift_date)
        
        if shift_type == 'hybrid':
            # Для пересмена получаем назначенные ему задачи
            assignment = db.query(HybridShiftAssignment).filter(
                and_(
                    HybridShiftAssignment.point == point,
                    HybridShiftAssignment.day_of_week == day_of_week
                )
            ).first()
            
            tasks = []
            if assignment and assignment.morning_task:
                tasks.append(assignment.morning_task)
            if assignment and assignment.evening_task:
                tasks.append(assignment.evening_task)
            return tasks
            
        elif shift_type == 'morning':
            # Для утра - исключаем задачи, переданные пересмену
            morning_tasks = get_checklist_templates(point=point, day_of_week=day_of_week, shift_type='morning')
            if has_hybrid:
                assignment = db.query(HybridShiftAssignment).filter(
                    and_(
                        HybridShiftAssignment.point == point,
                        HybridShiftAssignment.day_of_week == day_of_week
                    )
                ).first()
                if assignment and assignment.morning_task:
                    morning_tasks = [task for task in morning_tasks if task.id != assignment.morning_task.id]
            return morning_tasks
            
        elif shift_type == 'evening':
            # Для вечера - аналогично утру
            evening_tasks = get_checklist_templates(point=point, day_of_week=day_of_week, shift_type='evening')
            if has_hybrid:
                assignment = db.query(HybridShiftAssignment).filter(
                    and_(
                        HybridShiftAssignment.point == point,
                        HybridShiftAssignment.day_of_week == day_of_week
                    )
                ).first()
                if assignment and assignment.evening_task:
                    evening_tasks = [task for task in evening_tasks if task.id != assignment.evening_task.id]
            return evening_tasks
        
        return []
    finally:
        db.close()

def get_completed_tasks_for_shift(shift_date: date, point: str) -> List[int]:
    """Получить список выполненных задач для смены на дату и точке"""
    db = SessionLocal()
    try:
        completed_tasks = db.query(ChecklistLog.task_id).filter(
            and_(
                ChecklistLog.shift_date == shift_date,
                ChecklistLog.point == point
            )
        ).all()
        return [task_id for (task_id,) in completed_tasks]
    finally:
        db.close()

def mark_task_completed(user_id: int, task_id: int, shift_date: date, shift_type: str, point: str) -> bool:
    """Отметить задачу как выполненную"""
    db = SessionLocal()
    try:
        # Проверяем, не выполнена ли уже задача
        existing = db.query(ChecklistLog).filter(
            and_(
                ChecklistLog.task_id == task_id,
                ChecklistLog.shift_date == shift_date,
                ChecklistLog.point == point
            )
        ).first()
        
        if existing:
            return True  # Уже выполнена
        
        log_entry = ChecklistLog(
            user_id=user_id,
            task_id=task_id,
            shift_date=shift_date,
            shift_type=shift_type,
            point=point,
            completed_by_user_id=user_id
        )
        db.add(log_entry)
        db.commit()
        logger.info(f"Задача {task_id} отмечена как выполненная пользователем {user_id}")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при отметке задачи: {e}")
        return False
    finally:
        db.close()