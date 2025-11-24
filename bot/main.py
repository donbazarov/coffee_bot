import logging
import sqlite3
import os
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ConversationHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

from bot.config import BotConfig
from bot.database.simple_db import init_db
from bot.handlers.review import get_review_conversation_handler
from bot.handlers.stats import stats_command, get_stats_handlers
from bot.handlers.settings import get_settings_conversation_handler
from bot.handlers.checklist import get_checklist_conversation_handler
from bot.handlers.schedule import get_swap_conversation_handler
from bot.keyboards.menus import get_main_menu
from bot.utils.auth import is_mentor, is_senior_or_mentor, get_user_role
from bot.utils.common_handlers import cancel_conversation
from bot.database.user_operations import get_user_by_username
from bot.database.schedule_operations import get_upcoming_shifts_by_iiko_id
from datetime import date, timedelta

# Настройка логирования с обработкой ошибок
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class CoffeeBot:
    def __init__(self):
        self.application = Application.builder().token(BotConfig.token).build()
        
        # Добавляем обработчик ошибок
        self.application.add_error_handler(self.error_handler)
        
        init_db()
        self.setup_handlers()
    
    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик ошибок с детальным логированием"""
        logger.error(msg="Exception while handling an update:", exc_info=context.error)
    
        # Детальное логирование для отладки
        if update and hasattr(update, 'effective_message'):
            logger.error(f"Update content: {update.effective_message.text if update.effective_message else 'No message'}")
    
        # Отправляем сообщение пользователю
        if update and hasattr(update, 'effective_chat'):
            error_text = str(context.error)
            text = (
                "❌ Произошла ошибка. Пожалуйста, попробуйте еще раз или обратитесь к администратору.\n"
                f"Ошибка: {error_text[:100]}..."
            )
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text
            )
    
    def setup_handlers(self):
        """Настройка обработчиков"""
        print("🔄 Настройка обработчиков бота...")
              
        # Основные команды
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("stats", stats_command))
        self.application.add_handler(CommandHandler("cancel", self.cancel_command))
        
         # Обработчики статистики
        stats_handlers = get_stats_handlers()
        for handler in stats_handlers:
            self.application.add_handler(handler)
        
        # Отладочные команды
        self.application.add_handler(CommandHandler("show_db", self.show_db_command))
        self.application.add_handler(CommandHandler("stats_debug", self.stats_debug_command))
        self.application.add_handler(CommandHandler("show_photo", self.show_photo_command))
        
        # ConversationHandler для оценки напитков
        self.application.add_handler(get_review_conversation_handler())
        
        # ConversationHandler для настроек
        self.application.add_handler(get_settings_conversation_handler())
        
        # ConversationHandler для замен
        self.application.add_handler(get_swap_conversation_handler())
        
        # ВРЕМЕННО: Глобальный обработчик callback_query для отладки
        #self.application.add_handler(CallbackQueryHandler(self.debug_callback))
        
        # ConversationHandler для чек-листов
        self.application.add_handler(get_checklist_conversation_handler())
        
        # Общий обработчик сообщений
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        
        print("✅ Все обработчики настроены!")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        user = update.effective_user
        
        # Очищаем данные пользователя при /start (на случай застопоривания)
        context.user_data.clear()
        
        # Получаем пользователя по username
        db_user = None
        
        if user.username:
            db_user = get_user_by_username(user.username)
        
        # Формируем приветствие в зависимости от роли
        if db_user:
            role_names = {
                'barista': '☕ Бариста',
                'senior': '⭐ Старший', 
                'mentor': '👨‍🏫 Наставник'
            }
            role_text = role_names.get(db_user.role, 'Пользователь')
            greeting = f"Привет, {user.first_name}! 👋\n\nВы вошли как: {role_text}"
            
            # Получаем ближайшие смены на неделю
            if db_user.iiko_id:
                shifts = get_upcoming_shifts_by_iiko_id(str(db_user.iiko_id), days=7)
                if shifts:
                    greeting += "\n\n📅 Ваши ближайшие смены на неделю:\n"
                    for shift in shifts:
                        if not shift.shift_type_obj:
                            continue
                        shift_type_names = {
                            'morning': '🌅 Утро',
                            'hybrid': '🌤️ Пересмен',
                            'evening': '🌆 Вечер'
                        }
                        shift_type_text = shift_type_names.get(shift.shift_type_obj.shift_type, shift.shift_type_obj.shift_type)
                        date_str = shift.shift_date.strftime("%d.%m")
                        start_str = shift.shift_type_obj.start_time.strftime("%H:%M")
                        end_str = shift.shift_type_obj.end_time.strftime("%H:%M")
                        greeting += f"• {date_str} ({shift_type_text}) {shift.shift_type_obj.point}: {start_str} - {end_str}\n"
                else:
                    greeting += "\n\n📅 Ближайших смен не найдено"
        else:
            greeting = f"Привет, {user.first_name}! 👋\n\n⚠️ Ваш аккаунт не найден в системе. Обратитесь к администратору."
        
        reply_markup = get_main_menu()
        await update.message.reply_text(
            greeting,
            reply_markup=reply_markup
        )
    
    async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /cancel"""
        return await cancel_conversation(update, context)
        
    async def show_db_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /show_db - показать содержимое базы данных"""
        try:
            conn = sqlite3.connect('coffee_quality.db')
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM drink_reviews ORDER BY id DESC LIMIT 5")
            records = cursor.fetchall()
            conn.close()
            
            if not records:
                await update.message.reply_text("📭 База данных пуста")
                return
            
            response = "📊 Последние 5 записей:\n\n"
            for record in records:
                (id, respondent, barista, point, category, drink_type, 
                 balance, bouquet, body, aftertaste, foam, latte_art, 
                 photo_file_id, comment, created_at) = record
                
                response += f"🆔 ID: {id}\n"
                response += f"👤 Наставник: {respondent}\n"
                response += f"☕ Бариста: {barista}\n"
                response += f"🏪 Точка: {point}\n"
                response += f"📋 Категория: {category}\n"
                
                if drink_type:
                    response += f"🍵 Напиток: {drink_type}\n"
                
                if category == "Эспрессо/Фильтр":
                    if balance: response += f"⚖️ Баланс: {balance}/5\n"
                    if bouquet: response += f"🌿 Букет: {bouquet}/5\n"
                    if body: response += f"🏋️ Тело: {body}/5\n"
                    if aftertaste: response += f"🎭 Послевкусие: {aftertaste}/5\n"
                else:
                    if balance: response += f"⚖️ Баланс: {balance}/5\n"
                    if bouquet: response += f"🌿 Букет: {bouquet}/5\n"
                    if foam: response += f"🥛 Пена: {foam}/5\n"
                    if latte_art: response += f"🎨 Латте-арт: {latte_art}/5\n"
                
                if photo_file_id:
                    response += f"📷 Фото: {photo_file_id[:30]}...\n"
                else:
                    response += "📷 Фото: нет\n"
                
                if comment and comment != '-':
                    response += f"💬 Комментарий: {comment}\n"
                
                response += f"🕐 Дата: {created_at}\n"
                response += "─" * 30 + "\n\n"
            
            await update.message.reply_text(response)
            
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def stats_debug_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /stats_debug - отладочная статистика"""
        try:
            conn = sqlite3.connect('coffee_quality.db')
            cursor = conn.cursor()
            
            # Общее количество
            cursor.execute("SELECT COUNT(*) FROM drink_reviews")
            total_count = cursor.fetchone()[0]
            
            # По категориям
            cursor.execute("SELECT category, COUNT(*) FROM drink_reviews GROUP BY category")
            category_stats = cursor.fetchall()
            
            # По бариста
            cursor.execute("SELECT barista_name, COUNT(*) FROM drink_reviews GROUP BY barista_name")
            barista_stats = cursor.fetchall()
            
            conn.close()
            
            response = "📈 Статистика базы данных:\n\n"
            response += f"📊 Всего записей: {total_count}\n\n"
            
            response += "☕ По категориям:\n"
            for category, count in category_stats:
                response += f"• {category}: {count}\n"
            
            response += "\n👥 По бариста:\n"
            for barista, count in barista_stats:
                response += f"• {barista}: {count}\n"
            
            await update.message.reply_text(response)
            
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def show_photo_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /show_photo [id] - показать фото по ID"""
        try:
            if not context.args:
                await update.message.reply_text("❌ Укажите ID записи: /show_photo [id]")
                return
            
            record_id = context.args[0]
            
            conn = sqlite3.connect('coffee_quality.db')
            cursor = conn.cursor()
            cursor.execute("SELECT photo_file_id FROM drink_reviews WHERE id = ?", (record_id,))
            result = cursor.fetchone()
            conn.close()
            
            if not result or not result[0]:
                await update.message.reply_text(f"❌ Для записи {record_id} фото не найдено")
                return
            
            photo_file_id = result[0]
            
            await update.message.reply_photo(
                photo=photo_file_id,
                caption=f"📷 Фото для записи {record_id}"
            )
                
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка общих сообщений"""
        text = update.message.text
    
        if text == "☕ Оценить напиток":
            # Проверяем доступ - только наставники могут оценивать
            if not is_mentor(update):
                await update.message.reply_text(
                    "❌ У вас нет доступа к этой функции.\n"
                    "Оценивать напитки могут только наставники."
                )
                return
            # ConversationHandler сам обработает это
            pass
        elif text == "📊 Статистика":
            await stats_command(update, context)
        elif text == "⬅️ Назад":
            return await cancel_conversation(update, context)
        elif text == "🧹 Контроль чистоты":
            # Проверяем доступ - только старшие и наставники
            if not is_senior_or_mentor(update):
                await update.message.reply_text(
                    "❌ У вас нет доступа к этой функции.\n"
                    "Контроль чистоты доступен только старшим и наставникам."
                )
                return
            await update.message.reply_text("Модуль контроля чистоты в разработке")
        elif text == "📝 Чек-лист смены":
            # Доступно всем
            # ConversationHandler сам обработает это
            pass
        elif text == "🔄 Замены":
            # Доступно всем
            # ConversationHandler сам обработает это
            pass
        elif text == "⚙️ Настройки":
            # Проверяем доступ - только старшие и наставники
            if not is_senior_or_mentor(update):
                await update.message.reply_text(
                    "❌ У вас нет доступа к этой функции.\n"
                    "Настройки доступны только старшим и наставникам."
                )
                return
            # ConversationHandler сам обработает это
            pass
        elif text == "⚙️ Управление чек-листами":
            # Проверяем доступ - только старшие и наставники
            if not is_senior_or_mentor(update):
                await update.message.reply_text(
                    "❌ У вас нет доступа к этой функции.\n"
                    "Управление чек-листами доступно только старшим и наставникам."
                )
                return
            # ConversationHandler сам обработает это
            pass
        else:
            await update.message.reply_text(
                "Используйте кнопки меню или команды:\n"
                "/start - Главное меню\n"
                "/review - Начать оценку\n"
                "/stats - Статистика\n"
                "/show_db - Показать базу\n"
                "/stats_debug - Статистика (отладка)\n"
                "/show_photo [id] - Показать фото\n"
                "/cancel - Отмена"
            )
            
    async def debug_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Временный отладочный обработчик"""
        query = update.callback_query
        logger.info(f"🔧 GLOBAL DEBUG: callback_data = {query.data}")
        logger.info(f"🔧 GLOBAL DEBUG: user_data = {context.user_data}")
        await query.answer(f"Debug: {query.data}")
        return ConversationHandler.END
    
    def run(self):
        """Запуск бота"""
        print("🚀 Запуск бота...")
        self.application.run_polling()

if __name__ == "__main__":
    bot = CoffeeBot()
    bot.run()