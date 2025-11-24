"""Обработчики для работы с расписанием и заменами"""
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters, CommandHandler, CallbackQueryHandler
from bot.utils.common_handlers import cancel_conversation, start_cancel_conversation
from bot.database.user_operations import get_user_by_telegram_id, get_all_users
from bot.database.schedule_operations import (
    get_upcoming_shifts_by_iiko_id, update_shift_iiko_id, get_shift_by_id,
    get_shifts_by_iiko_id, create_shift, update_shift
)
from bot.utils.emulation import get_current_iiko_id, get_current_user_name, is_emulation_mode 
from bot.keyboards.menus import get_main_menu
import logging

logger = logging.getLogger(__name__)

# Состояния для замен
(SWAP_MENU, SELECTING_SHIFT_TO_SWAP, SELECTING_EMPLOYEE, CONFIRMING_SWAP, SELECTING_RETURN_SHIFT) = range(5)

# Состояния для настроек расписания
(SCHEDULE_MENU, PARSING_MONTH, SELECTING_EMPLOYEE_FOR_SHIFTS, VIEWING_SHIFTS,
 ADDING_SHIFT_DATE, ADDING_SHIFT_IIKO_ID, ADDING_SHIFT_POINT, ADDING_SHIFT_TYPE,
 ADDING_SHIFT_START, ADDING_SHIFT_END, EDITING_SHIFT_ID, EDITING_SHIFT_FIELD) = range(12)

async def swap_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню замен с поддержкой эмуляции"""
    # Получаем текущего пользователя (эмулированного или реального)
    current_iiko_id = get_current_iiko_id(update, context)
    current_user_name = get_current_user_name(update, context)
    
    if not current_iiko_id:
        await update.message.reply_text(
            "❌ Ваш аккаунт не найден в системе или не указан iiko_id. Обратитесь к администратору."
        )
        return ConversationHandler.END
    
    # Получаем ближайшие смены текущего пользователя
    shifts = get_upcoming_shifts_by_iiko_id(str(current_iiko_id), days=30)
    
    if not shifts:
        mode_text = " (эмуляция)" if is_emulation_mode(context) else ""
        await update.message.reply_text(
            f"📅 У {current_user_name}{mode_text} нет ближайших смен для замены.",
            reply_markup=get_main_menu()
        )
        return ConversationHandler.END
    
    # Формируем список смен с кнопками
    keyboard = []
    mode_text = " (эмуляция)" if is_emulation_mode(context) else ""
    text = f"🔄 Выберите смену для замены{mode_text}:\n\n"
    
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
            callback_data=f"swap_shift_{shift.shift_id}"
        )])
    
    # Добавляем кнопку завершения эмуляции, если в режиме эмуляции
    if is_emulation_mode(context):
        keyboard.append([InlineKeyboardButton("🔚 Завершить эмуляцию", callback_data="end_emulation")])
    else:
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_conversation")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup)
    return SWAP_MENU

async def handle_return_shift_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора смены для двустороннего обмена"""
    query = update.callback_query
    await query.answer()
    
    logger.info(f"🎯 ДИАГНОСТИКА: handle_return_shift_selection с {query.data}")
    
    if query.data == "cancel_conversation":
        return await cancel_swap(update, context)
    
    if query.data.startswith("swap_force_"):
        new_iiko_id = query.data.split("_")[2]
        return await confirm_one_way_swap(update, context, new_iiko_id)
    
    if query.data.startswith("swap_return_shift_"):
        return_shift_id = int(query.data.split("_")[3])
        context.user_data['return_shift_id'] = return_shift_id
        
        # Получаем смены
        original_shift_id = context.user_data.get('swap_shift_id')
        original_shift = get_shift_by_id(original_shift_id)
        return_shift = get_shift_by_id(return_shift_id)
        
        if not original_shift or not return_shift:
            await query.edit_message_text("❌ Ошибка: одна из смен не найдена")
            return await cancel_swap(update, context)
        
        employee_name = context.user_data.get('swap_employee_name', 'Сотрудник')
        
        # 🎯 Определяем тип обмена
        exchange_type = "🔄 Прямой обмен в один день" if original_shift.shift_date == return_shift.shift_date else "🔄 Обмен разными днями"
        
        # Подтверждение двустороннего обмена
        keyboard = [
            [InlineKeyboardButton("✅ Подтвердить обмен", callback_data="swap_confirm_exchange")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel_conversation")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        current_user_name = get_current_user_name(update, context)
        mode_text = " (эмуляция)" if is_emulation_mode(context) else ""
        await query.edit_message_text(
            f"🔄 Подтверждение обмена сменами\n\n"
            f"• {current_user_name}{mode_text} отдаёт: {original_shift.shift_date.strftime('%d.%m.%Y')}\n"
            f"• {current_user_name}{mode_text} получает: {return_shift.shift_date.strftime('%d.%m.%Y')}\n"
            f"• С сотрудником: {employee_name}\n"
            f"• Тип: {exchange_type}\n\n"
            f"Подтвердите обмен:",
            reply_markup=reply_markup
        )
        return CONFIRMING_SWAP

async def handle_swap_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора смены для замены с проверкой возможности обмена"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel_conversation":
        return await cancel_swap(update, context)  
    
    if query.data == "end_emulation":
        from bot.utils.emulation import stop_emulation
        stop_emulation(context)
        await query.edit_message_text("🔚 Эмуляция завершена")
        return await cancel_swap(update, context)
    
    if query.data.startswith("swap_shift_"):
        shift_id = int(query.data.split("_")[2])
        context.user_data['swap_shift_id'] = shift_id
        
        # Получаем текущего пользователя для исключения из списка
        current_iiko_id = get_current_iiko_id(update, context)
        
        # Получаем информацию о выбранной смене
        shift = get_shift_by_id(shift_id)
        if not shift:
            await query.edit_message_text("❌ Ошибка: смена не найдена")
            return ConversationHandler.END
        
        # Получаем список всех активных пользователей (исключая текущего)     
        users = get_all_users(active_only=True)
        users_with_iiko = [u for u in users if u.iiko_id and str(u.iiko_id) != current_iiko_id]
        
        if not users_with_iiko:
            await query.edit_message_text("❌ Нет доступных сотрудников для замены")
            return ConversationHandler.END
        
        # Формируем список сотрудников
        keyboard = []
        text = f"👥 Выберите сотрудника для замены на {shift.shift_date.strftime('%d.%m.%Y')}:\n\n"
        
        for user in users_with_iiko:
            # Проверяем, есть ли у сотрудника смена в этот день
            user_shifts = get_shifts_by_iiko_id(str(user.iiko_id), 
                                              start_date=shift.shift_date, 
                                              end_date=shift.shift_date)
            has_shift = len(user_shifts) > 0
            
            status = "🟢" if not has_shift else "🟡"
            text += f"{status} {user.name} - {'есть смена' if has_shift else 'вых'}\n"
            
            keyboard.append([InlineKeyboardButton(
                f"{user.name} {'🟡' if has_shift else '🟢'}",
                callback_data=f"swap_employee_{user.iiko_id}"
            )])
        
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_conversation")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)
        return SELECTING_EMPLOYEE

async def handle_swap_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка подтверждения всех типов замен"""
    query = update.callback_query
    await query.answer()
    
    logger.info(f"🎯 ДИАГНОСТИКА: handle_swap_confirmation вызван с {query.data}")
    logger.info(f"🎯 ДИАГНОСТИКА: Текущее состояние: {context.user_data}")
    
    if query.data == "cancel_conversation":
        logger.info("🎯 ДИАГНОСТИКА: Отмена в подтверждении")
        return await cancel_swap(update, context)
    
    if query.data == "swap_confirm_exchange":
        logger.info("🎯 ДИАГНОСТИКА: Начинаем двусторонний обмен")
        # Убедитесь, что возвращается следующее состояние или END
        result = await execute_two_way_swap(update, context)
        logger.info(f"🎯 ДИАГНОСТИКА: execute_two_way_swap вернул: {result}")
        return result
    
    if query.data.startswith("swap_confirm_one_way_"):
        logger.info("🎯 ДИАГНОСТИКА: Начинаем одностороннюю замену")
        new_iiko_id = query.data.split("_")[4]  # Проверьте индекс
        result = await execute_one_way_swap(update, context, new_iiko_id)
        logger.info(f"🎯 ДИАГНОСТИКА: execute_one_way_swap вернул: {result}")
        return result
    
    logger.error(f"🎯 ДИАГНОСТИКА: Неизвестный callback_data: {query.data}")
    return await cancel_swap(update, context)

async def handle_employee_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора сотрудника - ВСЕГДА предлагаем двусторонний обмен"""
    query = update.callback_query
    await query.answer()
    
    logger.info(f"🎯 ДИАГНОСТИКА: handle_employee_selection ВЫЗВАН с {query.data}")
    
    if query.data == "cancel_conversation":
        return await cancel_swap(update, context)
    
    if query.data.startswith("swap_employee_"):
        new_iiko_id = query.data.split("_")[2]
        shift_id = context.user_data.get('swap_shift_id')
        
        logger.info(f"🎯 ДИАГНОСТИКА: Обрабатываем сотрудника {new_iiko_id} для смены {shift_id}")
        
        if not shift_id:
            await query.edit_message_text("❌ Ошибка: смена не выбрана")
            return await cancel_swap(update, context)
        
        # Получаем исходную смену
        original_shift = get_shift_by_id(shift_id)
        if not original_shift:
            await query.edit_message_text("❌ Ошибка: смена не найдена")
            return await cancel_swap(update, context)
        
        from bot.database.user_operations import get_user_by_iiko_id
        new_employee = get_user_by_iiko_id(int(new_iiko_id))
        employee_name = new_employee.name if new_employee else new_iiko_id
        
        # Сохраняем данные
        context.user_data['swap_new_iiko_id'] = new_iiko_id
        context.user_data['swap_employee_name'] = employee_name
        
        # 🎯 КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: ВКЛЮЧАЕМ смены в тот же день для прямых замен
        all_shifts = get_upcoming_shifts_by_iiko_id(str(new_iiko_id), days=60)
        
        # НЕ исключаем смены в тот же день - они нужны для прямых замен!
        shifts_for_swap = all_shifts
        
        logger.info(f"🎯 ДИАГНОСТИКА: Всего смен у сотрудника: {len(all_shifts)}")
        logger.info(f"🎯 ДИАГНОСТИКА: Доступно смен для обмена (ВКЛЮЧАЯ день замены): {len(shifts_for_swap)}")
        
        # Логируем все смены сотрудника для диагностики
        for shift in all_shifts:
            logger.info(f"🎯 Смена сотрудника: {shift.shift_date} - {shift.shift_type_obj.start_time if shift.shift_type_obj else 'NO_TYPE'}")
        
        if not shifts_for_swap:
            # Если нет других смен - предлагаем только одностороннюю замену
            logger.info(f"🎯 ДИАГНОСТИКА: Нет смен для обмена, предлагаем одностороннюю замену")
            
            keyboard = [
                [InlineKeyboardButton("✅ Односторонняя замена", 
                                    callback_data=f"swap_force_{new_iiko_id}")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_conversation")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            current_user_name = get_current_user_name(update, context)
            mode_text = " (эмуляция)" if is_emulation_mode(context) else ""
            
            await query.edit_message_text(
                f"🔄 Обмен сменами между {current_user_name}{mode_text} и {employee_name}\n\n"
                f"У сотрудника нет других смен для обмена.\n"
                f"Вы можете сделать одностороннюю замену:",
                reply_markup=reply_markup
            )
            return CONFIRMING_SWAP
        else:
            # 🎯 ВСЕГДА предлагаем выбор смены для двустороннего обмена + опцию односторонней
            logger.info(f"🎯 ДИАГНОСТИКА: Показываем выбор смен для обмена (включая смены в тот же день)")
            
            keyboard = []
            current_user_name = get_current_user_name(update, context)
            mode_text = " (эмуляция)" if is_emulation_mode(context) else ""
            
            text = (
                f"🔄 Обмен сменами между {current_user_name}{mode_text} и {employee_name}\n\n"
                f"📅 Выберите смену для обмена (можно выбрать смену в тот же день для прямого обмена):\n\n"
            )
            
            # Добавляем пометку для смены в тот же день
            for i, shift in enumerate(shifts_for_swap[:10]):  # Ограничиваем 10 сменами
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
                
                # 🎯 Помечаем смену в тот же день
                same_day_marker = " 🔄" if shift.shift_date == original_shift.shift_date else ""
                
                text += f"{i+1}. {date_str} ({shift_type_text}) {shift.shift_type_obj.point}: {start_str} - {end_str}{same_day_marker}\n"
                keyboard.append([InlineKeyboardButton(
                    f"{date_str} {shift.shift_type_obj.point} {start_str}{same_day_marker}",
                    callback_data=f"swap_return_shift_{shift.shift_id}"
                )])
            
            # Добавляем опцию односторонней замены
            keyboard.append([InlineKeyboardButton(
                "✅ Односторонняя замена (без получения смены)", 
                callback_data=f"swap_force_{new_iiko_id}"
            )])
            keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_conversation")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Обновляем сообщение с новой клавиатурой
            try:
                await query.edit_message_text(
                    text, 
                    reply_markup=reply_markup
                )
                logger.info("🎯 ДИАГНОСТИКА: Сообщение успешно обновлено с клавиатурой выбора смен (включая смены в тот же день)")
            except Exception as e:
                logger.error(f"🎯 ОШИБКА при обновлении сообщения: {e}")
                # Если не удалось обновить, отправляем новое сообщение
                await query.message.reply_text(
                    text,
                    reply_markup=reply_markup
                )
            
            return SELECTING_RETURN_SHIFT

async def cancel_swap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена замены и возврат в главное меню"""
    from bot.keyboards.menus import get_main_menu
    
    if update.callback_query:
        await update.callback_query.edit_message_text("❌ Замена отменена")
        await update.callback_query.message.reply_text("Выберите действие:", reply_markup=get_main_menu())
    else:
        await update.message.reply_text("❌ Замена отменена", reply_markup=get_main_menu())
    
    context.user_data.clear()
    return ConversationHandler.END

async def complete_swap_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение замены и возврат в главное меню"""
    from bot.keyboards.menus import get_main_menu
    
    # Очищаем данные пользователя
    context.user_data.clear()
    
    if update.callback_query:
        await update.callback_query.message.reply_text(
            "Выберите действие:", 
            reply_markup=get_main_menu()
        )
    else:
        await update.message.reply_text(
            "Выберите действие:", 
            reply_markup=get_main_menu()
        )
    
    return ConversationHandler.END

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

async def confirm_one_way_swap(update: Update, context: ContextTypes.DEFAULT_TYPE, new_iiko_id: str):
    """Подтверждение односторонней замены"""
    query = update.callback_query
    shift_id = context.user_data.get('swap_shift_id')
    
    original_shift = get_shift_by_id(shift_id)
    from bot.database.user_operations import get_user_by_iiko_id
    new_employee = get_user_by_iiko_id(int(new_iiko_id))
    employee_name = new_employee.name if new_employee else new_iiko_id
    
    keyboard = [
        [InlineKeyboardButton("✅ Подтвердить замену", callback_data=f"swap_confirm_one_way_{new_iiko_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_conversation")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    current_user_name = get_current_user_name(update, context)
    mode_text = " (эмуляция)" if is_emulation_mode(context) else ""
    await query.edit_message_text(
        f"🔄 Подтверждение односторонней замены\n\n"
        f"• Смена {current_user_name}{mode_text}: {original_shift.shift_date.strftime('%d.%m.%Y')}\n"
        f"• Передаётся: {employee_name}\n"
        f"• Тип: Односторонняя (вы отдаёте смену без получения взамен)\n\n"
        f"Подтвердите замену:",
        reply_markup=reply_markup
    )
    return CONFIRMING_SWAP

def get_swap_conversation_handler():
    """Упрощенный ConversationHandler для замен"""
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🔄 Замены$"), swap_menu)
        ],
        states={
            SWAP_MENU: [
                CallbackQueryHandler(handle_swap_callback, pattern="^swap_shift_"),
            ],
            SELECTING_EMPLOYEE: [
                CallbackQueryHandler(handle_employee_selection, pattern="^swap_employee_"),
            ],
            SELECTING_RETURN_SHIFT: [
                CallbackQueryHandler(handle_return_shift_selection, pattern="^swap_return_shift_"),
                CallbackQueryHandler(handle_return_shift_selection, pattern="^swap_force_"),
            ],
            CONFIRMING_SWAP: [
                CallbackQueryHandler(handle_swap_confirmation, pattern="^swap_confirm_"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_swap),
            CommandHandler("start", cancel_swap),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_swap),
            CallbackQueryHandler(cancel_swap, pattern="^cancel_conversation$"),
        ],
        allow_reentry=True,
        name="swap_conversation",
        per_user=True,
        per_chat=True
    )

async def complete_swap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение замены с синхронизацией в Google Sheets"""
    query = update.callback_query
    await query.answer()
    
    swap_data = context.user_data.get('swap_data')
    if not swap_data:
        await query.edit_message_text("❌ Данные замены не найдены")
        return ConversationHandler.END
    
    logger.info(f"Начинаем завершение замены: {swap_data}")
    
    try:
        # Сохраняем замену в БД
        swap_success = save_swap_to_db(swap_data)
        logger.info(f"Результат сохранения в БД: {swap_success}")
        
        if swap_success:
            # Синхронизируем с Google Sheets
            from bot.utils.google_sheets import sync_swap_to_sheets
            logger.info("Начинаем синхронизацию с Google Sheets...")
            
            
            if sheets_success:
                message = "✅ Замена успешно выполнена и синхронизирована с расписанием!"
            else:
                message = "✅ Замена выполнена, но не удалось обновить Google Sheets. Сообщите администратору."
            
            # Отправляем уведомления участникам
            await notify_swap_participants(swap_data, context)
        else:
            message = "❌ Ошибка при сохранении замены"
        
        await query.edit_message_text(message)
        
    except Exception as e:
        logger.error(f"Ошибка при завершении замены: {e}")
        await query.edit_message_text("❌ Произошла ошибка при выполнении замены")
    
    context.user_data.clear()
    return ConversationHandler.END
        
async def show_processing_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "🔄 Замена в процессе, ожидайте..."):
    """Показывает сообщение о процессе и убирает клавиатуру"""
    try:
        query = update.callback_query
        await query.edit_message_text(
            text,
            reply_markup=None  # Убираем клавиатуру
        )
        return True
    except Exception as e:
        logger.error(f"Ошибка при обновлении сообщения: {e}")
        # Если не удалось обновить, отправляем новое сообщение
        try:
            await query.message.reply_text(text)
            return True
        except Exception as e2:
            logger.error(f"Ошибка при отправке нового сообщения: {e2}")
            return False

async def execute_one_way_swap(update: Update, context: ContextTypes.DEFAULT_TYPE, new_iiko_id: str):
    """Выполнить одностороннюю замену"""
    query = update.callback_query
    
    # Показываем сообщение о процессе и убираем клавиатуру
    await show_processing_message(update, context, "🔄 Замена в процессе, ожидайте...")
    
    shift_id = context.user_data.get('swap_shift_id')
    
    if not shift_id:
        await query.edit_message_text("❌ Ошибка: смена не выбрана")
        return await cancel_swap(update, context)
    
    # Получаем исходную смену
    original_shift = get_shift_by_id(shift_id)
    if not original_shift:
        await query.edit_message_text("❌ Ошибка: смена не найдена")
        return await cancel_swap(update, context)
    
    # Меняем iiko_id в смене
    updated_shift = update_shift_iiko_id(shift_id, new_iiko_id)
    
    if not updated_shift:
        await query.edit_message_text("❌ Ошибка при замене смены")
        return await cancel_swap(update, context)
    
    # СИНХРОНИЗАЦИЯ С GOOGLE SHEETS
    sync_success = False
    try:
        from bot.utils.google_sheets import sync_swap_to_sheets
        
        swap_data = {
            'from_employee': {
                'iiko_id': original_shift.iiko_id,
                'old_shift': original_shift,
                'new_shift': None
            },
            'to_employee': {
                'iiko_id': new_iiko_id,
                'old_shift': None,
                'new_shift': updated_shift
            }
        }
        
        sync_success = sync_swap_to_sheets(swap_data)
        
        if sync_success:
            logger.info("✅ Успешная синхронизация замены в Google Sheets")
        else:
            logger.warning("⚠️ Не удалось синхронизировать замену в Google Sheets")
            
    except Exception as e:
        logger.error(f"❌ Ошибка синхронизации с Google Sheets: {e}")
    
    # Получаем имя нового сотрудника
    from bot.database.user_operations import get_user_by_iiko_id
    new_employee = get_user_by_iiko_id(int(new_iiko_id))
    employee_name = new_employee.name if new_employee else new_iiko_id
    
    # Сообщаем о результате
    if sync_success:
        current_user_name = get_current_user_name(update, context)
        mode_text = " (эмуляция)" if is_emulation_mode(context) else ""
        success_text = (
            f"✅ Замена успешно завершена!\n\n"
            f"• Cмена {current_user_name}{mode_text} на {original_shift.shift_date.strftime('%d.%m.%Y')}\n"
            f"• Передана: {employee_name}\n"
            f"• Тип: Односторонняя замена"
        )
    else:
        success_text = (
            f"⚠️ Замена выполнена с ограничениями\n\n"
            f"• Смена передана: {employee_name}\n"
            f"• Но не удалось обновить Google Sheets\n"
            f"• Сообщите администратору"
        )
    
    await query.edit_message_text(success_text)
    return await complete_swap_conversation(update, context)

async def execute_two_way_swap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнить двусторонний обмен сменами - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
    query = update.callback_query
    
    # Показываем сообщение о процессе
    await show_processing_message(update, context, "🔄 Обмен в процессе, ожидайте...")
    
    original_shift_id = context.user_data.get('swap_shift_id')
    return_shift_id = context.user_data.get('return_shift_id')
    
    if not original_shift_id or not return_shift_id:
        await query.edit_message_text("❌ Ошибка: не выбраны смены для обмена")
        return await cancel_swap(update, context)
    
    # Получаем смены ДО изменений
    original_shift = get_shift_by_id(original_shift_id)
    return_shift = get_shift_by_id(return_shift_id)
    
    if not original_shift or not return_shift:
        await query.edit_message_text("❌ Ошибка: одна из смен не найдена")
        return await cancel_swap(update, context)
    
    # Сохраняем ВСЕ исходные данные
    original_data = {
        'iiko_id': original_shift.iiko_id,
        'date': original_shift.shift_date,
        'start_time': original_shift.shift_type_obj.start_time.strftime("%H:%M"),
        'end_time': original_shift.shift_type_obj.end_time.strftime("%H:%M"),
        'point': original_shift.shift_type_obj.point
    }
    
    return_data = {
        'iiko_id': return_shift.iiko_id,
        'date': return_shift.shift_date,
        'start_time': return_shift.shift_type_obj.start_time.strftime("%H:%M"),
        'end_time': return_shift.shift_type_obj.end_time.strftime("%H:%M"),
        'point': return_shift.shift_type_obj.point
    }
    
    # Правильный порядок операций
    
    # Сначала синхронизируем Google Sheets - устанавливаем новые смены
    sync_success = False
    try:
        from bot.utils.google_sheets import update_shift_in_sheets
        
        # 1. Устанавливаем второму сотруднику смену первого (на дату первого)
        success1 = update_shift_in_sheets(
            iiko_id=return_data['iiko_id'],
            shift_date=original_data['date'],
            start_time=original_data['start_time'],
            end_time=original_data['end_time'],
            point=original_data['point']
        )
        
        # 2. Устанавливаем первому сотруднику смену второго (на дату второго)  
        success2 = update_shift_in_sheets(
            iiko_id=original_data['iiko_id'],
            shift_date=return_data['date'],
            start_time=return_data['start_time'],
            end_time=return_data['end_time'],
            point=return_data['point']
        )
        
        sync_success = success1 and success2
        
        if sync_success:
            logger.info("✅ Успешная синхронизация двусторонней замены в Google Sheets")
        else:
            logger.warning("⚠️ Не удалось синхронизировать двустороннюю замену в Google Sheets")
            
    except Exception as e:
        logger.error(f"❌ Ошибка синхронизации с Google Sheets: {e}")
        sync_success = False
    
    # 2. Только после успешной синхронизации обновляем базу данных
    if sync_success:
        # Меняем смены местами в БД
        updated_shift1 = update_shift_iiko_id(original_shift_id, return_data['iiko_id'])
        updated_shift2 = update_shift_iiko_id(return_shift_id, original_data['iiko_id'])
        
        if not updated_shift1 or not updated_shift2:
            await query.edit_message_text("❌ Ошибка при обмене сменами в базе данных")
            # 🎯 В случае ошибки нужно откатить изменения в Google Sheets
            try:
                # Откатываем изменения в Google Sheets
                update_shift_in_sheets(
                    iiko_id=return_data['iiko_id'],
                    shift_date=original_data['date'],
                    start_time=None,
                    end_time=None,
                    point=None
                )
                update_shift_in_sheets(
                    iiko_id=original_data['iiko_id'],
                    shift_date=return_data['date'], 
                    start_time=None,
                    end_time=None,
                    point=None
                )
            except Exception as rollback_error:
                logger.error(f"❌ Ошибка при откате Google Sheets: {rollback_error}")
            
            return await cancel_swap(update, context)
    else:
        await query.edit_message_text("❌ Ошибка при синхронизации с Google Sheets")
        return await cancel_swap(update, context)
    
    # Получаем имена сотрудников
    from bot.database.user_operations import get_user_by_iiko_id
    original_employee = get_user_by_iiko_id(int(original_data['iiko_id']))
    return_employee = get_user_by_iiko_id(int(return_data['iiko_id']))
    original_name = original_employee.name if original_employee else original_data['iiko_id']
    return_name = return_employee.name if return_employee else return_data['iiko_id']
    
    # Определяем тип обмена
    exchange_type = "прямой обмен в один день" if original_data['date'] == return_data['date'] else "обмен разными днями"
    
    # Сообщаем о результате
    current_user_name = get_current_user_name(update, context)
    mode_text = " (эмуляция)" if is_emulation_mode(context) else ""
    success_text = (
        f"✅ Двусторонний обмен успешно завершен!\n\n"
        f"• {current_user_name}{mode_text} отдал: {original_data['date'].strftime('%d.%m.%Y')}\n"  
        f"• {current_user_name}{mode_text} получил: {return_data['date'].strftime('%d.%m.%Y')}\n"
        f"• С сотрудником: {return_name}\n"
        f"• Тип: {exchange_type}"
    )
    
    await query.edit_message_text(success_text)
    return await complete_swap_conversation(update, context)
