"""Утилиты для проверки прав доступа"""
from telegram import Update
from bot.database.user_operations import get_user_by_username, has_any_role
from typing import List, Callable
from functools import wraps

# Роли
ROLE_BARISTA = 'barista'
ROLE_SENIOR = 'senior'
ROLE_MENTOR = 'mentor'

def get_user_role(update: Update) -> str:
    """Получить роль пользователя"""
    user = update.effective_user
    if not user:
        return None
    
    if user.username:
        db_user = get_user_by_username(user.username)
        if db_user:
            return db_user.role
    
    return None

def require_roles(roles: List[str]):
    """Декоратор для проверки ролей"""
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(update: Update, context, *args, **kwargs):
            user = update.effective_user
            if not user:
                await update.message.reply_text("❌ Ошибка: пользователь не найден")
                return
            
            # Проверяем по telegram_id
            if has_any_role(user.id, roles):
                return await func(update, context, *args, **kwargs)
            
            # Если не нашли по ID, пробуем по username
            if user.username:
                db_user = get_user_by_username(user.username)
                if db_user and db_user.role in roles and db_user.is_active:
                    return await func(update, context, *args, **kwargs)
            
            await update.message.reply_text(
                "❌ У вас нет доступа к этой функции.\n"
                f"Требуемые роли: {', '.join(roles)}"
            )
        return wrapper
    return decorator

def is_mentor(update: Update) -> bool:
    """Проверить, является ли пользователь наставником"""
    role = get_user_role(update)
    return role == ROLE_MENTOR

def is_senior_or_mentor(update: Update) -> bool:
    """Проверить, является ли пользователь старшим или наставником"""
    role = get_user_role(update)
    return role in [ROLE_SENIOR, ROLE_MENTOR]

def is_mentor_only(update: Update) -> bool:
    """Проверить, является ли пользователь только наставником (не старшим)"""
    role = get_user_role(update)
    return role == ROLE_MENTOR

