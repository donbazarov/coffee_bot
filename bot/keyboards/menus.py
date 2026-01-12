from telegram import ReplyKeyboardMarkup, KeyboardButton

def get_main_menu():
    """Главное меню"""
    keyboard = [
        [KeyboardButton("📆 Мои смены"), KeyboardButton("🔄 Замены")],
        [KeyboardButton("📝 Чек-лист смены"), KeyboardButton("💎 Контроль качества")],
        [KeyboardButton("📦 Другое"), KeyboardButton("⚙️ Настройки")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_qc_menu():
    """Меню контроля качества"""
    keyboard = [
        [KeyboardButton("☕ Оценить напиток"), KeyboardButton("🧹 Контроль чистоты")],
        [KeyboardButton("📊 Статистика по напиткам"), KeyboardButton("⬅️ Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_other_menu():
    """Меню другого"""
    keyboard = [
        [KeyboardButton("⬅️ Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_santa_menu():
    """Меню тайного санты"""
    keyboard = [
        [KeyboardButton("✅ Участвую"), KeyboardButton("❌ Не участвую")],
        [KeyboardButton("📝 Мой вишлист"), KeyboardButton("🎁 Чей я Санта")],
        [KeyboardButton("⬅️ Назад")]
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
