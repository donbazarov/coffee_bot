"""Общие обработчики для всех ConversationHandler'ов"""
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from bot.keyboards.menus import get_main_menu

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Универсальная функция отмены для всех ConversationHandler'ов"""
    # Очищаем данные пользователя
    context.user_data.clear()
    
    # Отправляем сообщение и возвращаем главное меню
    await update.message.reply_text(
        "❌ Действие отменено.",
        reply_markup=get_main_menu()
    )
    
    return ConversationHandler.END

async def start_cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик для /start - отменяет текущий диалог и возвращает в главное меню"""
    return await cancel_conversation(update, context)