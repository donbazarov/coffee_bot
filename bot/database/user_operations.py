"""Операции для работы с пользователями"""
from sqlalchemy.orm import Session
from .models import SessionLocal, User
from typing import Optional, List

def get_user_by_iiko_id(iiko_id: int) -> Optional[User]:
    """Получить пользователя по Iiko ID"""
    db = SessionLocal()
    try:
        return db.query(User).filter(User.iiko_id == iiko_id).first()
    finally:
        db.close()

def get_user_by_telegram_id(telegram_id: int) -> Optional[User]:
    """Получить пользователя по Telegram ID"""
    db = SessionLocal()
    try:
        return db.query(User).filter(User.telegram_id == telegram_id).first()
    finally:
        db.close()

def get_user_by_username(telegram_username: str) -> Optional[User]:
    """Получить пользователя по Telegram username"""
    db = SessionLocal()
    try:
        return db.query(User).filter(User.telegram_username == telegram_username).first()
    finally:
        db.close()

def get_user_by_id(user_id: int) -> Optional[User]:
    """Получить пользователя по внутреннему ID"""
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id).first()
    finally:
        db.close()

def create_user(name: str, iiko_id: Optional[int] = None,
                telegram_username: Optional[str] = None, 
                role: str = 'barista') -> User:
    """Создать нового пользователя"""
    db = SessionLocal()
    try:
        user = User(
            name=name,
            iiko_id=iiko_id,
            telegram_username=telegram_username,
            role=role,
            is_active=1
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()

def update_user(user_id: int, **kwargs) -> Optional[User]:
    """Обновить данные пользователя"""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return None
        
        for key, value in kwargs.items():
            if hasattr(user, key):
                setattr(user, key, value)
        
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()

def delete_user(user_id: int) -> bool:
    """Удалить пользователя (мягкое удаление - деактивация)"""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return False
        
        user.is_active = 0
        db.commit()
        return True
    finally:
        db.close()

def get_all_users(active_only: bool = True) -> List[User]:
    """Получить всех пользователей"""
    db = SessionLocal()
    try:
        query = db.query(User)
        if active_only:
            query = query.filter(User.is_active == 1)
        return query.all()
    finally:
        db.close()

def get_users_by_role(role: str, active_only: bool = True) -> List[User]:
    """Получить пользователей по роли"""
    db = SessionLocal()
    try:
        query = db.query(User).filter(User.role == role)
        if active_only:
            query = query.filter(User.is_active == 1)
        return query.all()
    finally:
        db.close()

def has_role(telegram_id: int, role: str) -> bool:
    """Проверить, имеет ли пользователь указанную роль"""
    user = get_user_by_telegram_id(telegram_id)
    if not user or not user.is_active:
        return False
    return user.role == role

def has_any_role(telegram_id: int, roles: List[str]) -> bool:
    """Проверить, имеет ли пользователь одну из указанных ролей"""
    user = get_user_by_telegram_id(telegram_id)
    if not user or not user.is_active:
        return False
    return user.role in roles

