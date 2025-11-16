"""Обработчики для работы с расписанием и заменами"""
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters, CommandHandler, CallbackQueryHandler
from bot.utils.auth import require_roles, ROLE_MENTOR, ROLE_SENIOR
from bot.database.user_operations import get_user_by_telegram_id, get_all_users
from bot.database.schedule_operations import (
    get_upcoming_shifts_by_iiko_id, update_shift_iiko_id, get_shift_by_id,
    get_shifts_by_iiko_id, create_shift, update_shift
)
from bot.utils.google_sheets import (
    parse_schedule_from_sheet, get_current_month_name, get_next_month_name
)
from bot.keyboards.menus import get_main_menu
from datetime import date, datetime, timedelta
import logging

logger = logging.getLogger(__name__)

# Состояния для замен
(SWAP_MENU, SELECTING_SHIFT_TO_SWAP, SELECTING_EMPLOYEE, CONFIRMING_RETURN_SWAP, SELECTING_RETURN_SHIFT) = range(5)

# Состояния для настроек расписания
(SCHEDULE_MENU, PARSING_MONTH, SELECTING_EMPLOYEE_FOR_SHIFTS, VIEWING_SHIFTS,
 ADDING_SHIFT_DATE, ADDING_SHIFT_IIKO_ID, ADDING_SHIFT_POINT, ADDING_SHIFT_TYPE,
 ADDING_SHIFT_START, ADDING_SHIFT_END, EDITING_SHIFT_ID, EDITING_SHIFT_FIELD) = range(12)

async def swap_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню замен"""
    user = update.effective_user
    db_user = get_user_by_telegram_id(user.id)
    
    # Если не нашли по telegram_id, пробуем по username
    if not db_user and user.username:
        from bot.database.user_operations import get_user_by_username
        db_user = get_user_by_username(user.username)
    
    if not db_user or not db_user.iiko_id:
        await update.message.reply_text(
            "❌ Ваш аккаунт не найден в системе или не указан iiko_id. Обратитесь к администратору."
        )
        return ConversationHandler.END
    
    # Получаем ближайшие смены пользователя
    shifts = get_upcoming_shifts_by_iiko_id(str(db_user.iiko_id), days=30)
    
    if not shifts:
        await update.message.reply_text(
            "📅 У вас нет ближайших смен для замены.",
            reply_markup=get_main_menu()
        )
        return ConversationHandler.END
    
    # Формируем список смен с кнопками
    keyboard = []
    text = "🔄 Выберите смену для замены:\n\n"
    
    for shift in shifts[:20]:  # Ограничиваем 20 сменами
        if not shift.shift_type_obj:
            continue
        shift_type_names = {
            'morning': '🌅 Утро',
            'hybrid': '🌤️ Гибрид',
            'evening': '🌆 Вечер'
        }
        shift_type_text = shift_type_names.get(shift.shift_type_obj.shift_type, shift.shift_type_obj.shift_type)
        date_str = shift.shift_date.strftime("%d.%m.%Y")
        start_str = shift.shift_type_obj.start_time.strftime("%H:%M")
        end_str = shift.shift_type_obj.end_time.strftime("%H:%M")
        
        text += f"• {date_str} ({shift_type_text}) {shift.shift_type_obj.point}: {start_str} - {end_str}\n"
        keyboard.append([InlineKeyboardButton(
            f"{date_str} {shift.shift_type_obj.point} {start_str}",
            callback_data=f"swap_shift_{shift.shift_id}"
        )])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_swap")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup)
    return SWAP_MENU

async def handle_swap_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора смены для замены"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel_swap":
        await query.edit_message_text("❌ Замена отменена")
        return ConversationHandler.END
    
    if query.data.startswith("swap_shift_"):
        shift_id = int(query.data.split("_")[2])
        context.user_data['swap_shift_id'] = shift_id
        
        # Получаем список всех активных пользователей
        users = get_all_users(active_only=True)
        users_with_iiko = [u for u in users if u.iiko_id]
        
        if not users_with_iiko:
            await query.edit_message_text("❌ Нет доступных сотрудников для замены")
            return ConversationHandler.END
        
        # Формируем список сотрудников
        keyboard = []
        text = "👥 Выберите сотрудника для замены:\n\n"
        
        for user in users_with_iiko:
            text += f"• {user.name}\n"
            keyboard.append([InlineKeyboardButton(
                user.name,
                callback_data=f"swap_employee_{user.iiko_id}"
            )])
        
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_swap")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)
        return SELECTING_EMPLOYEE

async def handle_employee_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора сотрудника для замены"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel_swap":
        await query.edit_message_text("❌ Замена отменена")
        return ConversationHandler.END
    
    if query.data.startswith("swap_employee_"):
        new_iiko_id = query.data.split("_")[2]
        shift_id = context.user_data.get('swap_shift_id')
        
        if not shift_id:
            await query.edit_message_text("❌ Ошибка: смена не выбрана")
            return ConversationHandler.END
        
        # Меняем iiko_id в смене
        shift = update_shift_iiko_id(shift_id, new_iiko_id)
        
        if not shift:
            await query.edit_message_text("❌ Ошибка при замене смены")
            return ConversationHandler.END
        
        # Получаем имя нового сотрудника
        from bot.database.user_operations import get_user_by_iiko_id
        new_employee = get_user_by_iiko_id(int(new_iiko_id))
        employee_name = new_employee.name if new_employee else new_iiko_id
        
        await query.edit_message_text(
            f"✅ Смена успешно передана сотруднику: {employee_name}\n\n"
            "Забираете смену в ответ?"
        )
        
        context.user_data['swap_new_iiko_id'] = new_iiko_id
        context.user_data['swap_employee_name'] = employee_name
        
        keyboard = [
            [InlineKeyboardButton("✅ Да", callback_data="swap_return_yes")],
            [InlineKeyboardButton("❌ Нет", callback_data="swap_return_no")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("Выберите действие:", reply_markup=reply_markup)
        return CONFIRMING_RETURN_SWAP

async def handle_return_swap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ответа на вопрос о замене в ответ"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "swap_return_no":
        await query.edit_message_text("✅ Замена завершена")
        context.user_data.clear()
        return ConversationHandler.END
    
    if query.data == "swap_return_yes":
        # Показываем смены выбранного сотрудника
        new_iiko_id = context.user_data.get('swap_new_iiko_id')
        employee_name = context.user_data.get('swap_employee_name', 'Сотрудник')
        
        if not new_iiko_id:
            await query.edit_message_text("❌ Ошибка: сотрудник не выбран")
            return ConversationHandler.END
        
        shifts = get_upcoming_shifts_by_iiko_id(str(new_iiko_id), days=30)
        
        if not shifts:
            await query.edit_message_text(
                f"📅 У {employee_name} нет ближайших смен для обмена."
            )
            context.user_data.clear()
            return ConversationHandler.END
        
        # Формируем список смен
        keyboard = []
        text = f"🔄 Выберите смену {employee_name} для обмена:\n\n"
        
        for shift in shifts[:20]:
            if not shift.shift_type_obj:
                continue
            shift_type_names = {
                'morning': '🌅 Утро',
                'hybrid': '🌤️ Пересмен',
                'evening': '🌆 Вечер'
            }
            shift_type_text = shift_type_names.get(shift.shift_type_obj.shift_type, shift.shift_type_obj.shift_type)
            date_str = shift.shift_date.strftime("%d.%m.%Y")
            start_str = shift.shift_type_obj.start_time.strftime("%H:%M")
            end_str = shift.shift_type_obj.end_time.strftime("%H:%M")
            
            text += f"• {date_str} ({shift_type_text}) {shift.shift_type_obj.point}: {start_str} - {end_str}\n"
            keyboard.append([InlineKeyboardButton(
                f"{date_str} {shift.shift_type_obj.point} {start_str}",
                callback_data=f"swap_return_shift_{shift.shift_id}"
            )])
        
        keyboard.append([InlineKeyboardButton("❌ Пропустить", callback_data="swap_return_no")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)
        return SELECTING_RETURN_SHIFT

async def handle_return_shift_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора смены для обмена в ответ"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "swap_return_no":
        await query.edit_message_text("✅ Замена завершена")
        context.user_data.clear()
        return ConversationHandler.END
    
    if query.data.startswith("swap_return_shift_"):
        return_shift_id = int(query.data.split("_")[3])
        user = update.effective_user
        db_user = get_user_by_telegram_id(user.id)
        
        # Если не нашли по telegram_id, пробуем по username
        if not db_user and user.username:
            from bot.database.user_operations import get_user_by_username
            db_user = get_user_by_username(user.username)
        
        if not db_user or not db_user.iiko_id:
            await query.edit_message_text("❌ Ошибка: ваш аккаунт не найден")
            context.user_data.clear()
            return ConversationHandler.END
        
        # Меняем iiko_id в смене на текущего пользователя
        shift = update_shift_iiko_id(return_shift_id, str(db_user.iiko_id))
        
        if not shift:
            await query.edit_message_text("❌ Ошибка при обмене смены")
            context.user_data.clear()
            return ConversationHandler.END
        
        await query.edit_message_text("✅ Обмен сменами успешно завершен!")
        context.user_data.clear()
        return ConversationHandler.END

async def cancel_swap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена замены"""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Замена отменена.",
        reply_markup=get_main_menu()
    )
    return ConversationHandler.END

def get_swap_conversation_handler():
    """Возвращает ConversationHandler для замен"""
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🔄 Замены$"), swap_menu)
        ],
        states={
            SWAP_MENU: [
                CallbackQueryHandler(handle_swap_callback, pattern="^(swap_shift_|cancel_swap)"),
            ],
            SELECTING_EMPLOYEE: [
                CallbackQueryHandler(handle_employee_selection, pattern="^(swap_employee_|cancel_swap)"),
            ],
            CONFIRMING_RETURN_SWAP: [
                CallbackQueryHandler(handle_return_swap, pattern="^(swap_return_yes|swap_return_no)"),
            ],
            SELECTING_RETURN_SHIFT: [
                CallbackQueryHandler(handle_return_shift_selection, pattern="^(swap_return_shift_|swap_return_no)"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_swap),
            CommandHandler("start", cancel_swap),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_swap),
        ],
        allow_reentry=True
    )

