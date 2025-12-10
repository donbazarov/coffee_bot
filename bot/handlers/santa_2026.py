# santa_2026.py
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes, MessageHandler, filters, ConversationHandler
from bot.keyboards.menus import get_santa_menu, get_other_menu
from bot.utils.auth import is_senior_or_mentor
from datetime import datetime
import random
import logging
import sqlite3

logger = logging.getLogger(__name__)

# Состояния для вишлиста
WISHLIST_INPUT = 1

def get_db_connection():
    """Получить соединение с базой данных"""
    return sqlite3.connect('coffee_quality.db')

async def santa_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню тайного санты"""
    await update.message.reply_text(
        "🎅 Тайный Санта 2026!\n\n"
        "Участвуйте в праздничном обмене подарками!",
        reply_markup=get_santa_menu()
    )

async def handle_santa_participation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка участия в тайном санте"""
    user = update.effective_user
    text = update.message.text
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Находим или создаем запись пользователя
        cursor.execute(
            "SELECT * FROM secret_santa_2026 WHERE telegram_username = ?", 
            (user.username,)
        )
        santa_record = cursor.fetchone()
        
        if not santa_record:
            # Создаем новую запись
            cursor.execute(
                "INSERT INTO secret_santa_2026 (telegram_username, is_participant) VALUES (?, ?)",
                (user.username, 1 if text == "✅ Участвую" else 0)
            )
        else:
            # Обновляем существующую запись
            cursor.execute(
                "UPDATE secret_santa_2026 SET is_participant = ?, updated_at = CURRENT_TIMESTAMP WHERE telegram_username = ?",
                (1 if text == "✅ Участвую" else 0, user.username)
            )
            
            # Если выходит из участия - сбрасываем назначение
            if text == "❌ Не участвую":
                cursor.execute(
                    "UPDATE secret_santa_2026 SET santa_of = '' WHERE telegram_username = ?",
                    (user.username,)
                )
        
        conn.commit()
        
        if text == "✅ Участвую":
            message = "🎉 Вы теперь участвуете в Тайном Санте 2026!"
        else:
            message = "😢 Вы больше не участвуете в Тайном Санте."
        
        await update.message.reply_text(message, reply_markup=get_santa_menu())
        
    except Exception as e:
        logger.error(f"Ошибка при обновлении участия в санте: {e}")
        conn.rollback()
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
    finally:
        conn.close()

async def handle_wishlist_simple(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Простой обработчик вишлиста"""
    user = update.effective_user
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            "SELECT wishlist FROM secret_santa_2026 WHERE telegram_username = ?", 
            (user.username,)
        )
        result = cursor.fetchone()
        
        if result and result[0]:
            # Показываем текущий вишлист и предлагаем обновить
            keyboard = [
                [KeyboardButton("🔄 Обновить вишлист")],
                [KeyboardButton("❌ Отмена")]
            ]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            
            await update.message.reply_text(
                f"📝 Ваш текущий вишлист:\n{result[0]}\n\n"
                f"Хотите обновить?",
                reply_markup=reply_markup
            )
            
            # Сохраняем состояние для следующего шага
            context.user_data['awaiting_wishlist'] = True
            
        else:
            await update.message.reply_text(
                "📝 Напишите ваш вишлист - что бы вы хотели получить в подарок?\n\n"
                "Можно написать несколько вариантов или общие пожелания."
            )
            context.user_data['awaiting_wishlist'] = True
            
    except Exception as e:
        logger.error(f"Ошибка при работе с вишлистом: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
    finally:
        conn.close()

async def handle_wishlist_update(update: Update, context):
    """Обработка обновления вишлиста"""
    user = update.effective_user
    wishlist_text = update.message.text
    
    # Проверяем, нажата ли кнопка отмены
    if wishlist_text == "❌ Отмена":
        context.user_data['awaiting_wishlist'] = False
        await update.message.reply_text("❌ Отменено", reply_markup=get_santa_menu())
        return
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Находим или создаем запись
        cursor.execute(
            "SELECT * FROM secret_santa_2026 WHERE telegram_username = ?", 
            (user.username,)
        )
        santa_record = cursor.fetchone()
        
        if not santa_record:
            cursor.execute(
                "INSERT INTO secret_santa_2026 (telegram_username, wishlist, is_participant) VALUES (?, ?, ?)",
                (user.username, wishlist_text, 1)
            )
        else:
            cursor.execute(
                "UPDATE secret_santa_2026 SET wishlist = ?, is_participant = 1, updated_at = CURRENT_TIMESTAMP WHERE telegram_username = ?",
                (wishlist_text, user.username)
            )
        
        conn.commit()
        context.user_data['awaiting_wishlist'] = False
        await update.message.reply_text(
            "✅ Ваш вишлист сохранен!",
            reply_markup=get_santa_menu()
        )
        
    except Exception as e:
        logger.error(f"Ошибка при сохранении вишлиста: {e}")
        conn.rollback()
        await update.message.reply_text("❌ Произошла ошибка при сохранении.")
    finally:
        conn.close()


async def handle_santa_assignment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показ назначения санты"""
    user = update.effective_user
    current_date = datetime.now()
    
    # Проверяем, наступило ли 1 декабря 2025
    if current_date < datetime(2025, 12, 1):
        await update.message.reply_text(
            "🎅 Игра еще не началась, приходи в декабре 2025!",
            reply_markup=get_santa_menu()
        )
        return
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Проверяем, участвует ли пользователь
        cursor.execute(
            "SELECT is_participant, santa_of FROM secret_santa_2026 WHERE telegram_username = ?", 
            (user.username,)
        )
        result = cursor.fetchone()
        
        if not result or not result[0]:  # is_participant
            await update.message.reply_text(
                "❌ Вы не участвуете в Тайном Санте.",
                reply_markup=get_santa_menu()
            )
            return
        
        if not result[1]:  # santa_of
            await update.message.reply_text(
                "❌ Распределение еще не завершено. Ожидайте назначения.",
                reply_markup=get_santa_menu()
            )
            return
        
        # Получаем информацию о назначенном участнике
        cursor.execute(
            "SELECT wishlist FROM secret_santa_2026 WHERE telegram_username = ?", 
            (result[1],)  # santa_of
        )
        target_result = cursor.fetchone()
        
        if not target_result:
            await update.message.reply_text(
                "❌ Ошибка: назначенный участник не найден.",
                reply_markup=get_santa_menu()
            )
            return
        
        message = f"🎅 Вы — Тайный Санта для @{result[1]}!\n\n"
        
        if target_result[0]:  # wishlist
            message += f"📝 У Вашего подопечного есть вишлист, но мы Вам его не покажем. Удачи!"
        else:
            message += "❌ Ваш подопечный еще не указал вишлист. Возможно, он сам не знает, чего хочет."
        
        await update.message.reply_text(message, reply_markup=get_santa_menu())
        
    except Exception as e:
        logger.error(f"Ошибка при показе назначения санты: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
    finally:
        conn.close()

async def assign_secret_santas():
    """Функция распределения тайных сант"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Получаем всех участников
        cursor.execute(
            "SELECT telegram_username FROM secret_santa_2026 WHERE is_participant = 1"
        )
        participants = [row[0] for row in cursor.fetchall()]
        
        if len(participants) < 2:
            logger.error("Недостаточно участников для распределения сант")
            return False
        
        logger.info(f"Участники для распределения: {participants}")
        
        # Создаем копию списка для получателей
        receivers = participants.copy()
        random.shuffle(receivers)
        
        # Распределяем с проверкой, чтобы никто не стал сантой самому себе
        assignments = {}
        remaining_givers = participants.copy()
        remaining_receivers = receivers.copy()
        
        for giver in participants:
            # Убираем текущего дарителя из списка получателей
            available_receivers = [r for r in remaining_receivers if r != giver]
            
            if not available_receivers:
                # Если остался только сам даритель - это ошибка логики
                logger.error("Невозможно распределить сант - конфликт назначений")
                return False
            
            # Выбираем случайного получателя
            receiver = random.choice(available_receivers)
            assignments[giver] = receiver
            
            # Убираем выбранного получателя из доступных
            remaining_receivers.remove(receiver)
        
        # Сохраняем распределение в базу
        for giver_username, receiver_username in assignments.items():
            cursor.execute(
                "UPDATE secret_santa_2026 SET santa_of = ?, updated_at = CURRENT_TIMESTAMP WHERE telegram_username = ?",
                (receiver_username, giver_username)
            )
        
        conn.commit()
        logger.info(f"Успешно распределено {len(assignments)} сант")
        
        # Логируем распределение для отладки
        for giver, receiver in assignments.items():
            logger.info(f"🎅 {giver} -> {receiver}")
        
        return True
        
    except Exception as e:
        logger.error(f"Ошибка при распределении сант: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

async def santa_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /santastart - запуск распределения сант"""
    user = update.effective_user
    
    # Проверяем права доступа (только администраторы/организаторы)
    if not is_senior_or_mentor(update):
        await update.message.reply_text("❌ У вас нет прав для выполнения этой команды.")
        return
    
    await update.message.reply_text("🔄 Запускаю распределение тайных сант...")
    
    success = await assign_secret_santas()
    
    if success:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            # Получаем статистику
            cursor.execute("SELECT COUNT(*) FROM secret_santa_2026 WHERE is_participant = 1")
            participants_count = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM secret_santa_2026 WHERE is_participant = 1 AND santa_of != ''")
            assigned_count = cursor.fetchone()[0]
            
            await update.message.reply_text(
                f"✅ Распределение тайных сант завершено!\n\n"
                f"• Участников: {participants_count}\n"
                f"• Распределено: {assigned_count}\n"
                f"• Санта может узнать своего подопечного через меню 'Чей я Санта'"
            )
        except Exception as e:
            logger.error(f"Ошибка при получении статистики: {e}")
            await update.message.reply_text("✅ Распределение завершено!")
        finally:
            conn.close()
    else:
        await update.message.reply_text("❌ Ошибка при распределении сант. Проверьте логи.")

async def santa_clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /santaclear - очистка распределения сант"""
    user = update.effective_user
    
    # Проверяем права доступа
    if not is_senior_or_mentor(update):
        await update.message.reply_text("❌ У вас нет прав для выполнения этой команды.")
        return
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Сбрасываем все назначения сант
        cursor.execute("UPDATE secret_santa_2026 SET santa_of = '', updated_at = CURRENT_TIMESTAMP")
        
        # Получаем статистику участников
        cursor.execute("SELECT COUNT(*) FROM secret_santa_2026 WHERE is_participant = 1")
        participants_count = cursor.fetchone()[0]
        
        conn.commit()
        
        await update.message.reply_text(
            f"✅ Распределение сант очищено!\n\n"
            f"• Участников: {participants_count}\n"
            f"• Все назначения сброшены\n"
            f"• Для нового распределения используйте /santastart"
        )
        
    except Exception as e:
        logger.error(f"Ошибка при очистке распределения: {e}")
        conn.rollback()
        await update.message.reply_text("❌ Ошибка при очистке распределения.")
    finally:
        conn.close()
