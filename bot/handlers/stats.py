from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters
from bot.database.stats_queries import get_period_stats, get_custom_period_stats
from datetime import datetime

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats - показывает меню выбора периода"""
    keyboard = [
        [KeyboardButton("📊 За неделю"), KeyboardButton("📈 За месяц")],
        [KeyboardButton("📅 За год"), KeyboardButton("🗓️ Произвольный период")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "📈 Выберите период для статистики:",
        reply_markup=reply_markup
    )

async def show_weekly_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика за неделю"""
    await show_stats(update, 'week', "неделю")

async def show_monthly_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика за месяц"""
    await show_stats(update, 'month', "месяц")

async def show_yearly_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика за год"""
    await show_stats(update, 'year', "год")

async def ask_custom_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрос произвольного периода"""
    await update.message.reply_text(
        "📅 Введите период в формате:\n"
        "ГГГГ-ММ-ДД ГГГГ-ММ-ДД\n\n"
        "Например:\n"
        "2024-01-01 2024-01-31\n"
        "для статистики за январь 2024 года"
    )

async def handle_custom_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка произвольного периода"""
    try:
        text = update.message.text
        start_date, end_date = text.split()
        
        # Проверяем корректность дат
        datetime.strptime(start_date, '%Y-%m-%d')
        datetime.strptime(end_date, '%Y-%m-%d')
        
        stats = get_custom_period_stats(start_date, end_date)
        await format_and_send_stats(update, stats, f"период с {start_date} по {end_date}")
        
    except ValueError as e:
        await update.message.reply_text(
            "❌ Неверный формат даты. Используйте:\n"
            "ГГГГ-ММ-ДД ГГГГ-ММ-ДД\n\n"
            "Пример: 2024-01-01 2024-01-31"
        )

async def show_stats(update: Update, period: str, period_name: str):
    """Показывает статистику за указанный период"""
    stats = get_period_stats(period)
    await format_and_send_stats(update, stats, period_name)

async def format_and_send_stats(update: Update, stats: list, period_name: str):
    """Форматирует и отправляет статистику"""
    if not stats:
        await update.message.reply_text(
            f"📭 За {period_name} данных нет.\n"
            "Проведите первые оценки напитков!"
        )
        return
    
    # Создаем красивую таблицу
    header = "📊 Статистика по бариста (за {}):\n\n".format(period_name)
    header += "{:<15} {:<6} {:<6} {:<6} {:<6} {:<6} {:<6} {:<6} {:<6}\n".format(
        "Бариста", "Эспр", "Ср", "Фил", "Ср", "Мол", "Ср", "Всего", "Общ.ср"
    )
    header += "─" * 80 + "\n"
    
    response = header
    
    for row in stats:
        (barista, espresso_count, espresso_avg, filter_count, 
         filter_avg, milk_count, milk_avg, total_count, total_avg) = row
        
        # Заменяем None на 0
        espresso_avg = espresso_avg or 0
        filter_avg = filter_avg or 0
        milk_avg = milk_avg or 0
        total_avg = total_avg or 0
        
        response += "{:<15} {:<6} {:<6} {:<6} {:<6} {:<6} {:<6} {:<6} {:<6}\n".format(
            barista[:14],
            espresso_count or 0,
            espresso_avg,
            filter_count or 0, 
            filter_avg,
            milk_count or 0,
            milk_avg,
            total_count,
            total_avg
        )
    
    # Добавляем итоги
    total_espresso = sum(row[1] or 0 for row in stats)
    total_filter = sum(row[3] or 0 for row in stats)
    total_milk = sum(row[5] or 0 for row in stats)
    grand_total = sum(row[7] for row in stats)
    
    response += "─" * 80 + "\n"
    response += "{:<15} {:<6} {:<6} {:<6} {:<6} {:<6} {:<6} {:<6}\n".format(
        "ВСЕГО:",
        total_espresso, "", 
        total_filter, "",
        total_milk, "",
        grand_total
    )
    
    # Отправляем сообщение (разбиваем если слишком длинное)
    if len(response) > 4000:
        parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
        for part in parts:
            await update.message.reply_text(f"```\n{part}\n```", parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(f"```\n{response}\n```", parse_mode='MarkdownV2')
    
    # Добавляем пояснение
    explanation = (
        "\n📝 Пояснение:\n"
        "• Эспр - количество эспрессо\n"
        "• Фил - количество фильтр-кофе\n" 
        "• Мол - количество молочных напитков\n"
        "• Ср - средняя оценка\n"
        "• Всего - общее количество оценок\n"
        "• Общ.ср - общая средняя оценка"
    )
    await update.message.reply_text(explanation)

# Для регистрации в main.py
def get_stats_handlers():
    """Возвращает обработчики для статистики"""
    return [
        MessageHandler(filters.Regex("^📊 За неделю$"), show_weekly_stats),
        MessageHandler(filters.Regex("^📈 За месяц$"), show_monthly_stats),
        MessageHandler(filters.Regex("^📅 За год$"), show_yearly_stats),
        MessageHandler(filters.Regex("^🗓️ Произвольный период$"), ask_custom_period),
        MessageHandler(filters.Regex(r'^\d{4}-\d{2}-\d{2} \d{4}-\d{2}-\d{2}$'), handle_custom_period),
    ]