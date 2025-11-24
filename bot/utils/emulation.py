"""Утилиты для эмуляции действий от лица других сотрудников"""
from telegram import Update
from telegram.ext import ContextTypes
from bot.database.user_operations import get_user_by_iiko_id
import logging

logger = logging.getLogger(__name__)

def get_emulated_user(context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Получить эмулированного пользователя из контекста - БЕЗОПАСНАЯ ВЕРСИЯ"""
    iiko_id = context.user_data.get('emulated_iiko_id')
    name = context.user_data.get('emulated_user_name')
    
    # Гарантируем, что возвращаем корректные строки
    return {
        'iiko_id': str(iiko_id) if iiko_id is not None else "Неизвестный",
        'name': str(name) if name is not None else "Неизвестный"
    }

def is_emulation_mode(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверить, находится ли пользователь в режиме эмуляции"""
    return 'emulated_iiko_id' in context.user_data

def start_emulation(context: ContextTypes.DEFAULT_TYPE, iiko_id: str, user_name: str):
    """Начать эмуляцию пользователя"""
    context.user_data['emulated_iiko_id'] = iiko_id
    context.user_data['emulated_user_name'] = user_name
    logger.info(f"🔄 Начата эмуляция: {user_name} (iiko_id: {iiko_id})")

def stop_emulation(context: ContextTypes.DEFAULT_TYPE):
    """Завершить эмуляцию"""
    emulated_name = context.user_data.get('emulated_user_name')
    context.user_data.pop('emulated_iiko_id', None)
    context.user_data.pop('emulated_user_name', None)
    logger.info(f"🔄 Завершена эмуляция: {emulated_name}")

def get_current_iiko_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Получить текущий iiko_id (эмулированный или реальный)"""
    if is_emulation_mode(context):
        return context.user_data['emulated_iiko_id']
    
    from bot.database.user_operations import get_user_by_telegram_id
    user = update.effective_user
    db_user = get_user_by_telegram_id(user.id)
    
    if not db_user and user.username:
        from bot.database.user_operations import get_user_by_username
        db_user = get_user_by_username(user.username)
    
    return str(db_user.iiko_id) if db_user and db_user.iiko_id else None

def get_current_user_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Получить текущее имя пользователя (эмулированное или реальное)"""
    if is_emulation_mode(context):
        return context.user_data['emulated_user_name']
    
    from bot.database.user_operations import get_user_by_telegram_id
    user = update.effective_user
    db_user = get_user_by_telegram_id(user.id)
    
    if not db_user and user.username:
        from bot.database.user_operations import get_user_by_username
        db_user = get_user_by_username(user.username)
    
    return db_user.name if db_user else "Пользователь"