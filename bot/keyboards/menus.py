from telegram import ReplyKeyboardMarkup, KeyboardButton

def get_main_menu():
    """Главное меню"""
    keyboard = [
        [KeyboardButton("☕ Оценить напиток"), KeyboardButton("📊 Статистика")],
        [KeyboardButton("🧹 Контроль чистоты"), KeyboardButton("📝 Чек-лист смены")],
        [KeyboardButton("⚙️ Настройки")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_stats_menu():
    """Меню статистики"""
    keyboard = [
        [KeyboardButton("📊 За неделю"), KeyboardButton("📈 За месяц")],
        [KeyboardButton("📅 За год"), KeyboardButton("🗓️ Произвольный период")],
        [KeyboardButton("⬅️ Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)