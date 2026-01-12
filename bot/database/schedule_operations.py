"""Операции для работы с расписанием смен"""
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, cast, String
from .models import SessionLocal, Schedule, ShiftType, User
from typing import Optional, List, Dict
from datetime import date, datetime, timedelta, time
import logging

logger = logging.getLogger(__name__)

def get_shift_type_by_times(start_time: time, end_time: time) -> Optional[ShiftType]:
    """Получить тип смены по времени начала и окончания"""
    db = SessionLocal()
    try:
        # Конвертируем time в строку для сравнения
        start_time_str = start_time.strftime("%H:%M")
        end_time_str = end_time.strftime("%H:%M")
        
        logger.info(f"🔍 Поиск типа смены по времени: {start_time_str} - {end_time_str}")
        
        shift_type = db.query(ShiftType).filter(
            and_(
                ShiftType.start_time == start_time_str,
                ShiftType.end_time == end_time_str
            )
        ).first()
        
        if shift_type:
            logger.info(f"✅ Найден тип смены: {shift_type.name} (ID: {shift_type.id})")
        else:
            logger.warning(f"❌ Тип смены не найден для {start_time_str} - {end_time_str}")
            
        return shift_type
    finally:
        db.close()

def get_shift_type_by_time_strings(start_time_str: str, end_time_str: str):
    """Получить тип смены по времени начала и окончания в виде строк"""
    db = SessionLocal()
    try:
        return db.query(ShiftType).filter(
            and_(
                ShiftType.start_time == start_time_str,
                ShiftType.end_time == end_time_str
            )
        ).first()
    finally:
        db.close()

def get_shift_type_by_id(shift_type_id: int) -> Optional[ShiftType]:
    """Получить тип смены по ID"""
    db = SessionLocal()
    try:
        return db.query(ShiftType).filter(ShiftType.id == shift_type_id).first()
    finally:
        db.close()

def create_shift(shift_date: date, iiko_id: str, shift_type_id: int) -> Schedule:
    """Создать новую смену"""
    db = SessionLocal()
    try:
        shift = Schedule(
            shift_date=shift_date,
            iiko_id=str(iiko_id),
            shift_type_id=shift_type_id
        )
        db.add(shift)
        db.commit()
        db.refresh(shift)
        logger.info(f"Создана смена ID {shift.shift_id} для сотрудника {iiko_id} на {shift_date}")
        return shift
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при создании смены: {e}")
        raise
    finally:
        db.close()

def get_shift_by_id(shift_id: int) -> Optional[Schedule]:
    """Получить смену по ID"""
    db = SessionLocal()
    try:
        return db.query(Schedule).filter(Schedule.shift_id == shift_id).first()
    finally:
        db.close()

def get_shifts_by_iiko_id(iiko_id: str, start_date: Optional[date] = None,
                          end_date: Optional[date] = None) -> List[Schedule]:
    """Получить смены сотрудника по iiko_id"""
    db = SessionLocal()
    try:
        query = db.query(Schedule).filter(Schedule.iiko_id == str(iiko_id))
        
        logger.info(f"🔍 ДИАГНОСТИКА get_shifts_by_iiko_id: iiko_id={iiko_id}, start_date={start_date}, end_date={end_date}")
        
        if start_date:
            query = query.filter(Schedule.shift_date >= start_date)
        if end_date:
            query = query.filter(Schedule.shift_date <= end_date)
        
        # Сортируем по дате и времени начала смены
        shifts = query.order_by(
            Schedule.shift_date,
            ShiftType.start_time
        ).join(ShiftType).all()
        
        logger.info(f"🔍 ДИАГНОСТИКА get_shifts_by_iiko_id: найдено {len(shifts)} смен")
        for shift in shifts:
            logger.info(f"🔍 Смена: {shift.shift_date} - {shift.shift_type_obj.start_time if shift.shift_type_obj else 'NO_TYPE'}")
        
        return shifts
    finally:
        db.close()

def get_shifts_by_date_range(start_date: date, end_date: date) -> List[Schedule]:
    """Получить все смены в диапазоне дат"""
    db = SessionLocal()
    try:
        return db.query(Schedule).filter(
            and_(Schedule.shift_date >= start_date, Schedule.shift_date <= end_date)
        ).order_by(
            Schedule.shift_date,
            ShiftType.start_time
        ).join(ShiftType).all()
    finally:
        db.close()

def get_upcoming_shifts_by_iiko_id(iiko_id: str, days: int = 7) -> List[Schedule]:
    """Получить ближайшие смены сотрудника на указанное количество дней"""
    today = date.today()
    end_date = today + timedelta(days=days)
    return get_shifts_by_iiko_id(iiko_id, start_date=today, end_date=end_date)

def _find_partner_by_shift_type(db: Session, shift_date: date, point: str, shift_type: str, exclude_iiko_id: str) -> Optional[User]:
    """Найти напарника по типу смены на точке и дате."""
    return (
        db.query(User)
        .join(Schedule, cast(User.iiko_id, String) == Schedule.iiko_id)
        .join(ShiftType, Schedule.shift_type_id == ShiftType.id)
        .filter(
            and_(
                Schedule.shift_date == shift_date,
                ShiftType.point == point,
                ShiftType.shift_type == shift_type,
                Schedule.iiko_id != str(exclude_iiko_id),
                User.is_active == 1
            )
        )
        .order_by(User.name)
        .first()
    )

def get_shift_partner(shift_date: date, point: str, shift_type: str, exclude_iiko_id: str) -> Optional[Dict[str, object]]:
    """Найти напарника по смене. Возвращает пользователя и фактический тип смены."""
    db = SessionLocal()
    try:
        partner = _find_partner_by_shift_type(db, shift_date, point, shift_type, exclude_iiko_id)
        if partner:
            return {"user": partner, "shift_type": shift_type}

        if shift_type != "hybrid":
            partner = _find_partner_by_shift_type(db, shift_date, point, "hybrid", exclude_iiko_id)
            if partner:
                return {"user": partner, "shift_type": "hybrid"}

        return None
    finally:
        db.close()

def update_shift_iiko_id(shift_id: int, new_iiko_id: str) -> Optional[Schedule]:
    """Изменить iiko_id смены (для замен)"""
    db = SessionLocal()
    try:
        shift = db.query(Schedule).filter(Schedule.shift_id == shift_id).first()
        if not shift:
            return None
        
        shift.iiko_id = str(new_iiko_id)
        shift.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(shift)
        logger.info(f"Смена ID {shift_id} переназначена на сотрудника {new_iiko_id}")
        return shift
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при обновлении смены: {e}")
        raise
    finally:
        db.close()

def update_shift(shift_id: int, **kwargs) -> Optional[Schedule]:
    """Обновить смену"""
    db = SessionLocal()
    try:
        shift = db.query(Schedule).filter(Schedule.shift_id == shift_id).first()
        if not shift:
            return None
        
        for key, value in kwargs.items():
            if hasattr(shift, key):
                setattr(shift, key, value)
        
        shift.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(shift)
        logger.info(f"Смена ID {shift_id} обновлена")
        return shift
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при обновлении смены: {e}")
        raise
    finally:
        db.close()

def delete_shift(shift_id: int) -> bool:
    """Удалить смену"""
    db = SessionLocal()
    try:
        shift = db.query(Schedule).filter(Schedule.shift_id == shift_id).first()
        if not shift:
            return False
        
        db.delete(shift)
        db.commit()
        logger.info(f"Смена ID {shift_id} удалена")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при удалении смены: {e}")
        raise
    finally:
        db.close()

def bulk_create_shifts(shifts: List[Dict]) -> int:
    """Массовое создание смен"""
    db = SessionLocal()
    created_count = 0
    try:
        for shift_data in shifts:
            # Проверяем, не существует ли уже такая смена
            existing = db.query(Schedule).filter(
                and_(
                    Schedule.shift_date == shift_data['shift_date'],
                    Schedule.iiko_id == str(shift_data['iiko_id']),
                    Schedule.shift_type_id == shift_data['shift_type_id']
                )
            ).first()
            
            if existing:
                # Обновляем существующую смену
                existing.updated_at = datetime.utcnow()
                continue
            
            shift = Schedule(**shift_data)
            db.add(shift)
            created_count += 1
        
        db.commit()
        logger.info(f"Создано/обновлено {created_count} смен")
        return created_count
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при массовом создании смен: {e}")
        raise
    finally:
        db.close()

def delete_shifts_by_date_range(start_date: date, end_date: date) -> int:
    """Удалить все смены в диапазоне дат (для перепарсинга) - только будущие"""
    db = SessionLocal()
    try:
        # Удаляем только будущие смены
        today = date.today()
        actual_start_date = max(start_date, today)
        
        if actual_start_date > end_date:
            logger.info("Нет будущих смен для удаления в указанном диапазоне")
            return 0
            
        deleted_count = db.query(Schedule).filter(
            and_(
                Schedule.shift_date >= actual_start_date, 
                Schedule.shift_date <= end_date
            )
        ).delete()
        db.commit()
        logger.info(f"Удалено {deleted_count} будущих смен в диапазоне {actual_start_date} - {end_date}")
        return deleted_count
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при удалении смен: {e}")
        raise
    finally:
        db.close()

def remove_stale_shifts(new_shifts: List[Dict], start_date: date, end_date: date) -> int:
    """Удалить смены, отсутствующие в новом наборе данных (только источник sheets)."""
    db = SessionLocal()
    try:
        new_keys = {
            (shift['shift_date'], str(shift['iiko_id']), shift['shift_type_id'])
            for shift in new_shifts
        }
        deleted_count = 0
        existing_shifts = db.query(Schedule).filter(
            and_(
                Schedule.shift_date >= start_date,
                Schedule.shift_date <= end_date
            )
        ).all()
        
        for shift in existing_shifts:
            key = (shift.shift_date, str(shift.iiko_id), shift.shift_type_id)
            if key in new_keys:
                continue
            if shift.source not in (None, 'sheets'):
                continue
            db.delete(shift)
            deleted_count += 1
        
        db.commit()
        logger.info(f"Удалено {deleted_count} устаревших смен в диапазоне {start_date} - {end_date}")
        return deleted_count
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при удалении устаревших смен: {e}")
        raise
    finally:
        db.close()

def get_all_shifts_for_user_in_range(iiko_id: str, start_date: date, end_date: date) -> List[Schedule]:
    """Получить все смены пользователя в диапазоне дат"""
    return get_shifts_by_iiko_id(iiko_id, start_date=start_date, end_date=end_date)

def create_shift_type(shift_type_data):
    """Создать новый тип смены"""
    db = SessionLocal()
    try:
        shift_type = ShiftType(
            name=shift_type_data['name'],
            start_time=shift_type_data['start_time'],
            end_time=shift_type_data['end_time'],
            point=shift_type_data['point'],
            shift_type=shift_type_data['shift_type']
        )
        db.add(shift_type)
        db.flush()
        shift_type_id = shift_type.id
        db.commit()
        return shift_type_id
    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()

def get_shift_types():
    """Получить все типы смен"""
    db = SessionLocal()
    try:
        return db.query(ShiftType).order_by(ShiftType.point, ShiftType.start_time).all()
    finally:
        db.close()

def get_shift_type_by_id(shift_type_id):
    """Получить тип смены по ID"""
    db = SessionLocal()
    try:
        return db.query(ShiftType).filter(ShiftType.id == shift_type_id).first()
    finally:
        db.close()

def update_shift_type(shift_type_id, update_data):
    """Обновить тип смены"""
    db = SessionLocal()
    try:
        shift_type = db.query(ShiftType).filter(ShiftType.id == shift_type_id).first()
        if shift_type:
            for key, value in update_data.items():
                setattr(shift_type, key, value)
            db.commit()
            return True
        return False
    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()

def delete_shift_type(shift_type_id):
    """Удалить тип смены"""
    db = SessionLocal()
    try:
        shift_type = db.query(ShiftType).filter(ShiftType.id == shift_type_id).first()
        if shift_type:
            db.delete(shift_type)
            db.commit()
            return True
        return False
    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()
        
def update_shift_iiko_id(shift_id: int, new_iiko_id: str) -> Optional[Schedule]:
    """Изменить iiko_id смены (для замен) с проверкой"""
    db = SessionLocal()
    try:
        shift = db.query(Schedule).filter(Schedule.shift_id == shift_id).first()
        if not shift:
            return None
        
        # Логируем изменение
        logger.info(f"Смена ID {shift_id}: {shift.iiko_id} -> {new_iiko_id}")
        
        shift.iiko_id = str(new_iiko_id)
        shift.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(shift)
        logger.info(f"Смена ID {shift_id} переназначена на сотрудника {new_iiko_id}")
        return shift
    except Exception as e:
        db.rollback()
        logger.error(f"Ошибка при обновлении смены: {e}")
        raise
    finally:
        db.close()
