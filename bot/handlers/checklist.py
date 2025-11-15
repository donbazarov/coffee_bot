"""Обработчики для чек-листа смены"""
from telegram import Update
from telegram.ext import ContextTypes
from bot.keyboards.menus import get_main_menu

async def checklist_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню чек-листа смены (заглушка)"""
    await update.message.reply_text(
        "📝 Чек-лист смены\n\n"
        "Эта функция находится в разработке.\n"
        "Скоро здесь будет доступен чек-лист для контроля смены.",
        reply_markup=get_main_menu()
    )

