import logging
import sqlite3
import os
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
SELECTING_BARISTA, SELECTING_POINT, SELECTING_CATEGORY = range(3)

# Данные пользователей
users = [
    # Баристы
    {'name': 'Настя', 'id': 222559, 'role': 'barista'},
    {'name': 'Богдана', 'id': 901953, 'role': 'barista'},
    {'name': 'Польза', 'id': 400481, 'role': 'barista'},
    {'name': 'Катя', 'id': 901927, 'role': 'barista'},
    {'name': 'Паша', 'id': 20441, 'role': 'barista'},
    {'name': 'Аида', 'id': 400487, 'role': 'barista'},
    {'name': 'Ева', 'id': 70622, 'role': 'barista'},
    {'name': 'Мердан', 'id': 222556, 'role': 'barista'},
    {'name': 'Стас', 'id': 909333, 'role': 'barista'},
    {'name': 'Камила', 'id': 222668, 'role': 'barista'},
    # Наставники с Telegram username
    {'name': 'Ди', 'id': 222557, 'role': 'respondent', 'telegram_username': 'drodonit'},
    {'name': 'Дон', 'id': 90944, 'role': 'respondent', 'telegram_username': 'don22487'},
    {'name': 'Софа', 'id': 400482, 'role': 'respondent', 'telegram_username': 'SophiaLavkraft'}
]

points = ['ДЕ', 'УЯ']

class CoffeeQualityBot:
    def __init__(self, token):
        self.token = token
        self.application = Application.builder().token(token).build()
        self.init_database()
        self.setup_handlers()
    
    def init_database(self):
        """Инициализация базы данных"""
        conn = sqlite3.connect('coffee_quality.db')
        cursor = conn.cursor()
        
        # Таблица для оценок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS drink_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                respondent_name TEXT NOT NULL,
                barista_name TEXT NOT NULL,
                point TEXT NOT NULL,
                category TEXT NOT NULL,
                drink_type TEXT,
                balance INTEGER,
                bouquet INTEGER,
                body INTEGER,
                aftertaste INTEGER,
                foam INTEGER,
                latte_art INTEGER,
                photo_path TEXT,
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info("База данных инициализирована")
    
    def setup_handlers(self):
        """Настройка обработчиков команд"""
        # Сначала обработчики команд
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        self.application.add_handler(CommandHandler("cancel", self.cancel_command))
        
        # ConversationHandler для оценки напитков - ДОБАВЛЯЕМ ПЕРВЫМ!
        review_conv_handler = ConversationHandler(
            entry_points=[
                MessageHandler(filters.Regex("^☕ Оценить напиток$"), self.start_review),
                CommandHandler("review", self.start_review)
            ],
            states={
                SELECTING_BARISTA: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.select_barista)
                ],
                SELECTING_POINT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.select_point)
                ],
                SELECTING_CATEGORY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.select_category)
                ],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_review)],
            allow_reentry=True
        )
        
        self.application.add_handler(review_conv_handler)
        
        # Общий обработчик сообщений - ДОБАВЛЯЕМ ПОСЛЕДНИМ!
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start - главное меню"""
        user = update.effective_user
        
        # Проверяем, является ли пользователь наставником по username
        respondent_usernames = [user.get('telegram_username') for user in users 
                               if user['role'] == 'respondent' and user.get('telegram_username')]
        
        user_username = user.username if user.username else ""
        
        if user_username not in respondent_usernames:
            await update.message.reply_text(
                "❌ У вас нет доступа к этому боту. Обратитесь к администратору.\n"
                f"Ваш username: @{user_username if user_username else 'не установлен'}"
            )
            return
        
        keyboard = [
            [KeyboardButton("☕ Оценить напиток"), KeyboardButton("📊 Статистика")],
            [KeyboardButton("🧹 Контроль чистоты"), KeyboardButton("⚙️ Настройки")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        # Находим имя наставника по username
        respondent_name = next((user['name'] for user in users 
                              if user.get('telegram_username') == user_username), user.first_name)
        
        await update.message.reply_text(
            f"Привет, {respondent_name}! 👋\n"
            "Я помогу с оценкой качества напитков и контролем в кофейнях.\n"
            "Выберите действие:",
            reply_markup=reply_markup
        )
    
    async def start_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начало оценки напитка"""
        # Очищаем предыдущие данные
        context.user_data.clear()
        
        # Получаем список барист для клавиатуры
        baristas = [user['name'] for user in users if user['role'] == 'barista']
        keyboard = [[barista] for barista in baristas]
        keyboard.append(["❌ Отмена"])
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(
            "Выберите баристу для оценки:",
            reply_markup=reply_markup
        )
        return SELECTING_BARISTA
    
    async def select_barista(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Выбор баристы"""
        barista_name = update.message.text
        
        # Проверяем отмену
        if barista_name == "❌ Отмена":
            return await self.cancel_review(update, context)
        
        # Проверяем, что выбран существующий бариста
        barista_names = [user['name'] for user in users if user['role'] == 'barista']
        if barista_name not in barista_names:
            await update.message.reply_text("❌ Пожалуйста, выберите баристу из списка:")
            return SELECTING_BARISTA
        
        context.user_data['barista'] = barista_name
        
        # Клавиатура для выбора точки
        keyboard = [[point] for point in points]
        keyboard.append(["❌ Отмена"])
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(
            f"✅ Бариста: {barista_name}\n\nТеперь выберите точку:",
            reply_markup=reply_markup
        )
        return SELECTING_POINT
    
    async def select_point(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Выбор точки"""
        point = update.message.text
        
        # Проверяем отмену
        if point == "❌ Отмена":
            return await self.cancel_review(update, context)
        
        # Проверяем, что точка существует
        if point not in points:
            await update.message.reply_text("❌ Пожалуйста, выберите точку из списка:")
            return SELECTING_POINT
        
        context.user_data['point'] = point
        
        # Клавиатура для выбора категории напитка
        keyboard = [["Эспрессо/Фильтр"], ["Молочный напиток"]]
        keyboard.append(["❌ Отмена"])
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(
            f"✅ Точка: {point}\n\nВыберите категорию напитка:",
            reply_markup=reply_markup
        )
        return SELECTING_CATEGORY
    
    async def select_category(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Выбор категории напитка"""
        category = update.message.text
        
        # Проверяем отмену
        if category == "❌ Отмена":
            return await self.cancel_review(update, context)
        
        # Проверяем корректность категории
        valid_categories = ["Эспрессо/Фильтр", "Молочный напиток"]
        if category not in valid_categories:
            await update.message.reply_text("❌ Пожалуйста, выберите категорию из списка:")
            return SELECTING_CATEGORY
        
        context.user_data['category'] = category
        
        # Сохраняем оценку
        await self.save_review(update, context)
        return ConversationHandler.END
    
    async def save_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Сохранение оценки в базу данных"""
        data = context.user_data
        respondent_name = update.effective_user.first_name
        
        conn = sqlite3.connect('coffee_quality.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO drink_reviews 
            (respondent_name, barista_name, point, category)
            VALUES (?, ?, ?, ?)
        ''', (respondent_name, data['barista'], data['point'], data['category']))
        
        conn.commit()
        conn.close()
        
        # Главное меню
        keyboard = [
            [KeyboardButton("☕ Оценить напиток"), KeyboardButton("📊 Статистика")],
            [KeyboardButton("🧹 Контроль чистоты"), KeyboardButton("⚙️ Настройки")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(
            "✅ Оценка сохранена!\n"
            f"📋 Бариста: {data['barista']}\n"
            f"🏪 Точка: {data['point']}\n"
            f"☕ Категория: {data['category']}\n\n"
            "В следующих версиях здесь будет полная форма оценки с детальными параметрами.",
            reply_markup=reply_markup
        )
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /stats - временная заглушка"""
        await update.message.reply_text(
            "📊 Модуль статистики в разработке.\n"
            "Скоро здесь будет:\n"
            "- Таблица по баристам\n" 
            "- Средние оценки\n"
            "- Количество проверок\n\n"
            "Используйте /review для новой оценки"
        )
    
    async def cancel_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Отмена оценки"""
        context.user_data.clear()
        
        keyboard = [
            [KeyboardButton("☕ Оценить напиток"), KeyboardButton("📊 Статистика")],
            [KeyboardButton("🧹 Контроль чистоты"), KeyboardButton("⚙️ Настройки")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(
            "❌ Оценка отменена.",
            reply_markup=reply_markup
        )
        return ConversationHandler.END
    
    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /cancel"""
        return await self.cancel_review(update, context)
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка текстовых сообщений (только когда НЕ в Conversation)"""
        text = update.message.text
        
        if text == "☕ Оценить напиток":
            await self.start_review(update, context)
        elif text == "📊 Статистика":
            await self.stats_command(update, context)
        elif text == "🧹 Контроль чистоты":
            await update.message.reply_text("Модуль контроля чистоты в разработке")
        elif text == "⚙️ Настройки":
            await update.message.reply_text("Настройки в разработке")
        else:
            await update.message.reply_text(
                "Используйте кнопки меню или команды:\n"
                "/start - Главное меню\n"
                "/review - Начать оценку\n"
                "/stats - Статистика\n"
                "/cancel - Отмена"
            )
    
    def run(self):
        """Запуск бота"""
        self.application.run_polling()

# Запуск бота
if __name__ == "__main__":
    bot = CoffeeQualityBot("8531765653:AAEWDaM2crEA1ZMLoNFRLFxC-48CAxwMKOE")
    print("Бот запущен...")
    bot.run()