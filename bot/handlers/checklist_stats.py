"""Обработчики для управления чек-листами (только для старших и наставников)"""
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters, CommandHandler
from bot.utils.common_handlers import start_cancel_conversation, cancel_conversation
from bot.utils.auth import require_roles, ROLE_MENTOR, ROLE_SENIOR
from bot.database.checklist_operations import (
    create_checklist_template, get_checklist_templates, 
    update_checklist_template, delete_checklist_template
)
from bot.database.checklist_stats_operations import (
    get_individual_stats, get_point_stats, get_task_stats, get_detailed_log,
    get_weekday_name, format_stats_period
)
from bot.keyboards.menus import get_main_menu
from .checklist_management import checklist_management_start
from datetime import date, datetime, timedelta
import logging

logger = logging.getLogger(__name__)

# Добавим новые состояния для статистики
(STATS_MENU, STATS_INDIVIDUAL, STATS_POINT, STATS_TASK, STATS_DETAILED_LOG,
 SELECT_STATS_PERIOD, SELECT_STATS_DATE, SELECT_STATS_POINT) = range(8)

@require_roles([ROLE_SENIOR, ROLE_MENTOR])
async def checklist_stats_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню статистики чек-листов"""
    keyboard = [
        [KeyboardButton("👤 Индивидуальная статистика")],
        [KeyboardButton("📍 Статистика по точкам")],
        [KeyboardButton("📝 Статистика по заданиям")],
        [KeyboardButton("📋 Детальный лог за день")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "📊 Статистика выполнения чек-листов\n\n"
        "Выберите тип отчета:",
        reply_markup=reply_markup
    )
    return STATS_MENU

async def stats_individual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Индивидуальная статистика"""
    context.user_data['stats_type'] = 'individual'
    
    keyboard = [
        [KeyboardButton("📅 За неделю"), KeyboardButton("📅 За месяц")],
        [KeyboardButton("📅 Произвольный период"), KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "👤 Индивидуальная статистика\n\n"
        "Выберите период:",
        reply_markup=reply_markup
    )
    return SELECT_STATS_PERIOD

async def stats_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика по точкам"""
    context.user_data['stats_type'] = 'point'
    
    keyboard = [
        [KeyboardButton("📅 За неделю"), KeyboardButton("📅 За месяц")],
        [KeyboardButton("📅 Произвольный период"), KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "📍 Статистика по точкам\n\n"
        "Выберите период:",
        reply_markup=reply_markup
    )
    return SELECT_STATS_PERIOD

async def stats_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика по заданиям"""
    context.user_data['stats_type'] = 'task'
    
    keyboard = [
        [KeyboardButton("📅 За неделю"), KeyboardButton("📅 За месяц")],
        [KeyboardButton("📅 Произвольный период"), KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "📝 Статистика по заданиям\n\n"
        "Выберите период:",
        reply_markup=reply_markup
    )
    return SELECT_STATS_PERIOD

async def stats_detailed_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Детальный лог за день"""
    context.user_data['stats_type'] = 'detailed_log'
    
    keyboard = [
        [KeyboardButton("📅 Сегодня"), KeyboardButton("📅 Вчера")],
        [KeyboardButton("📅 Выбрать дату"), KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "📋 Детальный лог выполнения\n\n"
        "Выберите дату:",
        reply_markup=reply_markup
    )
    return SELECT_STATS_DATE

async def select_stats_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора периода для статистики"""
    period_text = update.message.text
    today = date.today()
    
    if period_text == "📅 За неделю":
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
    elif period_text == "📅 За месяц":
        start_date = date(today.year, today.month, 1)
        next_month = today.month + 1 if today.month < 12 else 1
        next_year = today.year if today.month < 12 else today.year + 1
        end_date = date(next_year, next_month, 1) - timedelta(days=1)
    elif period_text == "📅 Произвольный период":
        await update.message.reply_text(
            "Введите период в формате:\n"
            "ГГГГ-ММ-ДД ГГГГ-ММ-ДД\n\n"
            "Например: 2024-01-01 2024-01-31"
        )
        return SELECT_STATS_PERIOD
    else:
        await update.message.reply_text("❌ Неизвестный период")
        return SELECT_STATS_PERIOD
    
    context.user_data['start_date'] = start_date
    context.user_data['end_date'] = end_date
    
    return await generate_stats_report(update, context)

async def handle_custom_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка произвольного периода"""
    try:
        start_str, end_str = update.message.text.split()
        start_date = datetime.strptime(start_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_str, '%Y-%m-%d').date()
        
        if start_date > end_date:
            await update.message.reply_text("❌ Начальная дата не может быть больше конечной")
            return SELECT_STATS_PERIOD
        
        context.user_data['start_date'] = start_date
        context.user_data['end_date'] = end_date
        
        return await generate_stats_report(update, context)
        
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат даты. Используйте:\n"
            "ГГГГ-ММ-ДД ГГГГ-ММ-ДД\n\n"
            "Например: 2024-01-01 2024-01-31"
        )
        return SELECT_STATS_PERIOD

async def select_stats_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора даты для детального лога"""
    date_text = update.message.text
    today = date.today()
    
    if date_text == "📅 Сегодня":
        target_date = today
    elif date_text == "📅 Вчера":
        target_date = today - timedelta(days=1)
    elif date_text == "📅 Выбрать дату":
        await update.message.reply_text(
            "Введите дату в формате ГГГГ-ММ-ДД\n\n"
            "Например: 2024-01-15"
        )
        return SELECT_STATS_DATE
    else:
        # Пытаемся распарсить дату
        try:
            target_date = datetime.strptime(date_text, '%Y-%m-%d').date()
        except ValueError:
            await update.message.reply_text(
                "❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД\n\n"
                "Например: 2024-01-15"
            )
            return SELECT_STATS_DATE
    
    context.user_data['target_date'] = target_date
    
    # Запрашиваем точку
    keyboard = [
        [KeyboardButton("ДЕ"), KeyboardButton("УЯ")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"📅 Дата: {target_date.strftime('%d.%m.%Y')}\n\n"
        "Выберите точку:",
        reply_markup=reply_markup
    )
    return SELECT_STATS_POINT

async def select_stats_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора точки для детального лога"""
    point = update.message.text
    if point not in ['ДЕ', 'УЯ']:
        await update.message.reply_text("❌ Выберите точку: ДЕ или УЯ")
        return SELECT_STATS_POINT
    
    target_date = context.user_data.get('target_date')
    if not target_date:
        await update.message.reply_text("❌ Дата не установлена")
        return await stats_detailed_log(update, context)
    
    # Генерируем детальный лог
    detailed_log = get_detailed_log(target_date, point)
    
    if not detailed_log:
        await update.message.reply_text(
            f"📋 Детальный лог выполнения\n\n"
            f"📍 Точка: {point}\n"
            f"📅 Дата: {target_date.strftime('%d.%m.%Y')}\n\n"
            "❌ Данные не найдены"
        )
        return await checklist_stats_menu(update, context)
    
    response = f"📋 Детальный лог выполнения\n\n"
    response += f"📍 Точка: {point}\n"
    response += f"📅 Дата: {target_date.strftime('%d.%m.%Y')}\n\n"
    
    for task_log in detailed_log:
        status = "✅" if task_log['completed'] else "❌"
        response += f"{status} {task_log['task_description']}\n"
        
        if task_log['completed']:
            for completion in task_log['completions']:
                response += f"   👤 {completion['completed_by']} в {completion['completed_at']}\n"
        response += "\n"
    
    # Разбиваем сообщение если оно слишком длинное
    if len(response) > 4000:
        parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
        for part in parts:
            await update.message.reply_text(part)
    else:
        await update.message.reply_text(response)
    
    return await checklist_stats_menu(update, context)

async def generate_stats_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерация отчета статистики"""
    stats_type = context.user_data.get('stats_type')
    start_date = context.user_data.get('start_date')
    end_date = context.user_data.get('end_date')
    
    if not all([stats_type, start_date, end_date]):
        await update.message.reply_text("❌ Ошибка: не установлены параметры отчета")
        return await checklist_stats_menu(update, context)
    
    period_text = format_stats_period(start_date, end_date)
    
    if stats_type == 'individual':
        stats_data = get_individual_stats(start_date, end_date)
        response = f"👤 Индивидуальная статистика\n\nПериод: {period_text}\n\n"
        
        # Группируем по пользователям
        user_stats = {}
        for stat in stats_data:
            if stat['user_name'] not in user_stats:
                user_stats[stat['user_name']] = []
            user_stats[stat['user_name']].append(stat)
        
        for user_name, user_data in user_stats.items():
            response += f"👤 {user_name}:\n"
            for stat in user_data:
                weekday_name = get_weekday_name(stat['weekday'])
                response += f"   {weekday_name}: {stat['completed_tasks']}/{stat['total_tasks']} ({stat['completion_percent']}%)\n"
            response += "\n"
    
    elif stats_type == 'point':
        stats_data = get_point_stats(start_date, end_date)
        response = f"📍 Статистика по точкам\n\nПериод: {period_text}\n\n"
        
        # Группируем по точкам
        point_stats = {}
        for stat in stats_data:
            if stat['point'] not in point_stats:
                point_stats[stat['point']] = []
            point_stats[stat['point']].append(stat)
        
        for point_name, point_data in point_stats.items():
            response += f"📍 {point_name}:\n"
            for stat in point_data:
                weekday_name = get_weekday_name(stat['weekday'])
                response += f"   {weekday_name}:\n"
                response += f"     🌅 Утро: {stat['morning_avg_completion']}% ({stat['morning_shift_count']} смен)\n"
                response += f"     🌆 Вечер: {stat['evening_avg_completion']}% ({stat['evening_shift_count']} смен)\n"
                if stat['hybrid_shift_count'] > 0:
                    response += f"     🔄 Пересмен: {stat['hybrid_avg_completion']}% ({stat['hybrid_shift_count']} смен)\n"
            response += "\n"
    
    elif stats_type == 'task':
        stats_data = get_task_stats(start_date, end_date)
        response = f"📝 Статистика по заданиям\n\nПериод: {period_text}\n\n"
        
        for stat in stats_data:
            weekday_name = get_weekday_name(stat['day_of_week'])
            response += f"📍 {stat['point']} | {weekday_name} | {stat['shift_type']}\n"
            response += f"   {stat['task_description']}\n"
            response += f"   Выполнено: {stat['completed_shifts']}/{stat['total_shifts']} ({stat['completion_percent']}%)\n\n"
    
    else:
        await update.message.reply_text("❌ Неизвестный тип статистики")
        return await checklist_stats_menu(update, context)
    
    # Разбиваем сообщение если оно слишком длинное
    if len(response) > 4000:
        parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
        for part in parts:
            await update.message.reply_text(part)
    else:
        await update.message.reply_text(response)
    
    return await checklist_stats_menu(update, context)

# Обновим функцию checklist_stats в checklist_management.py
async def checklist_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика выполнения - теперь перенаправляет в меню статистики"""
    return await checklist_stats_menu(update, context)

# Обновим states в get_checklist_management_handler()
def get_checklist_management_handler():
    """Возвращает ConversationHandler для управления чек-листами"""
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^⚙️ Управление чек-листами$"), checklist_management_start)
        ],
        states={
            STATS_MENU: [
                MessageHandler(filters.Regex("^👤 Индивидуальная статистика$"), stats_individual),
                MessageHandler(filters.Regex("^📍 Статистика по точкам$"), stats_point),
                MessageHandler(filters.Regex("^📝 Статистика по заданиям$"), stats_task),
                MessageHandler(filters.Regex("^📋 Детальный лог за день$"), stats_detailed_log),
                MessageHandler(filters.Regex("^⬅️ Назад$"), checklist_management_start),
            ],
            SELECT_STATS_PERIOD: [
                MessageHandler(filters.Regex("^(📅 За неделю|📅 За месяц|📅 Произвольный период)$"), select_stats_period),
                MessageHandler(filters.Regex("^⬅️ Назад$"), checklist_stats_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_period),
            ],
            SELECT_STATS_DATE: [
                MessageHandler(filters.Regex("^(📅 Сегодня|📅 Вчера|📅 Выбрать дата)$"), select_stats_date),
                MessageHandler(filters.Regex("^⬅️ Назад$"), checklist_stats_menu),
                MessageHandler(filters.TEXT & ~filters.COMMAND, select_stats_date),
            ],
            SELECT_STATS_POINT: [
                MessageHandler(filters.Regex("^(ДЕ|УЯ)$"), select_stats_point),
                MessageHandler(filters.Regex("^⬅️ Назад$"), stats_detailed_log),
            ],
            # ... остальные состояния для управления шаблонами ...
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CommandHandler("start", start_cancel_conversation),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_conversation),
        ],
        allow_reentry=True
    )