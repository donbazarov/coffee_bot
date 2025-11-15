"""Обработчики для меню настроек"""
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters, CommandHandler, CallbackQueryHandler
from bot.utils.auth import require_roles, ROLE_MENTOR, ROLE_SENIOR
from bot.database.user_operations import (
    get_all_users, create_user, update_user, delete_user, get_user_by_id
)
from bot.keyboards.menus import get_main_menu
import sqlite3
import os
from datetime import datetime

# Состояния для настроек
(SETTINGS_MENU, ADDING_USER_NAME, ADDING_USER_IIKO_ID, ADDING_USER_USERNAME, ADDING_USER_ROLE,
 EDITING_USER_NAME, EDITING_USER_ROLE, EDITING_USER_IIKO_ID, EDITING_USER_USERNAME,
 DELETING_USER_CONFIRM, CLEARING_REVIEWS) = range(11)

@require_roles([ROLE_MENTOR, ROLE_SENIOR])
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню настроек"""
    keyboard = [
        [KeyboardButton("👥 Управление пользователями")],
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
                MessageHandler(filters.Regex("^🗑️ Очистить таблицу оценок$"), clear_reviews_confirm),
                MessageHandler(filters.Regex("^⬅️ Назад$"), cancel_settings),
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
        ],
        allow_reentry=True
    )
