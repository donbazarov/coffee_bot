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
from bot.utils.google_sheets import get_current_month_name, get_next_month_name, parse_schedule_from_sheet
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
 # Cостояния для управления типами смен
 SHIFT_TYPES_MENU, ADDING_SHIFT_TYPE_DATA, EDITING_SHIFT_TYPE_ID, EDITING_SHIFT_TYPE_FIELD,
 DELETING_SHIFT_TYPE_CONFIRM, EDITING_SHIFT_TYPE_FIELD) = range(29)

@require_roles([ROLE_MENTOR, ROLE_SENIOR])
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню настроек"""
    keyboard = [
        [KeyboardButton("👥 Управление пользователями")],
        [KeyboardButton("📅 Управление расписанием")],
        [KeyboardButton("🕒 Управление типами смен")],
        [KeyboardButton("🗑️ Очистить таблицу оценок")],
        [KeyboardButton("⬅️ Назад")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "⚙️ Настройки\n\n"
        "Выберите раздел:",
        reply_markup=reply_markup
    )
    return SETTINGS_MENU

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
        return await schedule_management(update, context)
    
    if query.data.startswith("view_shifts_"):
        iiko_id = query.data.split("_")[2]
        user = get_user_by_iiko_id(int(iiko_id))
        
        if not user:
            await query.edit_message_text("❌ Сотрудник не найден")
            return await schedule_management(update, context)
        
        # Получаем смены на ближайшие 30 дней
        shifts = get_shifts_by_iiko_id(str(iiko_id), start_date=date.today(), end_date=date.today() + timedelta(days=30))
        
        if not shifts:
            await query.edit_message_text(f"📅 У {user.name} нет смен на ближайшие 30 дней")
            return await schedule_management(update, context)
        
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
        
        if text == f"📅 Смены сотрудника {user.name}:\n\n":
            await query.message.reply_text(f"📅 У {user.name} нет смен на ближайшие 30 дней")
        else:
            await query.message.reply_text(text)
        return await schedule_management(update, context)

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
    """Получение времени окончания и создание смены"""
    try:
        from datetime import time
        from bot.database.schedule_operations import get_shift_type_by_times
        
        time_str = update.message.text.strip()
        shift_end = datetime.strptime(time_str, "%H:%M").time()
        shift_start = context.user_data['new_shift_start']
        
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
        
        await update.message.reply_text(
            f"✅ Смена успешно создана!\n"
            f"ID: {shift.shift_id}"
        )
        
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
        "Введите ID смены:"
    )
    return EDITING_SHIFT_ID

async def edit_shift_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получение ID смены для редактирования"""
    try:
        shift_id = int(update.message.text.strip())
        shift = get_shift_by_id(shift_id)
        
        if not shift:
            await update.message.reply_text("❌ Смена с таким ID не найдена")
            return await schedule_management(update, context)
        
        context.user_data['editing_shift_id'] = shift_id
        
        text = f"Смена ID: {shift_id}\n"
        text += f"Дата: {shift.shift_date.strftime('%d.%m.%Y')}\n"
        text += f"Сотрудник: {shift.iiko_id}\n"
        if shift.shift_type_obj:
            text += f"Точка: {shift.shift_type_obj.point}\n"
            text += f"Тип: {shift.shift_type_obj.shift_type}\n"
            text += f"Время: {shift.shift_type_obj.start_time.strftime('%H:%M')} - {shift.shift_type_obj.end_time.strftime('%H:%M')}\n\n"
        else:
            text += "Тип смены не найден\n\n"
        text += "Введите новое значение или отправьте '-' чтобы пропустить поле:\n"
        text += "1. Дата (DD.MM.YYYY)\n"
        text += "2. Iiko ID\n"
        text += "3. Точка (ДЕ/УЯ)\n"
        text += "4. Тип (morning/hybrid/evening)\n"
        text += "5. Время начала (HH:MM)\n"
        text += "6. Время окончания (HH:MM)\n\n"
        text += "Введите номер поля для редактирования (1-6):"
        
        await update.message.reply_text(text)
        return EDITING_SHIFT_FIELD
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID. Введите число")
        return EDITING_SHIFT_ID

async def edit_shift_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирование поля смены"""
    # Это упрощенная версия - в реальности нужно сделать более сложную логику
    await update.message.reply_text(
        "⚠️ Функция редактирования смены в разработке.\n"
        "Используйте удаление и создание новой смены."
    )
    context.user_data.clear()
    return await schedule_management(update, context)

async def cancel_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена настроек"""
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Настройки отменены.",
        reply_markup=get_main_menu()
    )
    return ConversationHandler.END

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
                MessageHandler(filters.Regex("^🗑️ Очистить таблицу оценок$"), clear_reviews_confirm),
                MessageHandler(filters.Regex("^⬅️ Назад$"), cancel_settings),
            ],
            # Расписание
            SCHEDULE_MENU: [
                MessageHandler(filters.Regex("^🔄 Парсить текущий месяц$"), parse_current_month),
                MessageHandler(filters.Regex("^📅 Парсить следующий месяц$"), parse_next_month),
                MessageHandler(filters.Regex("^👥 Смены по сотрудникам$"), select_employee_for_shifts),
                MessageHandler(filters.Regex("^➕ Назначить смену вручную$"), start_adding_shift),
                MessageHandler(filters.Regex("^✏️ Изменить смену по ID$"), start_editing_shift),
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
            
            # Очистка таблицы
            CLEARING_REVIEWS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_clear_reviews)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_settings),
            CommandHandler("start", cancel_settings),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_settings),
            CallbackQueryHandler(handle_user_callback, pattern="^(edit_user_|delete_user_|back_to_users_management)"),
            CallbackQueryHandler(handle_shift_type_callback, pattern="^(edit_shift_type_|delete_shift_type_|back_to_shift_types_management)"),
        ],
        allow_reentry=True
    )
