"""Обработчики для меню настроек"""
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters, CommandHandler, CallbackQueryHandler
from bot.utils.auth import require_roles, ROLE_MENTOR, ROLE_SENIOR
from bot.database.user_operations import (
    get_all_users, create_user, update_user, delete_user, get_user_by_id, get_user_by_iiko_id
)
from bot.database.schedule_operations import (
    get_upcoming_shifts_by_iiko_id, get_shifts_by_iiko_id,
    create_shift, update_shift, get_shift_by_id, bulk_create_shifts, delete_shifts_by_date_range,
    create_shift_type, get_shift_types, update_shift_type, delete_shift_type, get_shift_type_by_id
)
from bot.database.checklist_operations import get_hybrid_assignment_tasks
from bot.utils.google_sheets import get_current_month_name, get_next_month_name, parse_schedule_from_sheet
from bot.utils.common_handlers import cancel_conversation, start_cancel_conversation
from bot.utils.emulation import is_emulation_mode, stop_emulation, start_emulation, get_emulated_user
from bot.keyboards.menus import get_main_menu
import sqlite3
import os
from datetime import datetime, date, timedelta
import logging

logger = logging.getLogger(__name__)

# Состояния для настроек
(SETTINGS_MENU, ADDING_USER_NAME, ADDING_USER_IIKO_ID, ADDING_USER_USERNAME, ADDING_USER_ROLE,
 EDITING_USER_NAME, EDITING_USER_ROLE, EDITING_USER_IIKO_ID, EDITING_USER_USERNAME,
 DELETING_USER_CONFIRM, CLEARING_REVIEWS,
 # Состояния для расписания
 SCHEDULE_MENU, PARSING_MONTH, SELECTING_EMPLOYEE_FOR_SHIFTS, VIEWING_SHIFTS,
 ADDING_SHIFT_DATE, ADDING_SHIFT_IIKO_ID, ADDING_SHIFT_POINT, ADDING_SHIFT_TYPE,
 ADDING_SHIFT_START, ADDING_SHIFT_END, EDITING_SHIFT_ID, EDITING_SHIFT_FIELD,
 EDITING_SHIFT_MENU, EDITING_SHIFT_DATE, EDITING_SHIFT_IIKO_ID, 
 EDITING_SHIFT_POINT, EDITING_SHIFT_TYPE, EDITING_SHIFT_TIME,
 EMULATION_MANAGEMENT, EMULATING_USER, EMULATION_MENU, DELETING_SHIFT_CONFIRM,
 # Состояния для управления типами смен
 SHIFT_TYPES_MENU, ADDING_SHIFT_TYPE_DATA, EDITING_SHIFT_TYPE_ID, EDITING_SHIFT_TYPE_FIELD,
 DELETING_SHIFT_TYPE_CONFIRM,
 # Состояния для управления чеклистами
 CHECKLIST_MANAGEMENT_MENU, CHECKLIST_SELECT_POINT, CHECKLIST_SELECT_DAY, 
 CHECKLIST_SELECT_SHIFT, CHECKLIST_ADD_TASK_DESCRIPTION, CHECKLIST_VIEW_TEMPLATES,
 CHECKLIST_VIEW_SELECT_POINT, CHECKLIST_VIEW_SELECT_DAY, CHECKLIST_VIEW_TASKS_LIST,
 CHECKLIST_EDIT_TASK_SELECT, CHECKLIST_EDIT_TASK_DESCRIPTION, CHECKLIST_DELETE_TASK_SELECT,
 CHECKLIST_DELETE_TASK_CONFIRM, HYBRID_SELECT_POINT, HYBRID_SELECT_DAY, HYBRID_VIEW_CURRENT,
 HYBRID_SELECT_MORNING_TASK, HYBRID_SELECT_EVENING_TASK, HYBRID_SAVE_ASSIGNMENT,
 HYBRID_VIEW_EXISTING, HYBRID_EDIT_EXISTING, HYBRID_DELETE_EXISTING, HYBRID_DELETE_CONFIRM) = range(61)

@require_roles([ROLE_MENTOR, ROLE_SENIOR])
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню настроек"""
    
    keyboard = [
        [KeyboardButton("👥 Управление пользователями")],
        [KeyboardButton("📅 Управление расписанием")],
        [KeyboardButton("🕒 Управление типами смен")],
        [KeyboardButton("📝 Управление чек-листами")],
        [KeyboardButton("🗑️ Очистить таблицу оценок")],
    ]
    
    # Rнопка эмуляции с индикатором статуса
    if is_emulation_mode(context):
        emulated = get_emulated_user(context)
        emulated_name = str(emulated['name']) if emulated['name'] else "Неизвестный"
        emulation_button = KeyboardButton(f"🔁 Эмуляция: {emulated_name}")
    else:
        emulation_button = KeyboardButton("🔁 Управление эмуляцией")   
        
    keyboard.append([emulation_button])
    keyboard.append([KeyboardButton("⬅️ Назад")])
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    # Текст с информацией о статусе эмуляции
    status_text = ""
    if is_emulation_mode(context):
        emulated = get_emulated_user(context)
        emulated_name = emulated.get('name', 'Неизвестный')
        emulated_iiko_id = emulated.get('iiko_id', 'Неизвестный')
        if not isinstance(emulated_name, str) or not emulated_name:
            emulated_name = "Неизвестный"
        if not isinstance(emulated_iiko_id, str) or not emulated_iiko_id:
            emulated_iiko_id = "Неизвестный"
        status_text = f"\n\n🔁 Режим эмуляции: {emulated_name} (ID: {emulated_iiko_id})"
    
    await update.message.reply_text(
        "⚙️ Настройки\n\n"
        "Выберите раздел:",
        reply_markup=reply_markup
    )
    return SETTINGS_MENU

async def cancel_editing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена редактирования смены"""
    await update.message.reply_text(
        "❌ Редактирование смены отменено",
        reply_markup=get_main_menu()
    )
    context.user_data.clear()
    return ConversationHandler.END

async def users_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления пользователями"""
    keyboard = [
        [KeyboardButton("➕ Добавить пользователя")],
        [KeyboardButton("📋 Список пользователей")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "👥 Управление пользователями\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )
    return SETTINGS_MENU

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список всех пользователей с кнопками редактирования/удаления"""
    users = get_all_users(active_only=True)
    
    if not users:
        await update.message.reply_text("📭 Пользователи не найдены")
        return SETTINGS_MENU
    
    response = "📋 Список пользователей:\n\n"
    keyboard = []
    
    for user in users:
        role_emoji = {
            'barista': '☕',
            'senior': '⭐',
            'mentor': '👨‍🏫'
        }
        emoji = role_emoji.get(user.role, '👤')
        response += f"{emoji} {user.name} ({user.role})\n"
        if user.iiko_id:
            response += f"   Iiko ID: {user.iiko_id}\n"
        if user.telegram_username:
            response += f"   @{user.telegram_username}\n"
        response += "\n"
        
        # Добавляем кнопки для каждого пользователя (сокращаем имя если длинное)
        user_name_short = user.name[:15] if len(user.name) > 15 else user.name
        keyboard.append([
            InlineKeyboardButton(f"✏️ {user_name_short}", callback_data=f"edit_user_{user.id}"),
            InlineKeyboardButton(f"🗑️ {user_name_short}", callback_data=f"delete_user_{user.id}")
        ])
    
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_users_management")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(response, reply_markup=reply_markup)
    return SETTINGS_MENU

async def handle_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка callback для редактирования/удаления пользователей"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith("edit_user_"):
        user_id = int(data.split("_")[2])
        context.user_data['editing_user_id'] = user_id
        user = get_user_by_id(user_id)
        if user:
            # Отправляем новое сообщение вместо редактирования
            await query.message.reply_text(
                f"✏️ Редактирование пользователя: {user.name}\n\n"
                "Введите новое имя (или отправьте '-' чтобы оставить текущее):"
            )
            return EDITING_USER_NAME
        else:
            await query.message.reply_text("❌ Пользователь не найден")
            return SETTINGS_MENU
    
    elif data.startswith("delete_user_"):
        user_id = int(data.split("_")[2])
        context.user_data['deleting_user_id'] = user_id
        user = get_user_by_id(user_id)
        if user:
            await query.message.reply_text(
                f"🗑️ Удаление пользователя: {user.name}\n\n"
                "⚠️ ВНИМАНИЕ! Это действие нельзя отменить.\n\n"
                f"Для подтверждения введите имя пользователя: {user.name}"
            )
            return DELETING_USER_CONFIRM
        else:
            await query.message.reply_text("❌ Пользователь не найден")
            return SETTINGS_MENU
    
    elif data == "back_to_users_management":
        # Используем edit_message_text для обновления сообщения
        keyboard = [
            [KeyboardButton("➕ Добавить пользователя")],
            [KeyboardButton("📋 Список пользователей")],
            [KeyboardButton("⬅️ Назад")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await query.edit_message_text(
            "👥 Управление пользователями\n\n"
            "Выберите действие:",
            reply_markup=None
        )
        await query.message.reply_text(
            "👥 Управление пользователями\n\n"
            "Выберите действие:",
            reply_markup=reply_markup
        )
        return SETTINGS_MENU
    
    return SETTINGS_MENU

async def start_adding_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления пользователя"""
    await update.message.reply_text(
        "➕ Добавление пользователя\n\n"
        "Введите имя пользователя:"
    )
    return ADDING_USER_NAME

async def add_user_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение имени пользователя"""
    context.user_data['new_user_name'] = update.message.text
    
    await update.message.reply_text(
        f"Имя: {update.message.text}\n\n"
        "Введите Iiko ID (или отправьте '-' чтобы пропустить):"
    )
    return ADDING_USER_IIKO_ID

async def add_user_iiko_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение Iiko ID"""
    text = update.message.text
    if text == "-":
        context.user_data['new_user_iiko_id'] = None
    else:
        try:
            context.user_data['new_user_iiko_id'] = int(text)
        except ValueError:
            await update.message.reply_text("❌ Неверный формат ID. Введите число или '-'")
            return ADDING_USER_IIKO_ID
    
    await update.message.reply_text(
        "Введите Telegram username (без @, или отправьте '-' чтобы пропустить):"
    )
    return ADDING_USER_USERNAME

async def add_user_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение username"""
    text = update.message.text
    if text == "-":
        context.user_data['new_user_username'] = None
    else:
        context.user_data['new_user_username'] = text.replace('@', '')
    
    keyboard = [
        [KeyboardButton("☕ Бариста"), KeyboardButton("⭐ Старший")],
        [KeyboardButton("👨‍🏫 Наставник")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "Выберите роль:",
        reply_markup=reply_markup
    )
    return ADDING_USER_ROLE

async def add_user_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение роли и создание пользователя"""
    role_map = {
        "☕ Бариста": "barista",
        "⭐ Старший": "senior",
        "👨‍🏫 Наставник": "mentor"
    }
    
    role_text = update.message.text
    if role_text not in role_map:
        await update.message.reply_text("❌ Пожалуйста, выберите роль из списка")
        return ADDING_USER_ROLE
    
    role = role_map[role_text]
    
    try:
        user = create_user(
            name=context.user_data['new_user_name'],
            iiko_id=context.user_data.get('new_user_iiko_id'),
            telegram_username=context.user_data.get('new_user_username'),
            role=role
        )
        
        await update.message.reply_text(
            f"✅ Пользователь {user.name} успешно добавлен!\n"
            f"Роль: {role}"
        )
        
        context.user_data.clear()
        return await users_management(update, context)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при добавлении пользователя: {str(e)}")
        return await users_management(update, context)

# Редактирование пользователя
async def editing_user_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование имени пользователя"""
    user_id = context.user_data.get('editing_user_id')
    if not user_id:
        return await users_management(update, context)
    
    new_name = update.message.text
    keyboard = [
        [KeyboardButton("☕ Бариста"), KeyboardButton("⭐ Старший")],
        [KeyboardButton("👨‍🏫 Наставник"), KeyboardButton("-")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    if new_name == "-":
        # Пропускаем имя, переходим к роли
        await update.message.reply_text(
            "Имя не изменено.\n\n"
            "Выберите новую роль (или отправьте '-' чтобы оставить текущую):",
            reply_markup=reply_markup
        )
        return EDITING_USER_ROLE
    
    user = update_user(user_id, name=new_name)
    if user:
        await update.message.reply_text(
            f"✅ Имя изменено на: {new_name}\n\n"
            "Выберите новую роль (или отправьте '-' чтобы оставить текущую):",
            reply_markup=reply_markup
        )
        return EDITING_USER_ROLE
    else:
        await update.message.reply_text("❌ Ошибка при обновлении имени")
        return await users_management(update, context)

async def editing_user_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование роли пользователя"""
    user_id = context.user_data.get('editing_user_id')
    if not user_id:
        return await users_management(update, context)
    
    role_text = update.message.text
    if role_text == "-":
        # Пропускаем роль, переходим к Iiko ID
        await update.message.reply_text(
            "Роль не изменена.\n\n"
            "Введите новый Iiko ID (или отправьте '-' чтобы оставить текущий):"
        )
        return EDITING_USER_IIKO_ID
    
    role_map = {
        "☕ Бариста": "barista",
        "⭐ Старший": "senior",
        "👨‍🏫 Наставник": "mentor",
        "barista": "barista",
        "senior": "senior",
        "mentor": "mentor"
    }
    
    if role_text not in role_map:
        keyboard = [
            [KeyboardButton("☕ Бариста"), KeyboardButton("⭐ Старший")],
            [KeyboardButton("👨‍🏫 Наставник"), KeyboardButton("-")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            "❌ Пожалуйста, выберите роль из списка:",
            reply_markup=reply_markup
        )
        return EDITING_USER_ROLE
    
    role = role_map[role_text]
    user = update_user(user_id, role=role)
    if user:
        # Убираем клавиатуру после выбора роли
        await update.message.reply_text(
            f"✅ Роль изменена на: {role}\n\n"
            "Введите новый Iiko ID (или отправьте '-' чтобы оставить текущий):",
            reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)  # Пустая клавиатура
        )
        return EDITING_USER_IIKO_ID
    else:
        await update.message.reply_text("❌ Ошибка при обновлении роли")
        return await users_management(update, context)

async def editing_user_iiko_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование Iiko ID"""
    user_id = context.user_data.get('editing_user_id')
    if not user_id:
        return await users_management(update, context)
    
    text = update.message.text
    if text == "-":
        # Пропускаем Iiko ID, не обновляем поле
        await update.message.reply_text(
            "Iiko ID не изменен.\n\n"
            "Введите новый Telegram username (без @, или отправьте '-' чтобы оставить текущий):",
            reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)  # Пустая клавиатура
        )
        return EDITING_USER_USERNAME
    else:
        try:
            iiko_id = int(text)
            user = update_user(user_id, iiko_id=iiko_id)
            if user:
                await update.message.reply_text(
                    f"✅ Iiko ID изменен на: {iiko_id}\n\n"
                    "Введите новый Telegram username (без @, или отправьте '-' чтобы оставить текущий):",
                    reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)  # Пустая клавиатура
                )
                return EDITING_USER_USERNAME
            else:
                await update.message.reply_text("❌ Ошибка при обновлении Iiko ID")
                return await users_management(update, context)
        except ValueError:
            await update.message.reply_text("❌ Неверный формат ID. Введите число или '-'")
            return EDITING_USER_IIKO_ID

async def editing_user_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование Telegram username"""
    user_id = context.user_data.get('editing_user_id')
    if not user_id:
        return await users_management(update, context)
    
    text = update.message.text
    if text == "-":
        # Пропускаем username, не обновляем поле
        user = get_user_by_id(user_id)
        if user:
            await update.message.reply_text(
                f"✅ Пользователь {user.name} успешно обновлен!"
            )
            context.user_data.clear()
            return await users_management(update, context)
        else:
            await update.message.reply_text("❌ Пользователь не найден")
            return await users_management(update, context)
    else:
        telegram_username = text.replace('@', '')
        user = update_user(user_id, telegram_username=telegram_username)
        if user:
            await update.message.reply_text(
                f"✅ Пользователь {user.name} успешно обновлен!"
            )
            context.user_data.clear()
            return await users_management(update, context)
        else:
            await update.message.reply_text("❌ Ошибка при обновлении username")
            return await users_management(update, context)

# Удаление пользователя
async def deleting_user_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления пользователя"""
    user_id = context.user_data.get('deleting_user_id')
    if not user_id:
        return await users_management(update, context)
    
    user = get_user_by_id(user_id)
    if not user:
        await update.message.reply_text("❌ Пользователь не найден")
        context.user_data.clear()
        return await users_management(update, context)
    
    entered_name = update.message.text.strip()
    
    if entered_name == user.name:
        # Подтверждение получено, удаляем
        success = delete_user(user_id)
        if success:
            await update.message.reply_text(
                f"✅ Пользователь {user.name} успешно удален (деактивирован)."
            )
        else:
            await update.message.reply_text("❌ Ошибка при удалении пользователя")
        context.user_data.clear()
        return await users_management(update, context)
    else:
        await update.message.reply_text(
            "❌ Имена не совпадают. Удаление отменено."
        )
        context.user_data.clear()
        return await users_management(update, context)

# Очистка таблицы drink_reviews
async def clear_reviews_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение очистки таблицы оценок"""
    await update.message.reply_text(
        "🗑️ Очистка таблицы оценок\n\n"
        "⚠️ ВНИМАНИЕ! Все оценки будут удалены!\n"
        "Будет создан бэкап перед очисткой.\n\n"
        "Для подтверждения введите 'Y' (да) или 'N' (нет):"
    )
    return CLEARING_REVIEWS

async def handle_clear_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка подтверждения очистки"""
    text = update.message.text.upper().strip()
    
    if text == 'Y' or text == 'ДА':
        try:
            # Создаем бэкап
            backup_filename = f"coffee_quality_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            
            if os.path.exists('coffee_quality.db'):
                import shutil
                shutil.copy2('coffee_quality.db', backup_filename)
            
            # Очищаем таблицу
            conn = sqlite3.connect('coffee_quality.db')
            cursor = conn.cursor()
            cursor.execute("DELETE FROM drink_reviews")
            conn.commit()
            conn.close()
            
            await update.message.reply_text(
                f"✅ Таблица оценок очищена!\n"
                f"📦 Бэкап сохранен: {backup_filename}"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка при очистке: {str(e)}")
    elif text == 'N' or text == 'НЕТ':
        await update.message.reply_text("❌ Очистка отменена.")
    else:
        await update.message.reply_text(
            "❌ Неверный ответ. Введите 'Y' (да) или 'N' (нет):"
        )
        return CLEARING_REVIEWS
    
    return await settings_menu(update, context)

# ========== ОБРАБОТЧИКИ РАСПИСАНИЯ ==========

async def shift_types_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления типами смен"""
    keyboard = [
        [KeyboardButton("➕ Добавить тип смены")],
        [KeyboardButton("📋 Список типов смен")],
        [KeyboardButton("✏️ Редактировать тип смены")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "🕒 Управление типами смен\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )
    return SHIFT_TYPES_MENU

async def start_adding_shift_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления типа смены"""
    await update.message.reply_text(
        "➕ Добавление типа смены\n\n"
        "Введите данные типа смены в формате:\n"
        "Название|Время начала|Время окончания|Точка|Тип смены\n\n"
        "Пример: Утро ДЕ|09:00|17:00|ДЕ|morning\n\n"
        "Типы смен: morning, hybrid, evening\n"
        "Точки: ДЕ, УЯ\n\n"
        "Для отмены введите /cancel"
    )
    return ADDING_SHIFT_TYPE_DATA

async def add_shift_type_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенных данных типа смены"""
    try:
        data = update.message.text.split('|')
        if len(data) != 5:
            await update.message.reply_text("❌ Неверный формат данных. Нужно 5 параметров через |. Попробуйте снова.")
            return ADDING_SHIFT_TYPE_DATA
        
        name, start_time, end_time, point, shift_type = [item.strip() for item in data]
        
        # Валидация точек
        if point not in ['ДЕ', 'УЯ']:
            await update.message.reply_text("❌ Неверная точка. Допустимые значения: ДЕ, УЯ")
            return ADDING_SHIFT_TYPE_DATA
        
        # Валидация типов смен
        if shift_type not in ['morning', 'hybrid', 'evening']:
            await update.message.reply_text("❌ Неверный тип смены. Допустимые значения: morning, hybrid, evening")
            return ADDING_SHIFT_TYPE_DATA
        
        # Создаем тип смены
        shift_type_id = create_shift_type({
            'name': name,
            'start_time': start_time,
            'end_time': end_time,
            'point': point,
            'shift_type': shift_type
        })
        
        await update.message.reply_text(
            f"✅ Тип смены '{name}' успешно добавлен!\n"
            f"ID: {shift_type_id}\n"
            f"Время: {start_time} - {end_time}\n"
            f"Точка: {point}\n"
            f"Тип: {shift_type}"
        )
        
        # Возвращаем в меню управления типами смен
        return await shift_types_management(update, context)
        
    except Exception as e:
        await update.message.reply_text(
            f"❌ Ошибка при добавлении: {str(e)}"
        )
        return ADDING_SHIFT_TYPE_DATA

async def list_shift_types(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать список всех типов смен"""
    shift_types = get_shift_types()
    
    if not shift_types:
        await update.message.reply_text("❌ Типы смен не найдены")
        return SHIFT_TYPES_MENU
    
    message = "📋 Список типов смен:\n\n"
    keyboard = []
    
    for st in shift_types:
        message += (
            f"🆔 ID: {st.id}\n"
            f"📝 Название: {st.name}\n"
            f"⏰ Время: {st.start_time} - {st.end_time}\n"
            f"📍 Точка: {st.point}\n"
            f"🔧 Тип: {st.shift_type}\n"
            f"---\n"
        )
    
    keyboard.append([
            InlineKeyboardButton(f"✏️ {st.id}", callback_data=f"edit_shift_type_{st.id}"),
            InlineKeyboardButton(f"🗑️ {st.id}", callback_data=f"delete_shift_type_{st.id}")
        ])
    
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_shift_types_management")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(message)
    return SHIFT_TYPES_MENU

async def start_editing_shift_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало редактирования типа смены"""
    await update.message.reply_text(
        "✏️ Редактирование типа смены\n\n"
        "Введите ID типа смены для редактирования:"
    )
    return EDITING_SHIFT_TYPE_ID

async def edit_shift_type_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение ID типа смены для редактирования"""
    try:
        shift_type_id = int(update.message.text.strip())
        shift_type = get_shift_type_by_id(shift_type_id)
        
        if not shift_type:
            await update.message.reply_text("❌ Тип смены с таким ID не найден")
            return await shift_types_management(update, context)
        
        context.user_data['editing_shift_type_id'] = shift_type_id
        
        await update.message.reply_text(
            f"✏️ Редактирование типа смены ID: {shift_type_id}\n"
            f"Текущие данные:\n"
            f"Название: {shift_type.name}\n"
            f"Время: {shift_type.start_time} - {shift_type.end_time}\n"
            f"Точка: {shift_type.point}\n"
            f"Тип: {shift_type.shift_type}\n\n"
            "Введите новые данные в формате:\n"
            "Название|Время начала|Время окончания|Точка|Тип смены\n\n"
            "Или введите /cancel для отмены"
        )
        return EDITING_SHIFT_TYPE_FIELD
        
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID. Введите число")
        return EDITING_SHIFT_TYPE_ID

async def edit_shift_type_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка редактирования типа смены"""
    try:
        shift_type_id = context.user_data.get('editing_shift_type_id')
        if not shift_type_id:
            await update.message.reply_text("❌ Ошибка: ID типа смены не найден")
            return await shift_types_management(update, context)
        
        data = update.message.text.split('|')
        if len(data) != 5:
            await update.message.reply_text("❌ Неверный формат данных. Нужно 5 параметров через |. Попробуйте снова.")
            return EDITING_SHIFT_TYPE_FIELD
        
        name, start_time, end_time, point, shift_type = [item.strip() for item in data]
        
        # Валидация точек
        if point not in ['ДЕ', 'УЯ']:
            await update.message.reply_text("❌ Неверная точка. Допустимые значения: ДЕ, УЯ")
            return EDITING_SHIFT_TYPE_FIELD
        
        # Валидация типов смен
        if shift_type not in ['morning', 'hybrid', 'evening']:
            await update.message.reply_text("❌ Неверный тип смены. Допустимые значения: morning, hybrid, evening")
            return EDITING_SHIFT_TYPE_FIELD
        
        # Обновляем тип смены
        success = update_shift_type(shift_type_id, {
            'name': name,
            'start_time': start_time,
            'end_time': end_time,
            'point': point,
            'shift_type': shift_type
        })
        
        if success:
            await update.message.reply_text(
                f"✅ Тип смены ID {shift_type_id} успешно обновлен!\n"
                f"Название: {name}\n"
                f"Время: {start_time} - {end_time}\n"
                f"Точка: {point}\n"
                f"Тип: {shift_type}"
            )
        else:
            await update.message.reply_text("❌ Ошибка при обновлении типа смены")
        
        context.user_data.clear()
        return await shift_types_management(update, context)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при обновлении: {str(e)}")
        return EDITING_SHIFT_TYPE_FIELD

async def deleting_shift_type_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления типа смены"""
    shift_type_id = context.user_data.get('deleting_shift_type_id')
    if not shift_type_id:
        await update.message.reply_text("❌ Ошибка: ID типа смены не найден")
        return await shift_types_management(update, context)
    
    shift_type = get_shift_type_by_id(shift_type_id)
    if not shift_type:
        await update.message.reply_text("❌ Тип смены не найден")
        context.user_data.clear()
        return await shift_types_management(update, context)
    
    entered_name = update.message.text.strip()
    
    if entered_name == shift_type.name:
        # Подтверждение получено, удаляем
        success = delete_shift_type(shift_type_id)
        if success:
            await update.message.reply_text(
                f"✅ Тип смены {shift_type.name} успешно удален."
            )
        else:
            await update.message.reply_text("❌ Ошибка при удалении типа смены")
        context.user_data.clear()
        return await shift_types_management(update, context)
    else:
        await update.message.reply_text(
            "❌ Названия не совпадают. Удаление отменено."
        )
        context.user_data.clear()
        return await shift_types_management(update, context)
    
async def schedule_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления расписанием"""
    keyboard = [
        [KeyboardButton("🔄 Парсить текущий месяц")],
        [KeyboardButton("📅 Парсить следующий месяц")],
        [KeyboardButton("👥 Смены по сотрудникам")],
        [KeyboardButton("➕ Назначить смену вручную")],
        [KeyboardButton("✏️ Изменить смену по ID")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "📅 Управление расписанием\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )
    return SCHEDULE_MENU

async def parse_current_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Парсинг текущего месяца"""
    await update.message.reply_text("🔄 Начинаю парсинг текущего месяца...")
    
    try:
        month_name = get_current_month_name()
        shifts_data = parse_schedule_from_sheet(month_name)
        
        if not shifts_data:
            await update.message.reply_text(f"❌ Не удалось получить данные для {month_name}")
            return await schedule_management(update, context)
        
        # Удаляем старые смены этого месяца
        first_date = min(s['shift_date'] for s in shifts_data)
        last_date = max(s['shift_date'] for s in shifts_data)
        delete_shifts_by_date_range(first_date, last_date)
        
        # Создаем новые смены
        created_count = bulk_create_shifts(shifts_data)
        
        await update.message.reply_text(
            f"✅ Парсинг завершен!\n"
            f"Месяц: {month_name}\n"
            f"Создано/обновлено смен: {created_count}"
        )
    except Exception as e:
        logger.error(f"Ошибка при парсинге текущего месяца: {e}")
        await update.message.reply_text(f"❌ Ошибка при парсинге: {str(e)}")
    
    return await schedule_management(update, context)

async def parse_next_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Парсинг следующего месяца"""
    await update.message.reply_text("🔄 Начинаю парсинг следующего месяца...")
    
    try:
        month_name = get_next_month_name()
        shifts_data = parse_schedule_from_sheet(month_name)
        
        if not shifts_data:
            await update.message.reply_text(f"❌ Не удалось получить данные для {month_name}")
            return await schedule_management(update, context)
        
        # Удаляем старые смены этого месяца
        first_date = min(s['shift_date'] for s in shifts_data)
        last_date = max(s['shift_date'] for s in shifts_data)
        delete_shifts_by_date_range(first_date, last_date)
        
        # Создаем новые смены
        created_count = bulk_create_shifts(shifts_data)
        
        await update.message.reply_text(
            f"✅ Парсинг завершен!\n"
            f"Месяц: {month_name}\n"
            f"Создано/обновлено смен: {created_count}"
        )
    except Exception as e:
        logger.error(f"Ошибка при парсинге следующего месяца: {e}")
        await update.message.reply_text(f"❌ Ошибка при парсинге: {str(e)}")
    
    return await schedule_management(update, context)

async def select_employee_for_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор сотрудника для просмотра смен"""
    users = get_all_users(active_only=True)
    users_with_iiko = [u for u in users if u.iiko_id]
    
    if not users_with_iiko:
        await update.message.reply_text("❌ Нет сотрудников с указанным iiko_id")
        return await schedule_management(update, context)
    
    keyboard = []
    text = "👥 Выберите сотрудника:\n\n"
    
    for user in users_with_iiko:
        text += f"• {user.name} (ID: {user.iiko_id})\n"
        keyboard.append([InlineKeyboardButton(
            user.name,
            callback_data=f"view_shifts_{user.iiko_id}"
        )])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_schedule")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup)
    return SELECTING_EMPLOYEE_FOR_SHIFTS

async def handle_employee_shifts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора сотрудника для просмотра смен"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel_schedule":
        await query.edit_message_text("❌ Отменено")
        # Отправляем новое сообщение с меню вместо использования update.message
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="📅 Управление расписанием\n\nВыберите действие:",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("🔄 Парсить текущий месяц")],
                [KeyboardButton("📅 Парсить следующий месяц")],
                [KeyboardButton("👥 Смены по сотрудникам")],
                [KeyboardButton("➕ Назначить смену вручную")],
                [KeyboardButton("✏️ Изменить смену по ID")],
                [KeyboardButton("⬅️ Назад")]
            ], resize_keyboard=True)
        )
        return SCHEDULE_MENU
    
    if query.data.startswith("view_shifts_"):
        iiko_id = query.data.split("_")[2]
        user = get_user_by_iiko_id(int(iiko_id))
        
        if not user:
            await query.edit_message_text("❌ Сотрудник не найден")
            return await schedule_management_callback(context, query.message.chat_id)
        
        # Получаем смены на ближайшие 30 дней
        shifts = get_shifts_by_iiko_id(str(iiko_id), start_date=date.today(), end_date=date.today() + timedelta(days=30))
        
        if not shifts:
            await query.edit_message_text(f"📅 У {user.name} нет смен на ближайшие 30 дней")
        else:
            text = f"📅 Смены сотрудника {user.name}:\n\n"
            
            for shift in shifts:
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
                
                text += f"ID: {shift.shift_id}\n"
                text += f"• {date_str} ({shift_type_text}) {shift.shift_type_obj.point}: {start_str} - {end_str}\n\n"
            
            await query.edit_message_text(text)
        
        # Отправляем меню управления расписанием новым сообщением
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="📅 Управление расписанием\n\nВыберите действие:",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("🔄 Парсить текущий месяц")],
                [KeyboardButton("📅 Парсить следующий месяц")],
                [KeyboardButton("👥 Смены по сотрудникам")],
                [KeyboardButton("➕ Назначить смену вручную")],
                [KeyboardButton("✏️ Изменить смену по ID")],
                [KeyboardButton("⬅️ Назад")]
            ], resize_keyboard=True)
        )
        return SCHEDULE_MENU

async def schedule_management_callback(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Вспомогательная функция для отправки меню расписания из callback"""
    await context.bot.send_message(
        chat_id=chat_id,
        text="📅 Управление расписанием\n\nВыберите действие:",
        reply_markup=ReplyKeyboardMarkup([
            [KeyboardButton("🔄 Парсить текущий месяц")],
            [KeyboardButton("📅 Парсить следующий месяц")],
            [KeyboardButton("👥 Смены по сотрудникам")],
            [KeyboardButton("➕ Назначить смену вручную")],
            [KeyboardButton("✏️ Изменить смену по ID")],
            [KeyboardButton("⬅️ Назад")]
        ], resize_keyboard=True)
    )
    return SCHEDULE_MENU

async def handle_shift_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка callback для редактирования/удаления типов смен"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith("edit_shift_type_"):
        shift_type_id = int(data.split("_")[3])
        context.user_data['editing_shift_type_id'] = shift_type_id
        shift_type = get_shift_type_by_id(shift_type_id)
        if shift_type:
            await query.edit_message_text(
                f"✏️ Редактирование типа смены ID: {shift_type_id}\n"
                f"Текущие данные:\n"
                f"Название: {shift_type.name}\n"
                f"Время: {shift_type.start_time} - {shift_type.end_time}\n"
                f"Точка: {shift_type.point}\n"
                f"Тип: {shift_type.shift_type}\n\n"
                "Введите новые данные в формате:\n"
                "Название|Время начала|Время окончания|Точка|Тип смены\n\n"
                "Или введите /cancel для отмены"
            )
            return EDITING_SHIFT_TYPE_FIELD
        else:
            await query.edit_message_text("❌ Тип смены не найден")
            return SHIFT_TYPES_MENU
    
    elif data.startswith("delete_shift_type_"):
        shift_type_id = int(data.split("_")[3])
        context.user_data['deleting_shift_type_id'] = shift_type_id
        shift_type = get_shift_type_by_id(shift_type_id)
        if shift_type:
            await query.edit_message_text(
                f"🗑️ Удаление типа смены: {shift_type.name}\n\n"
                "⚠️ ВНИМАНИЕ! Это действие нельзя отменить.\n\n"
                f"Для подтверждения введите название типа смены: {shift_type.name}"
            )
            return DELETING_SHIFT_TYPE_CONFIRM
        else:
            await query.edit_message_text("❌ Тип смены не найден")
            return SHIFT_TYPES_MENU
    
    elif data == "back_to_shift_types_management":
        # Возвращаемся в меню управления типами смен
        keyboard = [
            [KeyboardButton("➕ Добавить тип смены")],
            [KeyboardButton("📋 Список типов смен")],
            [KeyboardButton("⬅️ Назад")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await query.edit_message_text(
            "🕒 Управление типами смен\n\n"
            "Выберите действие:",
            reply_markup=None  # Убираем инлайн клавиатуру
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🕒 Управление типами смен\n\nВыберите действие:",
            reply_markup=reply_markup
        )
        return SHIFT_TYPES_MENU
    
    return SHIFT_TYPES_MENU

async def start_adding_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления смены вручную"""
    await update.message.reply_text(
        "➕ Добавление смены\n\n"
        "Введите дату смены в формате DD.MM.YYYY:"
    )
    return ADDING_SHIFT_DATE

async def add_shift_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение даты смены"""
    try:
        date_str = update.message.text.strip()
        shift_date = datetime.strptime(date_str, "%d.%m.%Y").date()
        context.user_data['new_shift_date'] = shift_date
        
        await update.message.reply_text(
            f"Дата: {date_str}\n\n"
            "Введите iiko_id сотрудника:"
        )
        return ADDING_SHIFT_IIKO_ID
    except ValueError:
        await update.message.reply_text("❌ Неверный формат даты. Используйте DD.MM.YYYY")
        return ADDING_SHIFT_DATE

async def add_shift_iiko_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение iiko_id"""
    try:
        iiko_id = str(update.message.text.strip())
        context.user_data['new_shift_iiko_id'] = iiko_id
        
        keyboard = [
            [KeyboardButton("ДЕ"), KeyboardButton("УЯ")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(
            f"Iiko ID: {iiko_id}\n\n"
            "Выберите точку:",
            reply_markup=reply_markup
        )
        return ADDING_SHIFT_POINT
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        return ADDING_SHIFT_IIKO_ID

async def add_shift_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение точки"""
    point = update.message.text.strip()
    if point not in ['ДЕ', 'УЯ']:
        await update.message.reply_text("❌ Выберите точку: ДЕ или УЯ")
        return ADDING_SHIFT_POINT
    
    context.user_data['new_shift_point'] = point
    
    keyboard = [
        [KeyboardButton("🌅 Утро"), KeyboardButton("🌤️ Гибрид")],
        [KeyboardButton("🌆 Вечер")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"Точка: {point}\n\n"
        "Выберите тип смены:",
        reply_markup=reply_markup
    )
    return ADDING_SHIFT_TYPE

async def add_shift_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение типа смены"""
    type_map = {
        "🌅 Утро": "morning",
        "🌤️ Пересмен": "hybrid",
        "🌆 Вечер": "evening"
    }
    
    shift_type = type_map.get(update.message.text)
    if not shift_type:
        await update.message.reply_text("❌ Выберите тип смены из списка")
        return ADDING_SHIFT_TYPE
    
    context.user_data['new_shift_type'] = shift_type
    
    await update.message.reply_text(
        f"Тип: {update.message.text}\n\n"
        "Введите время начала смены в формате HH:MM:",
        reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)
    )
    return ADDING_SHIFT_START

async def add_shift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение времени начала"""
    try:
        time_str = update.message.text.strip()
        shift_start = datetime.strptime(time_str, "%H:%M").time()
        context.user_data['new_shift_start'] = shift_start
        
        await update.message.reply_text(
            f"Начало: {time_str}\n\n"
            "Введите время окончания смены в формате HH:MM:"
        )
        return ADDING_SHIFT_END
    except ValueError:
        await update.message.reply_text("❌ Неверный формат времени. Используйте HH:MM")
        return ADDING_SHIFT_START

async def add_shift_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение времени окончания и создание смены с синхронизацией"""
    try:
        from datetime import time
        from bot.database.schedule_operations import get_shift_type_by_times
        
        time_str = update.message.text.strip()
        shift_end = datetime.strptime(time_str, "%H:%M").time()
        shift_start = context.user_data['new_shift_start']
        
        # Проверяем, есть ли уже смена у этого сотрудника в этот день
        existing_shifts = get_shifts_by_iiko_id(
            context.user_data['new_shift_iiko_id'],
            start_date=context.user_data['new_shift_date'],
            end_date=context.user_data['new_shift_date']
        )
        
        if existing_shifts:
            await update.message.reply_text(
                f"❌ У сотрудника уже есть смена в этот день:\n"
                f"• {existing_shifts[0].shift_date.strftime('%d.%m.%Y')}\n"
                f"• {existing_shifts[0].shift_type_obj.point if existing_shifts[0].shift_type_obj else 'Неизвестно'}\n"
                f"• {existing_shifts[0].shift_type_obj.start_time.strftime('%H:%M') if existing_shifts[0].shift_type_obj else ''} - "
                f"{existing_shifts[0].shift_type_obj.end_time.strftime('%H:%M') if existing_shifts[0].shift_type_obj else ''}\n\n"
                f"Сначала удалите существующую смену или выберите другую дату."
            )
            context.user_data.clear()
            return await schedule_management(update, context)
        
        # Находим shift_type_id по времени
        shift_type_obj = get_shift_type_by_times(shift_start, shift_end)
        if not shift_type_obj:
            await update.message.reply_text(
                f"❌ Не найден тип смены для времени {shift_start.strftime('%H:%M')} - {shift_end.strftime('%H:%M')}\n"
                "Проверьте правильность времени."
            )
            return ADDING_SHIFT_END
        
        # Создаем смену
        shift = create_shift(
            shift_date=context.user_data['new_shift_date'],
            iiko_id=context.user_data['new_shift_iiko_id'],
            shift_type_id=shift_type_obj.id
        )
        
        if shift:
            # Синхронизируем с Google Sheets
            from bot.utils.google_sheets import update_shift_in_sheets
            sync_success = update_shift_in_sheets(
                iiko_id=context.user_data['new_shift_iiko_id'],
                shift_date=context.user_data['new_shift_date'],
                start_time=shift_start.strftime("%H:%M"),
                end_time=shift_end.strftime("%H:%M"),
                point=shift_type_obj.point
            )
            
            if sync_success:
                await update.message.reply_text(
                    f"✅ Смена успешно создана и синхронизирована!\n"
                    f"ID: {shift.shift_id}"
                )
            else:
                await update.message.reply_text(
                    f"✅ Смена создана, но не синхронизирована с Google Sheets\n"
                    f"ID: {shift.shift_id}\n"
                    f"Сообщите администратору"
                )
        else:
            await update.message.reply_text("❌ Ошибка при создании смены")
        
        context.user_data.clear()
        return await schedule_management(update, context)
        
    except ValueError:
        await update.message.reply_text("❌ Неверный формат времени. Используйте HH:MM")
        return ADDING_SHIFT_END
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при создании смены: {str(e)}")
        context.user_data.clear()
        return await schedule_management(update, context)

async def start_editing_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало редактирования смены"""
    await update.message.reply_text(
        "✏️ Редактирование смены\n\n"
        "Введите ID смены для редактирования:",
        reply_markup=ReplyKeyboardMarkup([
            [KeyboardButton("❌ Отмена")]
        ], resize_keyboard=True)
    )
    return EDITING_SHIFT_ID

async def edit_shift_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода ID смены"""
    if update.message.text == "❌ Отмена":
        return await cancel_editing(update, context)
    
    try:
        shift_id = int(update.message.text.strip())
        shift = get_shift_by_id(shift_id)
        
        if not shift:
            await update.message.reply_text(
                "❌ Смена с таким ID не найдена\n\n"
                "Введите ID смены или '❌ Отмена':"
            )
            return EDITING_SHIFT_ID
        
        context.user_data['editing_shift_id'] = shift_id
        context.user_data['editing_shift'] = shift
        
        # Показываем информацию о смене и меню редактирования
        return await show_shift_editing_menu(update, context)
        
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат ID. Введите число\n\n"
            "Введите ID смены или '❌ Отмена':"
        )
        return EDITING_SHIFT_ID

async def show_shift_editing_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать меню редактирования смены"""
    shift_id = context.user_data.get('editing_shift_id')
    shift = get_shift_by_id(shift_id)
    
    if not shift:
        await update.message.reply_text("❌ Ошибка: смена не найдена")
        return await cancel_editing(update, context)
    
    # Формируем информацию о смене
    text = f"✏️ Редактирование смены ID: {shift_id}\n\n"
    text += f"📅 Дата: {shift.shift_date.strftime('%d.%m.%Y')}\n"
    text += f"👤 Сотрудник: {shift.iiko_id}\n"
    
    if shift.shift_type_obj:
        text += f"📍 Точка: {shift.shift_type_obj.point}\n"
        text += f"🕒 Тип: {shift.shift_type_obj.shift_type}\n"
        text += f"⏰ Время: {shift.shift_type_obj.start_time.strftime('%H:%M')} - {shift.shift_type_obj.end_time.strftime('%H:%M')}\n\n"
    else:
        text += "❌ Тип смены не найден\n\n"
    
    text += "Выберите поле для редактирования:"
    
    keyboard = [
        [KeyboardButton("📅 Дата"), KeyboardButton("👤 Сотрудник")],
        [KeyboardButton("📍 Точка"), KeyboardButton("🕒 Тип смены")],
        [KeyboardButton("⏰ Время"), KeyboardButton("🗑️ Удалить смену")],
        [KeyboardButton("✅ Завершить"), KeyboardButton("❌ Отмена")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(text, reply_markup=reply_markup)
    return EDITING_SHIFT_MENU

async def edit_shift_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование поля смены"""
    # Это упрощенная версия - в реальности нужно сделать более сложную логику
    await update.message.reply_text(
        "⚠️ Функция редактирования смены в разработке.\n"
        "Используйте удаление и создание новой смены."
    )
    context.user_data.clear()
    return await schedule_management(update, context)

async def editing_shift_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора в меню редактирования"""
    choice = update.message.text
    shift_id = context.user_data.get('editing_shift_id')
    
    if choice == "❌ Отмена":
        return await cancel_editing(update, context)
    
    if choice == "✅ Завершить":
        await update.message.reply_text(
            "✅ Редактирование завершено",
            reply_markup=get_main_menu()
        )
        context.user_data.clear()
        return ConversationHandler.END
    
    if choice == "📅 Дата":
        await update.message.reply_text(
            "Введите новую дату в формате DD.MM.YYYY:",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("❌ Отмена")]
            ], resize_keyboard=True)
        )
        return EDITING_SHIFT_DATE
        
    elif choice == "👤 Сотрудник":
        await update.message.reply_text(
            "Введите новый iiko_id сотрудника:",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("❌ Отмена")]
            ], resize_keyboard=True)
        )
        return EDITING_SHIFT_IIKO_ID
        
    elif choice == "📍 Точка":
        keyboard = [
            [KeyboardButton("ДЕ"), KeyboardButton("УЯ")],
            [KeyboardButton("❌ Отмена")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("Выберите точку:", reply_markup=reply_markup)
        return EDITING_SHIFT_POINT
        
    elif choice == "🕒 Тип смены":
        keyboard = [
            [KeyboardButton("🌅 Утро"), KeyboardButton("🌤️ Пересмен")],
            [KeyboardButton("🌆 Вечер"), KeyboardButton("❌ Отмена")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("Выберите тип смены:", reply_markup=reply_markup)
        return EDITING_SHIFT_TYPE
        
    elif choice == "⏰ Время":
        await update.message.reply_text(
            "Введите новое время в формате ЧЧ:ММ-ЧЧ:ММ (например, 09:00-17:00):",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("❌ Отмена")]
            ], resize_keyboard=True)
        )
        return EDITING_SHIFT_TIME
        
    elif choice == "🗑️ Удалить смену":
        keyboard = [
            [KeyboardButton("✅ Да, удалить"), KeyboardButton("❌ Нет, отмена")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            "⚠️ Вы уверены, что хотите удалить эту смену?",
            reply_markup=reply_markup
        )
        return DELETING_SHIFT_CONFIRM
    
    return await show_shift_editing_menu(update, context)

async def deleting_shift_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления смены"""
    choice = update.message.text
    
    if choice == "❌ Нет, отмена":
        return await show_shift_editing_menu(update, context)
    
    if choice == "✅ Да, удалить":
        shift_id = context.user_data.get('editing_shift_id')
        shift = get_shift_by_id(shift_id)
        
        if shift:
            # Сохраняем данные для синхронизации перед удалением
            shift_data = {
                'iiko_id': shift.iiko_id,
                'date': shift.shift_date,
                'start_time': shift.shift_type_obj.start_time if shift.shift_type_obj else None,
                'end_time': shift.shift_type_obj.end_time if shift.shift_type_obj else None,
                'point': shift.shift_type_obj.point if shift.shift_type_obj else None
            }
            
            # Удаляем смену
            success = delete_shift(shift_id)
            
            if success:
                # Синхронизируем удаление с Google Sheets
                from bot.utils.google_sheets import update_shift_in_sheets
                update_shift_in_sheets(
                    iiko_id=shift_data['iiko_id'],
                    shift_date=shift_data['date'],
                    start_time=None,  # Очищаем смену
                    end_time=None,
                    point=None
                )
                
                await update.message.reply_text(
                    "✅ Смена успешно удалена",
                    reply_markup=get_main_menu()
                )
            else:
                await update.message.reply_text(
                    "❌ Ошибка при удалении смены",
                    reply_markup=get_main_menu()
                )
        else:
            await update.message.reply_text(
                "❌ Смена не найдена",
                reply_markup=get_main_menu()
            )
        
        context.user_data.clear()
        return ConversationHandler.END
    
    return await show_shift_editing_menu(update, context)

async def sync_shift_to_sheets(shift):
    """Синхронизировать смену с Google Sheets с обработкой ошибок"""
    try:
        from bot.utils.google_sheets import update_shift_in_sheets
        
        if shift and shift.shift_type_obj:
            sync_success = update_shift_in_sheets(
                iiko_id=shift.iiko_id,
                shift_date=shift.shift_date,
                start_time=shift.shift_type_obj.start_time.strftime("%H:%M"),
                end_time=shift.shift_type_obj.end_time.strftime("%H:%M"),
                point=shift.shift_type_obj.point
            )
            
            return sync_success
        return False
        
    except Exception as e:
        logger.error(f"❌ Ошибка синхронизации смены {shift.shift_id if shift else 'N/A'} с Google Sheets: {e}")
        return False

async def edit_shift_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование даты смены с сообщением об ожидании"""
    if update.message.text == "❌ Отмена":
        return await show_shift_editing_menu(update, context)
    
    # Показываем сообщение об ожидании
    wait_message = await update.message.reply_text("🔄 Применяем изменения...")
    
    try:
        new_date = datetime.strptime(update.message.text.strip(), "%d.%m.%Y").date()
        shift_id = context.user_data.get('editing_shift_id')
        
        # Обновляем смену
        updated_shift = update_shift(shift_id, shift_date=new_date)
        
        if updated_shift:
            # Удаляем сообщение об ожидании
            await wait_message.delete()
            
            # Синхронизируем с Google Sheets
            sync_success = await sync_shift_to_sheets(updated_shift)
            
            if sync_success:
                await update.message.reply_text(f"✅ Дата изменена на: {new_date.strftime('%d.%m.%Y')}")
            else:
                await update.message.reply_text(
                    f"✅ Дата изменена, но ошибка синхронизации с Google Sheets\n"
                    f"Новая дата: {new_date.strftime('%d.%m.%Y')}"
                )
        else:
            await wait_message.delete()
            await update.message.reply_text("❌ Ошибка при изменении даты")
            
    except ValueError:
        await wait_message.delete()
        await update.message.reply_text(
            "❌ Неверный формат даты. Используйте DD.MM.YYYY\n\n"
            "Введите дату или '❌ Отмена':"
        )
        return EDITING_SHIFT_DATE
    
    return await show_shift_editing_menu(update, context)

# Аналогично обновим другие функции редактирования:
# - edit_shift_iiko_id
# - edit_shift_point  
# - edit_shift_type
# - edit_shift_time

async def deleting_shift_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления смены с синхронизацией"""
    choice = update.message.text
    
    if choice == "❌ Нет, отмена":
        return await show_shift_editing_menu(update, context)
    
    if choice == "✅ Да, удалить":
        # Показываем сообщение об ожидании
        wait_message = await update.message.reply_text("🔄 Удаляем смену...")
        
        shift_id = context.user_data.get('editing_shift_id')
        shift = get_shift_by_id(shift_id)
        
        if shift:
            # Сохраняем данные для синхронизации перед удалением
            shift_data = {
                'iiko_id': shift.iiko_id,
                'date': shift.shift_date,
                'start_time': shift.shift_type_obj.start_time if shift.shift_type_obj else None,
                'end_time': shift.shift_type_obj.end_time if shift.shift_type_obj else None,
                'point': shift.shift_type_obj.point if shift.shift_type_obj else None
            }
            
            # Удаляем смену
            success = delete_shift(shift_id)
            
            if success:
                # Синхронизируем удаление с Google Sheets
                from bot.utils.google_sheets import update_shift_in_sheets
                sync_success = update_shift_in_sheets(
                    iiko_id=shift_data['iiko_id'],
                    shift_date=shift_data['date'],
                    start_time=None,  # Очищаем смену
                    end_time=None,
                    point=None
                )
                
                await wait_message.delete()
                
                if sync_success:
                    await update.message.reply_text(
                        "✅ Смена успешно удалена и синхронизирована",
                        reply_markup=get_main_menu()
                    )
                else:
                    await update.message.reply_text(
                        "✅ Смена удалена, но ошибка синхронизации с Google Sheets",
                        reply_markup=get_main_menu()
                    )
            else:
                await wait_message.delete()
                await update.message.reply_text(
                    "❌ Ошибка при удалении смены",
                    reply_markup=get_main_menu()
                )
        else:
            await wait_message.delete()
            await update.message.reply_text(
                "❌ Смена не найдена",
                reply_markup=get_main_menu()
            )
        
        context.user_data.clear()
        return ConversationHandler.END
    
    return await show_shift_editing_menu(update, context)

async def edit_shift_iiko_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование сотрудника смены"""
    if update.message.text == "❌ Отмена":
        return await show_shift_editing_menu(update, context)
    
    try:
        new_iiko_id = str(update.message.text.strip())
        shift_id = context.user_data.get('editing_shift_id')
        
        # Проверяем существование пользователя
        from bot.database.user_operations import get_user_by_iiko_id
        user = get_user_by_iiko_id(int(new_iiko_id))
        
        if not user:
            await update.message.reply_text(
                "❌ Сотрудник с таким iiko_id не найден\n\n"
                "Введите iiko_id или '❌ Отмена':"
            )
            return EDITING_SHIFT_IIKO_ID
        
        # Обновляем смену
        updated_shift = update_shift_iiko_id(shift_id, new_iiko_id)
        
        if updated_shift:
            await update.message.reply_text(f"✅ Сотрудник изменен на: {user.name}")
            await sync_shift_to_sheets(updated_shift)
        else:
            await update.message.reply_text("❌ Ошибка при изменении сотрудника")
            
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат iiko_id\n\n"
            "Введите iiko_id или '❌ Отмена':"
        )
        return EDITING_SHIFT_IIKO_ID
    
    return await show_shift_editing_menu(update, context)
    
async def edit_shift_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование точки смены"""
    if update.message.text == "❌ Отмена":
        return await show_shift_editing_menu(update, context)
    
    point = update.message.text
    if point not in ['ДЕ', 'УЯ']:
        await update.message.reply_text(
            "❌ Выберите точку: ДЕ или УЯ\n\n"
            "Выберите точку или '❌ Отмена':"
        )
        return EDITING_SHIFT_POINT
    
    # Для изменения точки нужно создать новый тип смены или найти существующий
    shift_id = context.user_data.get('editing_shift_id')
    shift = get_shift_by_id(shift_id)
    
    if shift and shift.shift_type_obj:
        # Находим тип смены с той же временем но другой точкой
        from bot.database.schedule_operations import get_shift_type_by_times
        new_shift_type = get_shift_type_by_times(
            shift.shift_type_obj.start_time,
            shift.shift_type_obj.end_time
        )
        
        # Ищем тип смены с нужной точкой
        from bot.database.schedule_operations import get_shift_types
        all_shift_types = get_shift_types()
        for st in all_shift_types:
            if (st.start_time == shift.shift_type_obj.start_time and
                st.end_time == shift.shift_type_obj.end_time and
                st.point == point):
                new_shift_type = st
                break
        
        if new_shift_type:
            updated_shift = update_shift(shift_id, shift_type_id=new_shift_type.id)
            if updated_shift:
                await update.message.reply_text(f"✅ Точка изменена на: {point}")
                await sync_shift_to_sheets(updated_shift)
            else:
                await update.message.reply_text("❌ Ошибка при изменении точки")
        else:
            await update.message.reply_text("❌ Не найден подходящий тип смены")
    
    return await show_shift_editing_menu(update, context)    
    
async def edit_shift_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование типа смены"""
    if update.message.text == "❌ Отмена":
        return await show_shift_editing_menu(update, context)
    
    type_map = {
        "🌅 Утро": "morning",
        "🌤️ Пересмен": "hybrid", 
        "🌆 Вечер": "evening"
    }
    
    shift_type_text = update.message.text
    if shift_type_text not in type_map:
        await update.message.reply_text(
            "❌ Выберите тип смены из списка\n\n"
            "Выберите тип или '❌ Отмена':"
        )
        return EDITING_SHIFT_TYPE
    
    new_shift_type = type_map[shift_type_text]
    shift_id = context.user_data.get('editing_shift_id')
    shift = get_shift_by_id(shift_id)
    
    if shift and shift.shift_type_obj:
        # Ищем тип смены с той же точкой но другим временем/типом
        from bot.database.schedule_operations import get_shift_types
        all_shift_types = get_shift_types()
        
        for st in all_shift_types:
            if (st.point == shift.shift_type_obj.point and
                st.shift_type == new_shift_type):
                # Нашли подходящий тип смены
                updated_shift = update_shift(shift_id, shift_type_id=st.id)
                if updated_shift:
                    await update.message.reply_text(f"✅ Тип смены изменен на: {shift_type_text}")
                    await sync_shift_to_sheets(updated_shift)
                else:
                    await update.message.reply_text("❌ Ошибка при изменении типа смены")
                break
        else:
            await update.message.reply_text("❌ Не найден подходящий тип смены")
    
    return await show_shift_editing_menu(update, context)

async def edit_shift_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование времени смены"""
    if update.message.text == "❌ Отмена":
        return await show_shift_editing_menu(update, context)
    
    try:
        time_range = update.message.text.strip()
        if '-' not in time_range:
            raise ValueError
        
        start_str, end_str = time_range.split('-')
        start_time = datetime.strptime(start_str.strip(), "%H:%M").time()
        end_time = datetime.strptime(end_str.strip(), "%H:%M").time()
        
        shift_id = context.user_data.get('editing_shift_id')
        
        # Находим тип смены по времени
        from bot.database.schedule_operations import get_shift_type_by_times
        new_shift_type = get_shift_type_by_times(start_time, end_time)
        
        if new_shift_type:
            updated_shift = update_shift(shift_id, shift_type_id=new_shift_type.id)
            if updated_shift:
                await update.message.reply_text(f"✅ Время изменено на: {start_str}-{end_str}")
                await sync_shift_to_sheets(updated_shift)
            else:
                await update.message.reply_text("❌ Ошибка при изменении времени")
        else:
            await update.message.reply_text(
                "❌ Не найден тип смены для указанного времени\n\n"
                "Введите время в формате ЧЧ:ММ-ЧЧ:ММ или '❌ Отмена':"
            )
            return EDITING_SHIFT_TIME
            
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат времени. Используйте ЧЧ:ММ-ЧЧ:ММ\n\n"
            "Введите время или '❌ Отмена':"
        )
        return EDITING_SHIFT_TIME
    
    return await show_shift_editing_menu(update, context)

async def show_editing_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать меню редактирования"""
    shift_id = context.user_data.get('editing_shift_id')
    shift = get_shift_by_id(shift_id)
    
    text = f"✏️ Редактирование смены ID: {shift_id}\n\n"
    text += f"📅 Дата: {shift.shift_date.strftime('%d.%m.%Y')}\n"
    text += f"👤 Сотрудник: {shift.iiko_id}\n"
    if shift.shift_type_obj:
        text += f"📍 Точка: {shift.shift_type_obj.point}\n"
        text += f"🕒 Тип: {shift.shift_type_obj.shift_type}\n"
        text += f"⏰ Время: {shift.shift_type_obj.start_time.strftime('%H:%M')} - {shift.shift_type_obj.end_time.strftime('%H:%M')}\n\n"
    
    keyboard = [
        [KeyboardButton("📅 Изменить дату"), KeyboardButton("👤 Изменить сотрудника")],
        [KeyboardButton("📍 Изменить точку"), KeyboardButton("🕒 Изменить тип")],
        [KeyboardButton("⏰ Изменить время"), KeyboardButton("🗑️ Удалить смену")],
        [KeyboardButton("✅ Завершить редактирование")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(text + "Выберите действие:", reply_markup=reply_markup)
    return EDITING_SHIFT_MENU


@require_roles([ROLE_SENIOR, ROLE_MENTOR])
async def checklist_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления чек-листами"""
    keyboard = [
        [KeyboardButton("📋 Управление шаблонами")],
        [KeyboardButton("🔄 Управление пересменами")],
        [KeyboardButton("📊 Статистика выполнения")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "⚙️ Управление чек-листами\n\n"
        "Выберите раздел:",
        reply_markup=reply_markup
    )
    return CHECKLIST_MANAGEMENT_MENU

# ========== ОБРАБОТЧИКИ УПРАВЛЕНИЯ ЧЕК-ЛИСТАМИ ==========

async def templates_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления шаблонами задач"""
    keyboard = [
        [KeyboardButton("➕ Добавить задачу")],
        [KeyboardButton("📋 Просмотреть задачи")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "📋 Управление шаблонами задач\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )
    return CHECKLIST_MANAGEMENT_MENU

async def hybrid_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню управления пересменами"""
    keyboard = [
        [KeyboardButton("🔄 Настроить распределение")],
        [KeyboardButton("📋 Просмотреть распределения")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "🔄 Управление пересменами\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )
    return CHECKLIST_MANAGEMENT_MENU

async def start_hybrid_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало настройки распределения задач для пересмена"""
    keyboard = [
        [KeyboardButton("ДЕ"), KeyboardButton("УЯ")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "🔄 Настройка распределения для пересмена\n\n"
        "Выберите точку:",
        reply_markup=reply_markup
    )
    return HYBRID_SELECT_POINT

async def hybrid_select_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор точки для настройки пересмена"""
    point = update.message.text
    if point not in ['ДЕ', 'УЯ']:
        await update.message.reply_text("❌ Выберите точку: ДЕ или УЯ")
        return HYBRID_SELECT_POINT
    
    context.user_data['hybrid_point'] = point
    
    keyboard = [
        [KeyboardButton("Понедельник"), KeyboardButton("Вторник"), KeyboardButton("Среда")],
        [KeyboardButton("Четверг"), KeyboardButton("Пятница"), KeyboardButton("Суббота")],
        [KeyboardButton("Воскресенье"), KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"📍 Точка: {point}\n\n"
        "Выберите день недели:",
        reply_markup=reply_markup
    )
    return HYBRID_SELECT_DAY

async def hybrid_select_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор дня недели и отображение текущих задач"""
    day_map = {
        "Понедельник": 0, "Вторник": 1, "Среда": 2,
        "Четверг": 3, "Пятница": 4, "Суббота": 5, "Воскресенье": 6
    }
    
    day_name = update.message.text
    if day_name not in day_map:
        await update.message.reply_text("❌ Выберите день недели из списка")
        return HYBRID_SELECT_DAY
    
    day = day_map[day_name]
    context.user_data['hybrid_day'] = day
    context.user_data['hybrid_day_name'] = day_name
    
    # Получаем задачи для этой точки и дня
    from bot.database.checklist_operations import get_checklist_templates
    
    morning_tasks = get_checklist_templates(
        point=context.user_data['hybrid_point'],
        day_of_week=day,
        shift_type='morning'
    )
    
    evening_tasks = get_checklist_templates(
        point=context.user_data['hybrid_point'],
        day_of_week=day,
        shift_type='evening'
    )
    
    if not morning_tasks and not evening_tasks:
        await update.message.reply_text(
            f"❌ Нет задач для {context.user_data['hybrid_point']} в {day_name}.\n"
            "Сначала создайте задачи в разделе '📋 Управление шаблонами'."
        )
        return await start_hybrid_setup(update, context)
    
    # Формируем сообщение с текущими задачами
    response = f"🔄 Распределение задач для пересмена\n\n"
    response += f"📍 {context.user_data['hybrid_point']} | {day_name}\n\n"
    
    response += "🌅 Утренние задачи:\n"
    if morning_tasks:
        for i, task in enumerate(morning_tasks, 1):
            response += f"  {i}. {task.task_description}\n"
    else:
        response += "  Нет задач\n"
    
    response += "\n🌆 Вечерние задачи:\n"
    if evening_tasks:
        for i, task in enumerate(evening_tasks, 1):
            response += f"  {i}. {task.task_description}\n"
    else:
        response += "  Нет задач\n"
    
    # Сохраняем задачи в контексте
    context.user_data['morning_tasks'] = morning_tasks
    context.user_data['evening_tasks'] = evening_tasks
    
    keyboard = [
        [KeyboardButton("✅ Продолжить настройку")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        response + "\nПродолжить настройку распределения?",
        reply_markup=reply_markup
    )
    return HYBRID_VIEW_CURRENT

async def hybrid_view_current(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отображение текущих задач и начало выбора для пересмена"""
    morning_tasks = context.user_data.get('morning_tasks', [])
    
    if not morning_tasks:
        await update.message.reply_text("❌ Нет утренних задач для выбора")
        return await start_hybrid_setup(update, context)
    
    response = "🌅 Выберите утренние задачи для пересмена (можно несколько через запятую):\n\n"
    
    for i, task in enumerate(morning_tasks, 1):
        response += f"{i}. {task.task_description}\n"
    
    await update.message.reply_text(
        response + "\nВведите номера утренних задач через запятую (например: 1,3,5):",
        reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)
    )
    return HYBRID_SELECT_MORNING_TASK

async def hybrid_select_morning_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор утренних задач для пересмена"""
    try:
        task_numbers_str = update.message.text
        task_numbers = [int(num.strip()) for num in task_numbers_str.split(',')]
        morning_tasks = context.user_data.get('morning_tasks', [])
        
        # Проверяем валидность всех номеров
        for task_num in task_numbers:
            if task_num < 1 or task_num > len(morning_tasks):
                await update.message.reply_text(f"❌ Неверный номер задачи: {task_num}")
                return HYBRID_SELECT_MORNING_TASK
        
        selected_tasks = [morning_tasks[num - 1] for num in task_numbers]
        context.user_data['selected_morning_tasks'] = selected_tasks
        context.user_data['selected_morning_task_ids'] = [task.id for task in selected_tasks]
        
        # Переходим к выбору вечерних задач
        evening_tasks = context.user_data.get('evening_tasks', [])
        
        if not evening_tasks:
            await update.message.reply_text("❌ Нет вечерних задач для выбора")
            return await start_hybrid_setup(update, context)
        
        response = "🌆 Выберите вечерние задачи для пересмена (можно несколько через запятую):\n\n"
        
        for i, task in enumerate(evening_tasks, 1):
            response += f"{i}. {task.task_description}\n"
        
        await update.message.reply_text(
            response + "\nВведите номера вечерних задач через запятую (например: 2,4):",
            reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)
        )
        return HYBRID_SELECT_EVENING_TASK
        
    except ValueError:
        await update.message.reply_text("❌ Введите числа через запятую")
        return HYBRID_SELECT_MORNING_TASK

async def hybrid_select_evening_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор вечерних задач для пересмена"""
    try:
        task_numbers_str = update.message.text
        task_numbers = [int(num.strip()) for num in task_numbers_str.split(',')]
        evening_tasks = context.user_data.get('evening_tasks', [])
        
        # Проверяем валидность всех номеров
        for task_num in task_numbers:
            if task_num < 1 or task_num > len(evening_tasks):
                await update.message.reply_text(f"❌ Неверный номер задачи: {task_num}")
                return HYBRID_SELECT_EVENING_TASK
        
        selected_tasks = [evening_tasks[num - 1] for num in task_numbers]
        context.user_data['selected_evening_tasks'] = selected_tasks
        context.user_data['selected_evening_task_ids'] = [task.id for task in selected_tasks]
        
        # Показываем итоговое распределение
        morning_tasks = context.user_data['selected_morning_tasks']
        evening_tasks = context.user_data['selected_evening_tasks']
        
        response = "✅ Итоговое распределение для пересмена:\n\n"
        response += f"📍 {context.user_data['hybrid_point']} | {context.user_data['hybrid_day_name']}\n\n"
        
        response += "🌅 Утренние задачи для пересмена:\n"
        for task in morning_tasks:
            response += f"  • {task.task_description}\n"
        
        response += "\n🌆 Вечерние задачи для пересмена:\n"
        for task in evening_tasks:
            response += f"  • {task.task_description}\n"
        
        response += "\nСохранить это распределение?"
        
        keyboard = [
            [KeyboardButton("✅ Сохранить"), KeyboardButton("🔄 Начать заново")],
            [KeyboardButton("❌ Отмена")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(response, reply_markup=reply_markup)
        return HYBRID_SAVE_ASSIGNMENT
        
    except ValueError:
        await update.message.reply_text("❌ Введите числа через запятую")
        return HYBRID_SELECT_EVENING_TASK

async def hybrid_save_assignment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранение распределения задач для пересмена"""
    from bot.database.checklist_operations import create_hybrid_assignment_with_tasks
    
    if update.message.text == "✅ Сохранить":
        try:
            # Создаем или обновляем распределение
            assignment = create_hybrid_assignment_with_tasks(
                point=context.user_data['hybrid_point'],
                day_of_week=context.user_data['hybrid_day'],
                morning_task_ids=context.user_data['selected_morning_task_ids'],
                evening_task_ids=context.user_data['selected_evening_task_ids']
            )
            
            morning_tasks = context.user_data['selected_morning_tasks']
            evening_tasks = context.user_data['selected_evening_tasks']
            
            response = "✅ Распределение успешно сохранено!\n\n"
            response += f"📍 {context.user_data['hybrid_point']} | {context.user_data['hybrid_day_name']}\n\n"
            
            response += "🌅 Утренние задачи для пересмена:\n"
            for task in morning_tasks:
                response += f"  • {task.task_description}\n"
            
            response += "\n🌆 Вечерние задачи для пересмена:\n"
            for task in evening_tasks:
                response += f"  • {task.task_description}\n"
            
            response += "\nТеперь эти задачи будут отображаться у пересмена, а у утренней и вечерней смен - исключены."
            
            await update.message.reply_text(response)
            
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка при сохранении: {str(e)}")
    
    elif update.message.text == "🔄 Начать заново":
        return await start_hybrid_setup(update, context)
    
    # Очищаем контекст
    context.user_data.clear()
    
    return await hybrid_management(update, context)

async def hybrid_view_existing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр существующих распределений"""
    from bot.database.checklist_operations import get_hybrid_assignments, get_hybrid_assignment_tasks
    
    assignments = get_hybrid_assignments()
    
    if not assignments:
        await update.message.reply_text(
            "📭 Нет сохраненных распределений для пересменов.\n"
            "Сначала создайте распределения в разделе '🔄 Настроить распределение'."
        )
        return await hybrid_management(update, context)
    
    day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    
    response = "📋 Существующие распределения для пересменов:\n\n"
    
    for assignment in assignments:
        response += f"📍 {assignment.point} | {day_names[assignment.day_of_week]}\n"
        
        # Получаем задачи для этого распределения
        morning_tasks = get_hybrid_assignment_tasks(assignment.id, 'morning')
        evening_tasks = get_hybrid_assignment_tasks(assignment.id, 'evening')
        
        response += "🌅 Утренние задачи:\n"
        if morning_tasks:
            for task in morning_tasks:
                response += f"  • {task.task_description}\n"
        else:
            response += "  Нет задач\n"
            
        response += "🌆 Вечерние задачи:\n"
        if evening_tasks:
            for task in evening_tasks:
                response += f"  • {task.task_description}\n"
        else:
            response += "  Нет задач\n"
        
        response += "─" * 30 + "\n\n"
    
    keyboard = [
        [KeyboardButton("✏️ Редактировать распределение")],
        [KeyboardButton("🗑️ Удалить распределение")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    context.user_data['existing_assignments'] = assignments
    
    await update.message.reply_text(response, reply_markup=reply_markup)
    return HYBRID_VIEW_EXISTING

async def hybrid_edit_existing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор распределения для редактирования"""
    assignments = context.user_data.get('existing_assignments', [])
    
    if not assignments:
        await update.message.reply_text("❌ Нет распределений для редактирования")
        return await hybrid_management(update, context)
    
    day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    
    response = "✏️ Выберите распределение для редактирования:\n\n"
    
    for i, assignment in enumerate(assignments, 1):
        response += f"{i}. 📍 {assignment.point} | {day_names[assignment.day_of_week]}\n"
        
        # Получаем задачи для этого распределения
        morning_tasks = get_hybrid_assignment_tasks(assignment.id, 'morning')
        evening_tasks = get_hybrid_assignment_tasks(assignment.id, 'evening')
        
        response += "   🌅 Утренние задачи:\n"
        if morning_tasks:
            for task in morning_tasks:
                response += f"     • {task.task_description}\n"
        else:
            response += "     Нет задач\n"
            
        response += "   🌆 Вечерние задачи:\n"
        if evening_tasks:
            for task in evening_tasks:
                response += f"     • {task.task_description}\n"
        else:
            response += "     Нет задач\n"
        
        response += "\n"
    
    await update.message.reply_text(
        response + "Введите номер распределения для редактирования:",
        reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)
    )
    return HYBRID_EDIT_EXISTING

async def hybrid_edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора распределения для редактирования"""
    try:
        # Проверяем, не нажата ли кнопка "Назад"
        if update.message.text == "⬅️ Назад":
            return await hybrid_management(update, context)
        
        assignment_number = int(update.message.text)
        assignments = context.user_data.get('existing_assignments', [])
        
        if assignment_number < 1 or assignment_number > len(assignments):
            await update.message.reply_text("❌ Неверный номер распределения. Попробуйте снова:")
            return HYBRID_EDIT_EXISTING
        
        assignment = assignments[assignment_number - 1]
        
        # Сохраняем выбранное распределение для редактирования
        context.user_data['editing_assignment'] = assignment
        context.user_data['editing_assignment_id'] = assignment.id
        context.user_data['hybrid_point'] = assignment.point
        context.user_data['hybrid_day'] = assignment.day_of_week
        context.user_data['hybrid_day_name'] = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"][assignment.day_of_week]
        
        # Получаем текущие задачи для этой точки и дня
        from bot.database.checklist_operations import get_checklist_templates, get_hybrid_assignment_tasks
        
        morning_tasks = get_checklist_templates(
            point=assignment.point,
            day_of_week=assignment.day_of_week,
            shift_type='morning'
        )
        
        evening_tasks = get_checklist_templates(
            point=assignment.point,
            day_of_week=assignment.day_of_week,
            shift_type='evening'
        )
        
        # Получаем уже выбранные задачи для этого распределения
        current_morning_tasks = get_hybrid_assignment_tasks(assignment.id, 'morning')
        current_evening_tasks = get_hybrid_assignment_tasks(assignment.id, 'evening')
        
        # Сохраняем задачи в контексте
        context.user_data['morning_tasks'] = morning_tasks
        context.user_data['evening_tasks'] = evening_tasks
        context.user_data['current_morning_task_ids'] = [task.id for task in current_morning_tasks]
        context.user_data['current_evening_task_ids'] = [task.id for task in current_evening_tasks]
        
        # Показываем текущее распределение и начинаем процесс редактирования
        response = f"✏️ Редактирование распределения:\n\n"
        response += f"📍 {assignment.point} | {context.user_data['hybrid_day_name']}\n\n"
        
        response += "🌅 Текущие утренние задачи для пересмена:\n"
        if current_morning_tasks:
            for task in current_morning_tasks:
                response += f"  • {task.task_description}\n"
        else:
            response += "  • Нет задач\n"
        
        response += "\n🌆 Текущие вечерние задачи для пересмена:\n"
        if current_evening_tasks:
            for task in current_evening_tasks:
                response += f"  • {task.task_description}\n"
        else:
            response += "  • Нет задач\n"
        
        response += "\nПродолжить редактирование?"
        
        keyboard = [
            [KeyboardButton("✅ Продолжить редактирование")],
            [KeyboardButton("❌ Отмена")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        await update.message.reply_text(response, reply_markup=reply_markup)
        return HYBRID_VIEW_CURRENT
        
    except ValueError:
        await update.message.reply_text("❌ Введите число или нажмите '⬅️ Назад' для возврата:")
        return HYBRID_EDIT_EXISTING
    except Exception as e:
        logger.error(f"Ошибка в hybrid_edit_select: {e}")
        await update.message.reply_text(
            "❌ Произошла ошибка. Возврат в меню управления пересменами."
        )
        return await hybrid_management(update, context)

async def hybrid_delete_existing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор распределения для удаления"""
    assignments = context.user_data.get('existing_assignments', [])
    
    if not assignments:
        await update.message.reply_text("❌ Нет распределений для удаления")
        return await hybrid_management(update, context)
    
    day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    
    response = "🗑️ Выберите распределение для удаления:\n\n"
    
    for i, assignment in enumerate(assignments, 1):
        response += f"{i}. 📍 {assignment.point} | {day_names[assignment.day_of_week]}\n"
        
        # Получаем задачи для этого распределения
        morning_tasks = get_hybrid_assignment_tasks(assignment.id, 'morning')
        evening_tasks = get_hybrid_assignment_tasks(assignment.id, 'evening')
        
        response += "   🌅 Утренние задачи:\n"
        if morning_tasks:
            for task in morning_tasks:
                response += f"     • {task.task_description}\n"
        else:
            response += "     Нет задач\n"
            
        response += "   🌆 Вечерние задачи:\n"
        if evening_tasks:
            for task in evening_tasks:
                response += f"     • {task.task_description}\n"
        else:
            response += "     Нет задач\n"
        
        response += "\n"
    
    await update.message.reply_text(
        response + "Введите номер распределения для удаления:",
        reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)
    )
    return HYBRID_DELETE_EXISTING

async def hybrid_delete_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора распределения для удаления"""
    try:
        # Проверяем, не нажата ли кнопка "Назад"
        if update.message.text == "⬅️ Назад":
            return await hybrid_management(update, context)
        
        assignment_number = int(update.message.text)
        assignments = context.user_data.get('existing_assignments', [])
        
        if assignment_number < 1 or assignment_number > len(assignments):
            await update.message.reply_text("❌ Неверный номер распределения. Попробуйте снова:")
            return HYBRID_DELETE_EXISTING
        
        assignment = assignments[assignment_number - 1]
        context.user_data['deleting_assignment'] = assignment
        context.user_data['deleting_assignment_id'] = assignment.id
        
        day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        
        # Получаем задачи для этого распределения
        from bot.database.checklist_operations import get_hybrid_assignment_tasks
        
        morning_tasks = get_hybrid_assignment_tasks(assignment.id, 'morning')
        evening_tasks = get_hybrid_assignment_tasks(assignment.id, 'evening')
        
        response = "🗑️ Подтверждение удаления:\n\n"
        response += f"📍 {assignment.point} | {day_names[assignment.day_of_week]}\n\n"
        
        response += "🌅 Утренние задачи:\n"
        if morning_tasks:
            for task in morning_tasks:
                response += f"  • {task.task_description}\n"
        else:
            response += "  Нет задач\n"
            
        response += "\n🌆 Вечерние задачи:\n"
        if evening_tasks:
            for task in evening_tasks:
                response += f"  • {task.task_description}\n"
        else:
            response += "  Нет задач\n"
        
        response += "\n⚠️ Вы уверены, что хотите удалить это распределение?\n"
        response += "Введите 'ДА' для подтверждения или 'НЕТ' для отмены:"
        
        await update.message.reply_text(
            response,
            reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)
        )
        return HYBRID_DELETE_CONFIRM
        
    except ValueError:
        await update.message.reply_text("❌ Введите число или нажмите '⬅️ Назад' для возврата:")
        return HYBRID_DELETE_EXISTING
    except Exception as e:
        logger.error(f"Ошибка в hybrid_delete_select: {e}")
        await update.message.reply_text(
            "❌ Произошла ошибка. Возврат в меню управления пересменами."
        )
        return await hybrid_management(update, context)

async def hybrid_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления распределения"""
    confirmation = update.message.text.upper().strip()
    assignment_id = context.user_data.get('deleting_assignment_id')
    
    if confirmation in ['ДА', 'YES']:
        try:
            from bot.database.checklist_operations import delete_hybrid_assignment
            
            success = delete_hybrid_assignment(assignment_id)
            
            if success:
                await update.message.reply_text("✅ Распределение успешно удалено!")
            else:
                await update.message.reply_text("❌ Ошибка при удалении распределения")
                
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    elif confirmation in ['НЕТ', 'NO']:
        await update.message.reply_text("❌ Удаление отменено")
    else:
        await update.message.reply_text("❌ Введите 'ДА' или 'НЕТ'")
        return HYBRID_DELETE_CONFIRM
    
    # Очищаем контекст
    context.user_data.pop('deleting_assignment', None)
    context.user_data.pop('deleting_assignment_id', None)
    context.user_data.pop('existing_assignments', None)
    
    return await hybrid_management(update, context)

async def checklist_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика выполнения чек-листов"""
    from bot.database.checklist_operations import get_checklist_templates
    from datetime import datetime, timedelta
    
    templates = get_checklist_templates()
    
    if not templates:
        await update.message.reply_text(
            "📊 Статистика выполнения чек-листов\n\n"
            "❌ Нет созданных шаблонов задач для анализа."
        )
        return CHECKLIST_MANAGEMENT_MENU
    
    # Простая статистика
    total_tasks = len(templates)
    
    # Группируем по точкам
    points = {}
    for template in templates:
        if template.point not in points:
            points[template.point] = 0
        points[template.point] += 1
    
    # Группируем по дням недели
    days = {}
    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for template in templates:
        day_name = day_names[template.day_of_week]
        if day_name not in days:
            days[day_name] = 0
        days[day_name] += 1
    
    response = "📊 Статистика выполнения чек-листов\n\n"
    response += f"📈 Общее количество задач: {total_tasks}\n\n"
    
    response += "📍 Распределение по точкам:\n"
    for point, count in points.items():
        response += f"  • {point}: {count} задач\n"
    
    response += "\n📅 Распределение по дням недели:\n"
    for day, count in days.items():
        response += f"  • {day}: {count} задач\n"
    
    response += "\n📈 Модуль отслеживания выполнения в активной разработке.\n"
    response += "Скоро можно будет просматривать статистику выполнения по сотрудникам и сменам."
    
    await update.message.reply_text(response)
    return CHECKLIST_MANAGEMENT_MENU

async def start_adding_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления новой задачи"""
    keyboard = [
        [KeyboardButton("ДЕ"), KeyboardButton("УЯ")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "➕ Добавление новой задачи\n\n"
        "Выберите точку:",
        reply_markup=reply_markup
    )
    return CHECKLIST_SELECT_POINT

async def select_point_for_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор точки для новой задачи"""
    point = update.message.text
    if point not in ['ДЕ', 'УЯ']:
        await update.message.reply_text("❌ Выберите точку: ДЕ или УЯ")
        return CHECKLIST_SELECT_POINT
    
    context.user_data['new_task_point'] = point
    
    keyboard = [
        [KeyboardButton("Понедельник"), KeyboardButton("Вторник"), KeyboardButton("Среда")],
        [KeyboardButton("Четверг"), KeyboardButton("Пятница"), KeyboardButton("Суббота")],
        [KeyboardButton("Воскресенье")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"Точка: {point}\n\n"
        "Выберите день недели:",
        reply_markup=reply_markup
    )
    return CHECKLIST_SELECT_DAY

async def select_day_for_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор дня недели для новой задачи"""
    day_map = {
        "Понедельник": 0,
        "Вторник": 1,
        "Среда": 2,
        "Четверг": 3,
        "Пятница": 4,
        "Саббота": 5,
        "Воскресенье": 6
    }
    
    day_name = update.message.text
    if day_name not in day_map:
        await update.message.reply_text("❌ Выберите день недели из списка")
        return CHECKLIST_SELECT_DAY
    
    context.user_data['new_task_day'] = day_map[day_name]
    
    keyboard = [
        [KeyboardButton("🌅 Утро"), KeyboardButton("🌆 Вечер")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"День: {day_name}\n\n"
        "Выберите тип смены:",
        reply_markup=reply_markup
    )
    return CHECKLIST_SELECT_SHIFT

async def select_shift_for_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор типа смены для новой задачи"""
    shift_map = {
        "🌅 Утро": "morning",
        "🌆 Вечер": "evening"
    }
    
    shift_name = update.message.text
    if shift_name not in shift_map:
        await update.message.reply_text("❌ Выберите тип смены из списка")
        return CHECKLIST_SELECT_SHIFT
    
    context.user_data['new_task_shift'] = shift_map[shift_name]
    
    await update.message.reply_text(
        f"Тип смены: {shift_name}\n\n"
        "Введите описание задачи:",
        reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)
    )
    return CHECKLIST_ADD_TASK_DESCRIPTION

async def add_task_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавление описания задачи и сохранение"""
    task_description = update.message.text
    
    try:
        # Импортируем функцию создания шаблона
        from bot.database.checklist_operations import create_checklist_template
        
        # Создаем задачу
        task = create_checklist_template(
            point=context.user_data['new_task_point'],
            day_of_week=context.user_data['new_task_day'],
            shift_type=context.user_data['new_task_shift'],
            task_description=task_description
        )
        
        day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        day_name = day_names[context.user_data['new_task_day']]
        shift_name = "🌅 Утро" if context.user_data['new_task_shift'] == 'morning' else "🌆 Вечер"
        
        await update.message.reply_text(
            f"✅ Задача успешно добавлена!\n\n"
            f"📍 Точка: {context.user_data['new_task_point']}\n"
            f"📅 День: {day_name}\n"
            f"🕒 Смена: {shift_name}\n"
            f"📝 Задача: {task_description}"
        )
        
        # Очищаем данные
        context.user_data.clear()
        
        return await templates_management(update, context)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при добавлении задачи: {str(e)}")
        return await templates_management(update, context)

async def view_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало просмотра задач с фильтрацией"""
    keyboard = [
        [KeyboardButton("ДЕ"), KeyboardButton("УЯ")],
        [KeyboardButton("Все точки")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "📋 Просмотр задач\n\n"
        "Выберите точку для фильтрации:",
        reply_markup=reply_markup
    )
    return CHECKLIST_VIEW_SELECT_POINT

async def view_select_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор точки для просмотра задач"""
    point = update.message.text
    context.user_data['view_point'] = point if point != "Все точки" else None
    
    keyboard = [
        [KeyboardButton("Понедельник"), KeyboardButton("Вторник"), KeyboardButton("Среда")],
        [KeyboardButton("Четверг"), KeyboardButton("Пятница"), KeyboardButton("Суббота")],
        [KeyboardButton("Воскресенье"), KeyboardButton("Все дни")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"📍 Точка: {point}\n\n"
        "Выберите день недели:",
        reply_markup=reply_markup
    )
    return CHECKLIST_VIEW_SELECT_DAY

async def view_select_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор дня недели и отображение задач"""
    day_map = {
        "Понедельник": 0, "Вторник": 1, "Среда": 2,
        "Четверг": 3, "Пятница": 4, "Суббота": 5, "Воскресенье": 6
    }
    
    day_name = update.message.text
    day = day_map[day_name] if day_name in day_map else None
    context.user_data['view_day'] = day
    
    # Получаем отфильтрованные задачи
    from bot.database.checklist_operations import get_checklist_templates
    point = context.user_data.get('view_point')
    
    templates = get_checklist_templates(point=point, day_of_week=day)
    
    if not templates:
        await update.message.reply_text(
            "📭 Задачи не найдены для выбранных фильтров."
        )
        return await view_templates(update, context)
    
    # Формируем список задач
    day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    shift_names = {"morning": "🌅 Утро", "evening": "🌆 Вечер"}
    
    response = "📋 Список задач:\n\n"
    
    # Группируем по точкам и сменам (если не выбрана конкретная точка)
    grouped = {}
    for template in templates:
        key = (template.point, template.shift_type) if not point else template.shift_type
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(template)
    
    for key, tasks in grouped.items():
        if point:
            # Если выбрана конкретная точка
            shift_name = shift_names.get(key, key)
            response += f"🕒 {shift_name}:\n"
        else:
            point_name, shift_type = key
            shift_name = shift_names.get(shift_type, shift_type)
            response += f"📍 {point_name} | {shift_name}:\n"
        
        for i, task in enumerate(tasks, 1):
            day_name = day_names[task.day_of_week] if day is None else day_name
            response += f"  {i}. {task.task_description}"
            if day is None:  # Показываем день только если не выбран конкретный
                response += f" ({day_names[task.day_of_week]})"
            response += "\n"
        response += "\n"
    
    # Создаем клавиатуру с действиями
    keyboard = [
        [KeyboardButton("✏️ Редактировать задачу"), KeyboardButton("🗑️ Удалить задачу")],
        [KeyboardButton("🔍 Новый поиск"), KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    # Сохраняем задачи в контексте для операций редактирования/удаления
    context.user_data['current_templates'] = templates
    
    await update.message.reply_text(
        response + "\nВыберите действие:",
        reply_markup=reply_markup
    )
    return CHECKLIST_VIEW_TASKS_LIST

async def edit_task_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор задачи для редактирования"""
    templates = context.user_data.get('current_templates', [])
    
    if not templates:
        await update.message.reply_text("❌ Нет задач для редактирования")
        return await view_templates(update, context)
    
    day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    shift_names = {"morning": "🌅 Утро", "evening": "🌆 Вечер"}
    
    response = "✏️ Выберите задачу для редактирования:\n\n"
    
    for i, task in enumerate(templates, 1):
        response += f"{i}. {task.task_description}\n"
        response += f"   📍 {task.point} | {day_names[task.day_of_week]} | {shift_names.get(task.shift_type, task.shift_type)}\n\n"
    
    await update.message.reply_text(
        response + "Введите номер задачи для редактирования:",
        reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)
    )
    return CHECKLIST_EDIT_TASK_SELECT

async def edit_task_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора номера задачи для редактирования"""
    try:
        task_number = int(update.message.text)
        templates = context.user_data.get('current_templates', [])
        
        if task_number < 1 or task_number > len(templates):
            await update.message.reply_text("❌ Неверный номер задачи")
            return CHECKLIST_EDIT_TASK_SELECT
        
        task = templates[task_number - 1]
        context.user_data['editing_task'] = task
        context.user_data['editing_task_id'] = task.id
        
        await update.message.reply_text(
            f"✏️ Редактирование задачи:\n"
            f"Текущее описание: {task.task_description}\n\n"
            f"Введите новое описание задачи:"
        )
        return CHECKLIST_EDIT_TASK_DESCRIPTION
        
    except ValueError:
        await update.message.reply_text("❌ Введите число")
        return CHECKLIST_EDIT_TASK_SELECT

async def edit_task_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранение отредактированного описания задачи"""
    new_description = update.message.text.strip()
    task_id = context.user_data.get('editing_task_id')
    
    if not task_id:
        await update.message.reply_text("❌ Ошибка: задача не найдена")
        return await templates_management(update, context)
    
    try:
        from bot.database.checklist_operations import update_checklist_template
        
        success = update_checklist_template(task_id, task_description=new_description)
        
        if success:
            await update.message.reply_text("✅ Задача успешно обновлена!")
        else:
            await update.message.reply_text("❌ Ошибка при обновлении задачи")
        
        # Очищаем контекст
        context.user_data.pop('editing_task', None)
        context.user_data.pop('editing_task_id', None)
        context.user_data.pop('current_templates', None)
        
        return await templates_management(update, context)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        return await templates_management(update, context)
    
async def delete_task_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор задачи для удаления"""
    templates = context.user_data.get('current_templates', [])
    
    if not templates:
        await update.message.reply_text("❌ Нет задач для удаления")
        return await view_templates(update, context)
    
    day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    shift_names = {"morning": "🌅 Утро", "evening": "🌆 Вечер"}
    
    response = "🗑️ Выберите задачу для удаления:\n\n"
    
    for i, task in enumerate(templates, 1):
        response += f"{i}. {task.task_description}\n"
        response += f"   📍 {task.point} | {day_names[task.day_of_week]} | {shift_names.get(task.shift_type, task.shift_type)}\n\n"
    
    await update.message.reply_text(
        response + "Введите номер задачи для удаления:",
        reply_markup=ReplyKeyboardMarkup([[]], resize_keyboard=True)
    )
    return CHECKLIST_DELETE_TASK_SELECT

async def delete_task_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора номера задачи для удаления"""
    try:
        task_number = int(update.message.text)
        templates = context.user_data.get('current_templates', [])
        
        if task_number < 1 or task_number > len(templates):
            await update.message.reply_text("❌ Неверный номер задачи")
            return CHECKLIST_DELETE_TASK_SELECT
        
        task = templates[task_number - 1]
        context.user_data['deleting_task'] = task
        context.user_data['deleting_task_id'] = task.id
        
        day_names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        shift_names = {"morning": "🌅 Утро", "evening": "🌆 Вечер"}
        
        await update.message.reply_text(
            f"🗑️ Удаление задачи:\n"
            f"📍 {task.point} | {day_names[task.day_of_week]} | {shift_names.get(task.shift_type, task.shift_type)}\n"
            f"📝 {task.task_description}\n\n"
            f"⚠️ Вы уверены, что хотите удалить эту задачу?\n"
            f"Введите 'ДА' для подтверждения или 'НЕТ' для отмены:"
        )
        return CHECKLIST_DELETE_TASK_CONFIRM
        
    except ValueError:
        await update.message.reply_text("❌ Введите число")
        return CHECKLIST_DELETE_TASK_SELECT

async def delete_task_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение удаления задачи"""
    confirmation = update.message.text.upper().strip()
    task_id = context.user_data.get('deleting_task_id')
    
    if confirmation in ['ДА', 'YES']:
        try:
            from bot.database.checklist_operations import delete_checklist_template
            
            success = delete_checklist_template(task_id)
            
            if success:
                await update.message.reply_text("✅ Задача успешно удалена!")
            else:
                await update.message.reply_text("❌ Ошибка при удалении задачи")
                
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    elif confirmation in ['НЕТ', 'NO']:
        await update.message.reply_text("❌ Удаление отменено")
    else:
        await update.message.reply_text("❌ Введите 'ДА' или 'НЕТ'")
        return CHECKLIST_DELETE_TASK_CONFIRM
    
    # Очищаем контекст
    context.user_data.pop('deleting_task', None)
    context.user_data.pop('deleting_task_id', None)
    context.user_data.pop('current_templates', None)
    
    return await templates_management(update, context)

async def emulation_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления эмуляцией - только для обычных сообщений"""
    await send_emulation_menu(update, context)
    return EMULATION_MENU

async def send_emulation_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int = None):
    """Универсальная функция для отправки меню эмуляции"""
    from bot.utils.emulation import is_emulation_mode, get_emulated_user
    
    if is_emulation_mode(context):
        emulated = get_emulated_user(context)
        
        keyboard = [
            [KeyboardButton("🔄 Замены от лица сотрудника")],
            [KeyboardButton("🔚 Завершить эмуляцию")],
            [KeyboardButton("⬅️ Назад")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        text = (f"🔁 Режим эмуляции\n\n"
                f"Текущий сотрудник: {emulated['name']}\n"
                f"Iiko ID: {emulated['iiko_id']}\n\n"
                f"Выберите действие:")
    else:
        keyboard = [
            [KeyboardButton("👥 Начать эмуляцию сотрудника")],
            [KeyboardButton("⬅️ Назад")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        text = ("🔁 Управление эмуляцией\n\n"
                "Эмуляция позволяет выполнять действия от лица других сотрудников.\n"
                "Выберите действие:")

    # Отправляем сообщение
    if chat_id:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text=text, 
            reply_markup=reply_markup
        )

async def start_emulation_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало выбора сотрудника для эмуляции"""
    users = get_all_users(active_only=True)
    users_with_iiko = [u for u in users if u.iiko_id]
    
    if not users_with_iiko:
        await update.message.reply_text("❌ Нет сотрудников с указанным iiko_id")
        return await emulation_management(update, context)
    
    keyboard = []
    text = "👥 Выберите сотрудника для эмуляции:\n\n"
    
    for user in users_with_iiko:
        text += f"• {user.name} (ID: {user.iiko_id})\n"
        keyboard.append([InlineKeyboardButton(
            user.name,
            callback_data=f"emulate_user_{user.iiko_id}"
        )])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_emulation")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, reply_markup=reply_markup)
    return EMULATING_USER

async def handle_emulation_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора сотрудника для эмуляции - для callback-запросов"""
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_emulation":
        await query.edit_message_text("❌ Эмуляция отменена")
        # Возвращаемся в главное меню настроек
        return await settings_menu(update, context)

    if query.data.startswith("emulate_user_"):
        iiko_id = query.data.split("_")[2]
        user = get_user_by_iiko_id(int(iiko_id))

        if not user:
            await query.edit_message_text("❌ Сотрудник не найден")
            return

        # Запускаем эмуляцию
        start_emulation(context, iiko_id, user.name)

        # Редактируем сообщение с инлайн-кнопками
        await query.edit_message_text(
            f"🔁 Эмуляция начата!\n\n"
            f"Теперь вы действуете от лица: {user.name}\n"
            f"Iiko ID: {iiko_id}\n\n"
            f"Все замены будут выполняться от имени этого сотрудника."
        )

        # Отправляем новое сообщение с меню эмуляции
        await send_emulation_menu(update, context, chat_id=query.message.chat_id)
        return EMULATION_MENU

async def stop_emulation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение эмуляции"""
    from bot.utils.emulation import is_emulation_mode, get_emulated_user, stop_emulation
    
    if is_emulation_mode(context):
        emulated = get_emulated_user(context)
        stop_emulation(context)
        
        await update.message.reply_text(
            f"🔚 Эмуляция завершена\n\n"
            f"Сотрудник: {emulated['name']}"
        )
    else:
        await update.message.reply_text("❌ Эмуляция не активна")
    
    # Возвращаемся в меню эмуляции
    return await emulation_management(update, context)

async def start_emulated_swap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск замен от лица эмулированного сотрудника"""
    from bot.utils.emulation import is_emulation_mode
    
    if not is_emulation_mode(context):
        await update.message.reply_text("❌ Сначала запустите эмуляцию сотрудника")
        return await emulation_management(update, context)
    
    # Запускаем обычный модуль замен - он автоматически подхватит эмулированного пользователя
    from bot.handlers.schedule import swap_menu
    return await swap_menu(update, context)

def get_settings_conversation_handler():
    """Возвращает ConversationHandler для настроек"""
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^⚙️ Настройки$"), settings_menu)
        ],
        states={
            SETTINGS_MENU: [
                MessageHandler(filters.Regex("^👥 Управление пользователями$"), users_management),
                MessageHandler(filters.Regex("^📋 Список пользователей$"), list_users),
                MessageHandler(filters.Regex("^➕ Добавить пользователя$"), start_adding_user),
                MessageHandler(filters.Regex("^📅 Управление расписанием$"), schedule_management),
                MessageHandler(filters.Regex("^🕒 Управление типами смен$"), shift_types_management),
                MessageHandler(filters.Regex("^📝 Управление чек-листами$"), checklist_management),
                MessageHandler(filters.Regex("^🗑️ Очистить таблицу оценок$"), clear_reviews_confirm),
                MessageHandler(filters.Regex("^🔁 Управление эмуляцией$"), emulation_management),
                MessageHandler(filters.Regex("^⬅️ Назад$"), cancel_conversation),
            ],
            EMULATION_MENU: [
                MessageHandler(filters.Regex("^👥 Начать эмуляцию сотрудника$"), start_emulation_selection),
                MessageHandler(filters.Regex("^🔄 Замены от лица сотрудника$"), start_emulated_swap),
                MessageHandler(filters.Regex("^🔚 Завершить эмуляцию$"), stop_emulation_handler),
                MessageHandler(filters.Regex("^⬅️ Назад$"), settings_menu),
            ],
            # Расписание
            SCHEDULE_MENU: [
                MessageHandler(filters.Regex("^🔄 Парсить текущий месяц$"), parse_current_month),
                MessageHandler(filters.Regex("^📅 Парсить следующий месяц$"), parse_next_month),
                MessageHandler(filters.Regex("^👥 Смены по сотрудникам$"), select_employee_for_shifts),
                MessageHandler(filters.Regex("^➕ Назначить смену вручную$"), start_adding_shift),
                MessageHandler(filters.Regex("^✏️ Изменить смену по ID$"), start_editing_shift),
                #MessageHandler(filters.Regex("^🔄 Замена от лица сотрудника$"), start_emulated_swap),
                MessageHandler(filters.Regex("^⬅️ Назад$"), settings_menu),
            ],
            SELECTING_EMPLOYEE_FOR_SHIFTS: [
                CallbackQueryHandler(handle_employee_shifts_callback, pattern="^(view_shifts_|cancel_schedule)"),
            ],
            ADDING_SHIFT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_shift_date)],
            ADDING_SHIFT_IIKO_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_shift_iiko_id)],
            ADDING_SHIFT_POINT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_shift_point)],
            ADDING_SHIFT_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_shift_type)],
            ADDING_SHIFT_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_shift_start)],
            ADDING_SHIFT_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_shift_end)],
            EDITING_SHIFT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_shift_id)],
            EDITING_SHIFT_MENU: [
                MessageHandler(filters.Regex("^(📅 Изменить дату|👤 Изменить сотрудника|📍 Изменить точку|🕒 Изменить тип|⏰ Изменить время|🗑️ Удалить смену|✅ Завершить редактирование)$"), editing_shift_menu),
            ],
            EDITING_SHIFT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_shift_date)],
            EDITING_SHIFT_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_shift_field)],
            SHIFT_TYPES_MENU: [
                MessageHandler(filters.Regex("^➕ Добавить тип смены$"), start_adding_shift_type),
                MessageHandler(filters.Regex("^📋 Список типов смен$"), list_shift_types),
                MessageHandler(filters.Regex("^✏️ Редактировать тип смены$"), start_editing_shift_type),
                MessageHandler(filters.Regex("^⬅️ Назад$"), settings_menu),
                CallbackQueryHandler(handle_shift_type_callback, pattern="^(edit_shift_type_|delete_shift_type_|back_to_shift_types_management)"),
            ],
            DELETING_SHIFT_TYPE_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, deleting_shift_type_confirm),
            ],
            EDITING_SHIFT_TYPE_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_shift_type_field),
            ],
            ADDING_SHIFT_TYPE_DATA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_shift_type_process),
            ],
            EDITING_SHIFT_TYPE_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_shift_type_id),
            ],
            EDITING_SHIFT_TYPE_FIELD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_shift_type_field),
            ],
            EDITING_SHIFT_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_shift_id),
                CommandHandler("cancel", cancel_editing),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel_editing),
            ],
            EDITING_SHIFT_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, editing_shift_menu),
                CommandHandler("cancel", cancel_editing),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel_editing),
            ],
            EDITING_SHIFT_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_shift_date),
                CommandHandler("cancel", cancel_editing),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel_editing),
            ],
            EDITING_SHIFT_IIKO_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_shift_iiko_id),
                CommandHandler("cancel", cancel_editing),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel_editing),
            ],
            EDITING_SHIFT_POINT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_shift_point),
                CommandHandler("cancel", cancel_editing),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel_editing),
            ],
            EDITING_SHIFT_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_shift_type),
                CommandHandler("cancel", cancel_editing),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel_editing),
            ],
            EDITING_SHIFT_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_shift_time),
                CommandHandler("cancel", cancel_editing),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel_editing),
            ],
            DELETING_SHIFT_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, deleting_shift_confirm),
                CommandHandler("cancel", cancel_editing),
                MessageHandler(filters.Regex("^❌ Отмена$"), cancel_editing),
            ],
            ADDING_USER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_user_name)],
            ADDING_USER_IIKO_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_user_iiko_id)],
            ADDING_USER_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_user_username)],
            ADDING_USER_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_user_role)],
            
            # Редактирование
            EDITING_USER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, editing_user_name)],
            EDITING_USER_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, editing_user_role)],
            EDITING_USER_IIKO_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, editing_user_iiko_id)],
            EDITING_USER_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, editing_user_username)],
            
            # Удаление
            DELETING_USER_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, deleting_user_confirm)],
            
            # Эмуляция
            EMULATION_MANAGEMENT: [
                MessageHandler(filters.Regex("^👥 Начать эмуляцию сотрудника$"), start_emulation_selection),
                MessageHandler(filters.Regex("^🔄 Замены от лица сотрудника$"), start_emulated_swap),
                MessageHandler(filters.Regex("^🔚 Завершить эмуляцию$"), stop_emulation_handler),
                MessageHandler(filters.Regex("^⬅️ Назад$"), settings_menu),  # Возврат в настройки
            ],
            EMULATING_USER: [
                CallbackQueryHandler(handle_emulation_selection, pattern="^(emulate_user_|cancel_emulation)"),
            ],
            
            # Управление чеклистами
            CHECKLIST_MANAGEMENT_MENU: [
                MessageHandler(filters.Regex("^📋 Управление шаблонами$"), templates_management),
                MessageHandler(filters.Regex("^➕ Добавить задачу$"), start_adding_task),  # ДОБАВИТЬ
                MessageHandler(filters.Regex("^📋 Просмотреть задачи$"), view_templates),  # ДОБАВИТЬ
                MessageHandler(filters.Regex("^🔄 Управление пересменами$"), hybrid_management),
                MessageHandler(filters.Regex("^🔄 Настроить распределение$"), start_hybrid_setup),
                MessageHandler(filters.Regex("^📋 Просмотреть распределения$"), hybrid_view_existing),
                MessageHandler(filters.Regex("^📊 Статистика выполнения$"), checklist_stats),
                MessageHandler(filters.Regex("^⬅️ Назад$"), settings_menu),
            ],
            # Cостояния для управления шаблонами
            CHECKLIST_SELECT_POINT: [
                MessageHandler(filters.Regex("^(ДЕ|УЯ)$"), select_point_for_task),
            ],
            CHECKLIST_SELECT_DAY: [
                MessageHandler(filters.Regex("^(Понедельник|Вторник|Среда|Четверг|Пятница|Суббота|Воскресенье)$"), select_day_for_task),
            ],
            CHECKLIST_SELECT_SHIFT: [
                MessageHandler(filters.Regex("^(🌅 Утро|🌆 Вечер)$"), select_shift_for_task),
            ],
            CHECKLIST_ADD_TASK_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_task_description),
            ],
            CHECKLIST_VIEW_SELECT_POINT: [
                MessageHandler(filters.Regex("^(ДЕ|УЯ|Все точки|⬅️ Назад)$"), view_select_point),
            ],
            CHECKLIST_VIEW_SELECT_DAY: [
                MessageHandler(filters.Regex("^(Понедельник|Вторник|Среда|Четверг|Пятница|Суббота|Воскресенье|Все дни|⬅️ Назад)$"), view_select_day),
            ],
            CHECKLIST_VIEW_TASKS_LIST: [
                MessageHandler(filters.Regex("^✏️ Редактировать задачу$"), edit_task_select),
                MessageHandler(filters.Regex("^🗑️ Удалить задачу$"), delete_task_select),
                MessageHandler(filters.Regex("^🔍 Новый поиск$"), view_templates),
                MessageHandler(filters.Regex("^⬅️ Назад$"), templates_management),
            ],
            CHECKLIST_EDIT_TASK_SELECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_task_number),
            ],
            CHECKLIST_EDIT_TASK_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_task_description),
            ],
            CHECKLIST_DELETE_TASK_SELECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, delete_task_number),
            ],
            CHECKLIST_DELETE_TASK_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, delete_task_confirm),
            ],
            HYBRID_SELECT_POINT: [
                MessageHandler(filters.Regex("^(ДЕ|УЯ|⬅️ Назад)$"), hybrid_select_point),
            ],
            HYBRID_SELECT_DAY: [
                MessageHandler(filters.Regex("^(Понедельник|Вторник|Среда|Четверг|Пятница|Суббота|Воскресенье|⬅️ Назад)$"), hybrid_select_day),
            ],
            HYBRID_VIEW_CURRENT: [
                MessageHandler(filters.Regex("^✅ Продолжить настройку$"), hybrid_view_current),
                MessageHandler(filters.Regex("^⬅️ Назад$"), start_hybrid_setup),
            ],
            HYBRID_SELECT_MORNING_TASK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, hybrid_select_morning_task),
            ],
            HYBRID_SELECT_EVENING_TASK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, hybrid_select_evening_task),
            ],
            HYBRID_SAVE_ASSIGNMENT: [
                MessageHandler(filters.Regex("^(✅ Сохранить|🔄 Начать заново|❌ Отмена)$"), hybrid_save_assignment),
            ],
            HYBRID_VIEW_EXISTING: [
                MessageHandler(filters.Regex("^✏️ Редактировать распределение$"), hybrid_edit_existing),
                MessageHandler(filters.Regex("^🗑️ Удалить распределение$"), hybrid_delete_existing),
                MessageHandler(filters.Regex("^⬅️ Назад$"), hybrid_management),
            ],
            HYBRID_EDIT_EXISTING: [
                MessageHandler(filters.Regex("^⬅️ Назад$"), hybrid_management),
                MessageHandler(filters.TEXT & ~filters.COMMAND, hybrid_edit_select),
                
            ],
            HYBRID_DELETE_EXISTING: [
                MessageHandler(filters.Regex("^⬅️ Назад$"), hybrid_management),
                MessageHandler(filters.TEXT & ~filters.COMMAND, hybrid_delete_select),
            ],
            HYBRID_DELETE_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, hybrid_delete_confirm),
            ],
            # Очистка таблицы
            CLEARING_REVIEWS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_clear_reviews)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CommandHandler("start", start_cancel_conversation),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_conversation),
            CallbackQueryHandler(handle_user_callback, pattern="^(edit_user_|delete_user_|back_to_users_management)"),
            CallbackQueryHandler(handle_shift_type_callback, pattern="^(edit_shift_type_|delete_shift_type_|back_to_shift_types_management)"),
        ],
        allow_reentry=True
    )