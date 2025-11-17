"""Обработчики для системы чек-листов"""
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters, CommandHandler, CallbackQueryHandler
from bot.utils.auth import require_roles, ROLE_MENTOR, ROLE_SENIOR
from bot.utils.common_handlers import cancel_conversation, start_cancel_conversation
from bot.database.user_operations import get_user_by_username
from bot.database.checklist_operations import (
    get_current_shift_for_user, get_tasks_for_shift, get_completed_tasks_for_shift,
    mark_task_completed
)
from bot.keyboards.menus import get_main_menu
from datetime import datetime, date
import logging

logger = logging.getLogger(__name__)

# Состояния для чек-листов
(CHECKLIST_MENU, CHECKLIST_VIEW, CHECKLIST_TASK_ACTION) = range(3)

async def checklist_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню чек-листа"""
    user = update.effective_user
    
    # Ищем пользователя по telegram_username
    if not user.username:
        await update.message.reply_text(
            "❌ У вас не установлен username в Telegram.\n\n"
            "Для работы с ботом необходимо:\n"
            "1. Установить username в настройках Telegram\n"
            "2. Сообщить администратору для привязки к вашей учетной записи"
        )
        return ConversationHandler.END
    
    db_user = get_user_by_username(user.username)
    
    if not db_user:
        await update.message.reply_text(
            f"❌ Пользователь @{user.username} не найден в системе.\n\n"
            "Возможные причины:\n"
            "• Ваш username не привязан к учетной записи\n"
            "• Обратитесь к администратору для добавления"
        )
        return ConversationHandler.END
    
    if not db_user.iiko_id:
        await update.message.reply_text(
            "❌ У вашей учетной записи не указан Iiko ID.\n\n"
            "Обратитесь к администратору для настройки."
        )
        return ConversationHandler.END
    
    # Проверяем текущую смену пользователя
    shift_info = get_current_shift_for_user(db_user.id)
    
    if not shift_info:
        await update.message.reply_text(
            "❌ Сейчас у вас нет активной смены.\n\n"
            "Чек-лист доступен только во время смены:\n"
            "• За 1 час до начала смены\n"
            "• Во время смены\n" 
            "• В течение 1 часа после окончания\n\n"
            "Если вы считаете, что это ошибка, проверьте расписание."
        )
        return ConversationHandler.END
    
    # Получаем задачи для смены
    tasks = get_tasks_for_shift(
        db_user.id, 
        shift_info['shift'].shift_date, 
        shift_info['shift_type'].shift_type, 
        shift_info['point']
    )
    
    if not tasks:
        await update.message.reply_text(
            f"📝 Чек-лист для {shift_info['shift_type'].name}\n\n"
            "На эту смену не назначено задач."
        )
        return ConversationHandler.END
    
    # Получаем выполненные задачи
    completed_tasks = get_completed_tasks_for_shift(
        shift_info['shift'].shift_date, 
        shift_info['point']
    )
    
    # Создаем клавиатуру с задачами
    keyboard = []
    for task in tasks:
        status = "✅" if task.id in completed_tasks else "☐"
        button_text = f"{status} {task.task_description}"
        keyboard.append([KeyboardButton(button_text)])
    
    keyboard.append([KeyboardButton("🔄 Обновить статус")])
    keyboard.append([KeyboardButton("⬅️ Назад")])
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    shift_type_names = {
        'morning': '🌅 Утро',
        'hybrid': '🌤️ Пересмен', 
        'evening': '🌆 Вечер'
    }
    
    completion_count = len([t for t in tasks if t.id in completed_tasks])
    completion_percent = (completion_count / len(tasks)) * 100 if tasks else 0
    
    await update.message.reply_text(
        f"📝 Чек-лист смены\n\n"
        f"📍 Точка: {shift_info['point']}\n"
        f"🕒 Смена: {shift_type_names.get(shift_info['shift_type'].shift_type, shift_info['shift_type'].shift_type)}\n"
        f"📅 Дата: {shift_info['shift'].shift_date.strftime('%d.%m.%Y')}\n"
        f"📊 Прогресс: {completion_count}/{len(tasks)} ({completion_percent:.0f}%)\n\n"
        "Нажмите на задачу чтобы отметить выполнение:",
        reply_markup=reply_markup
    )
    
    # Сохраняем информацию о смене в контексте
    context.user_data['current_shift'] = shift_info
    context.user_data['tasks'] = tasks
    context.user_data['user_id'] = db_user.id
    
    return CHECKLIST_VIEW

async def handle_task_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка отметки выполнения задачи"""
    user = update.effective_user
    button_text = update.message.text
    
    # Получаем user_id из контекста
    user_id = context.user_data.get('user_id')
    if not user_id:
        await update.message.reply_text("❌ Сессия устарела, начните заново")
        return await checklist_menu(update, context)
    
    # Извлекаем ID задачи из текста кнопки (ищем задачу по описанию)
    if not button_text.startswith(("✅", "☐")):
        await update.message.reply_text("❌ Неизвестное действие")
        return CHECKLIST_VIEW
    
    # Убираем эмодзи и пробел из начала
    task_description = button_text[2:]  # Убираем "☐ " или "✅ "
    
    # Ищем задачу в сохраненном списке
    tasks = context.user_data.get('tasks', [])
    task_to_mark = None
    for task in tasks:
        if task.task_description == task_description:
            task_to_mark = task
            break
    
    if not task_to_mark:
        await update.message.reply_text("❌ Задача не найдена")
        return CHECKLIST_VIEW
    
    shift_info = context.user_data.get('current_shift')
    if not shift_info:
        await update.message.reply_text("❌ Информация о смене устарела")
        return await checklist_menu(update, context)
    
    # Отмечаем задачу как выполненную
    success = mark_task_completed(
        user_id,
        task_to_mark.id,
        shift_info['shift'].shift_date,
        shift_info['shift_type'].shift_type,
        shift_info['point']
    )
    
    if success:
        await update.message.reply_text(f"✅ Задача отмечена как выполненная: {task_description}")
        
        # Обновляем клавиатуру
        tasks = get_tasks_for_shift(
            user_id, 
            shift_info['shift'].shift_date, 
            shift_info['shift_type'].shift_type, 
            shift_info['point']
        )
        completed_tasks = get_completed_tasks_for_shift(shift_info['shift'].shift_date, shift_info['point'])
        
        keyboard = []
        for task in tasks:
            status = "✅" if task.id in completed_tasks else "☐"
            button_text = f"{status} {task.task_description}"
            keyboard.append([KeyboardButton(button_text)])
        
        keyboard.append([KeyboardButton("🔄 Обновить статус")])
        keyboard.append([KeyboardButton("⬅️ Назад")])
        
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        completion_count = len([t for t in tasks if t.id in completed_tasks])
        completion_percent = (completion_count / len(tasks)) * 100 if tasks else 0
        
        await update.message.reply_text(
            f"📊 Прогресс обновлен: {completion_count}/{len(tasks)} ({completion_percent:.0f}%)",
            reply_markup=reply_markup
        )
        
        # Обновляем задачи в контексте
        context.user_data['tasks'] = tasks
    else:
        await update.message.reply_text("❌ Ошибка при отметке задачи")
    
    return CHECKLIST_VIEW

async def refresh_checklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновить статус чек-листа"""
    return await checklist_menu(update, context)

def get_checklist_conversation_handler():
    """Возвращает ConversationHandler для чек-листов"""
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📝 Чек-лист смены$"), checklist_menu)
        ],
        states={
            CHECKLIST_VIEW: [
                MessageHandler(filters.Regex("^🔄 Обновить статус$"), refresh_checklist),
                MessageHandler(filters.Regex("^⬅️ Назад$"), cancel_conversation),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_action),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CommandHandler("start", start_cancel_conversation),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_conversation),
        ],
        allow_reentry=True
    )