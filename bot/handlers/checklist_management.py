"""Обработчики для управления чек-листами (только для старших и наставников)"""
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters, CommandHandler
from bot.utils.auth import require_roles, ROLE_MENTOR, ROLE_SENIOR
from bot.database.checklist_operations import (
    create_checklist_template, get_checklist_templates, 
    update_checklist_template, delete_checklist_template
)
from bot.keyboards.menus import get_main_menu
import logging

logger = logging.getLogger(__name__)

# Состояния для управления чек-листами
(MANAGEMENT_MENU, SELECT_POINT, SELECT_DAY, SELECT_SHIFT, 
 ADD_TASK_DESCRIPTION, VIEW_TEMPLATES) = range(6)

@require_roles([ROLE_SENIOR, ROLE_MENTOR])
async def checklist_management_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Старт управления чек-листами"""
    keyboard = [
        [KeyboardButton("📋 Управление шаблонами")],
        [KeyboardButton("🔄 Управление пересменами")],
        [KeyboardButton("📊 Статистика выполнения")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "⚙️ Управление чек-листами\n\n"
        "Выберите раздел:",
        reply_markup=reply_markup
    )
    return MANAGEMENT_MENU

async def templates_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления шаблонами"""
    keyboard = [
        [KeyboardButton("➕ Добавить задачу")],
        [KeyboardButton("📋 Просмотреть задачи")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "📋 Управление шаблонами задач\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )
    return MANAGEMENT_MENU

async def start_adding_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления задачи"""
    keyboard = [
        [KeyboardButton("ДЕ"), KeyboardButton("УЯ")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "➕ Добавление новой задачи\n\n"
        "Выберите точку:",
        reply_markup=reply_markup
    )
    return SELECT_POINT

async def select_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор точки"""
    point = update.message.text
    if point not in ['ДЕ', 'УЯ']:
        await update.message.reply_text("❌ Выберите точку: ДЕ или УЯ")
        return SELECT_POINT
    
    context.user_data['new_task_point'] = point
    
    keyboard = [
        [KeyboardButton("Понедельник"), KeyboardButton("Вторник"), KeyboardButton("Среда")],
        [KeyboardButton("Четверг"), KeyboardButton("Пятница"), KeyboardButton("Суббота")],
        [KeyboardButton("Воскресенье")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"📍 Точка: {point}\n\n"
        "Выберите день недели:",
        reply_markup=reply_markup
    )
    return SELECT_DAY

async def select_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор дня недели"""
    day_map = {
        "Понедельник": 0, "Вторник": 1, "Среда": 2,
        "Четверг": 3, "Пятница": 4, "Суббота": 5, "Воскресенье": 6
    }
    
    day_name = update.message.text
    if day_name not in day_map:
        await update.message.reply_text("❌ Выберите день недели из списка")
        return SELECT_DAY
    
    context.user_data['new_task_day'] = day_map[day_name]
    
    keyboard = [
        [KeyboardButton("🌅 Утро"), KeyboardButton("🌆 Вечер")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"📅 День: {day_name}\n\n"
        "Выберите тип смены:",
        reply_markup=reply_markup
    )
    return SELECT_SHIFT

async def select_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор типа смены"""
    shift_map = {"🌅 Утро": "morning", "🌆 Вечер": "evening"}
    
    shift_name = update.message.text
    if shift_name not in shift_map:
        await update.message.reply_text("❌ Выберите тип смены из списка")
        return SELECT_SHIFT
    
    context.user_data['new_task_shift'] = shift_map[shift_name]
    
    await update.message.reply_text(
        f"🕒 Смена: {shift_name}\n\n"
        "Введите описание задачи:",
        reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)
    )
    return ADD_TASK_DESCRIPTION

async def add_task_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавление описания задачи"""
    task_description = update.message.text.strip()
    
    if not task_description:
        await update.message.reply_text("❌ Описание задачи не может быть пустым")
        return ADD_TASK_DESCRIPTION
    
    try:
        # Создаем задачу
        task = create_checklist_template(
            point=context.user_data['new_task_point'],
            day_of_week=context.user_data['new_task_day'],
            shift_type=context.user_data['new_task_shift'],
            task_description=task_description
        )
        
        day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        day_name = day_names[context.user_data['new_task_day']]
        
        await update.message.reply_text(
            f"✅ Задача успешно добавлена!\n\n"
            f"📍 Точка: {context.user_data['new_task_point']}\n"
            f"📅 День: {day_name}\n"
            f"🕒 Смена: {context.user_data['new_task_shift']}\n"
            f"📝 Задача: {task_description}"
        )
        
        # Очищаем данные
        context.user_data.clear()
        
        return await templates_management(update, context)
        
    except Exception as e:
        logger.error(f"Ошибка при добавлении задачи: {e}")
        await update.message.reply_text(f"❌ Ошибка при добавлении задачи: {str(e)}")
        return await templates_management(update, context)

async def view_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр шаблонов задач"""
    templates = get_checklist_templates()
    
    if not templates:
        await update.message.reply_text("📭 Шаблоны задач не найдены")
        return MANAGEMENT_MENU
    
    day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    shift_names = {"morning": "🌅 Утро", "evening": "🌆 Вечер"}
    
    response = "📋 Список шаблонов задач:\n\n"
    
    # Группируем по точкам, дням и сменам
    grouped = {}
    for template in templates:
        key = (template.point, template.day_of_week, template.shift_type)
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(template)
    
    for (point, day, shift), tasks in grouped.items():
        response += f"📍 {point} | {day_names[day]} | {shift_names.get(shift, shift)}\n"
        for i, task in enumerate(tasks, 1):
            response += f"  {i}. {task.task_description}\n"
        response += "\n"
    
    await update.message.reply_text(response)
    return MANAGEMENT_MENU

async def hybrid_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Управление пересменами"""
    await update.message.reply_text(
        "🔄 Управление пересменами\n\n"
        "Модуль в разработке...\n"
        "Скоро можно будет распределять задачи между сменами."
    )
    return MANAGEMENT_MENU

async def checklist_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика выполнения"""
    await update.message.reply_text(
        "📊 Статистика выполнения чек-листов\n\n"
        "Модуль в разработке...\n"
        "Скоро можно будет просматривать статистику по выполнению."
    )
    return MANAGEMENT_MENU

async def cancel_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена управления чек-листами"""
    context.user_data.clear()
    
    # Получаем пользователя для формирования правильного меню
    from bot.database.user_operations import get_user_by_username
    user = update.effective_user
    
    db_user = None
    if user.username:
        db_user = get_user_by_username(user.username)
    
    if db_user:
        await update.message.reply_text(
            "❌ Управление чек-листами отменено.",
            reply_markup=get_main_menu()
        )
    else:
        await update.message.reply_text(
            "❌ Управление чек-листами отменено.",
            reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)
        )
    
    return ConversationHandler.END

def get_checklist_management_handler():
    """Возвращает ConversationHandler для управления чек-листами"""
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^⚙️ Управление чек-листами$"), checklist_management_start)
        ],
        states={
            MANAGEMENT_MENU: [
                MessageHandler(filters.Regex("^📋 Управление шаблонами$"), templates_management),
                MessageHandler(filters.Regex("^🔄 Управление пересменами$"), hybrid_management),
                MessageHandler(filters.Regex("^📊 Статистика выполнения$"), checklist_stats),
                MessageHandler(filters.Regex("^⬅️ Назад$"), cancel_management),
            ],
            # ... остальные состояния ...
        },
        fallbacks=[
            CommandHandler("cancel", cancel_management),
            CommandHandler("start", cancel_management),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_management),
        ],
        allow_reentry=True
    )