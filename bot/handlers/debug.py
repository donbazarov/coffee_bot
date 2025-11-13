import sqlite3
import os
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

def get_recent_reviews(limit=10):
    """Получение последних записей из базы данных"""
    conn = sqlite3.connect('coffee_quality.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM drink_reviews 
        ORDER BY created_at DESC 
        LIMIT ?
    ''', (limit,))
    
    reviews = cursor.fetchall()
    conn.close()
    return reviews

def get_reviews_count():
    """Получение общего количества записей"""
    conn = sqlite3.connect('coffee_quality.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) FROM drink_reviews')
    count = cursor.fetchone()[0]
    
    conn.close()
    return count

async def show_db_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /show_db для отладки - показывает последние записи"""
    try:
        # Получаем последние 5 записей
        reviews = get_recent_reviews(5)
        total_count = get_reviews_count()
        
        if not reviews:
            await update.message.reply_text("📭 База данных пуста")
            return
        
        response = f"📊 Всего записей в базе: {total_count}\n\n"
        response += "Последние 5 записей:\n\n"
        
        for review in reviews:
            (id, respondent, barista, point, category, drink_type, 
             balance, bouquet, body, aftertaste, foam, latte_art, 
             photo_file_id, comment, created_at) = review
            
            response += f"🆔 ID: {id}\n"
            response += f"👤 Наставник: {respondent}\n"
            response += f"☕ Бариста: {barista}\n"
            response += f"🏪 Точка: {point}\n"
            response += f"📋 Категория: {category}\n"
            
            if drink_type:
                response += f"🍵 Напиток: {drink_type}\n"
            
            # Показываем оценки в зависимости от категории
            if category == "Эспрессо/Фильтр":
                if balance: response += f"⚖️ Баланс: {balance}/5\n"
                if bouquet: response += f"🌿 Букет: {bouquet}/5\n"
                if body: response += f"🏋️ Тело: {body}/5\n"
                if aftertaste: response += f"🎭 Послевкусие: {aftertaste}/5\n"
            else:  # Молочный напиток
                if balance: response += f"⚖️ Баланс: {balance}/5\n"
                if bouquet: response += f"🌿 Букет: {bouquet}/5\n"
                if foam: response += f"🥛 Пена: {foam}/5\n"
                if latte_art: response += f"🎨 Латте-арт: {latte_art}/5\n"
            
            # Информация о фото
            if photo_file_id:
                response += f"📷 Фото: есть (file_id: {photo_file_id[:20]}...)\n"
            else:
                response += "📷 Фото: нет\n"
            
            if comment and comment != '-':
                response += f"💬 Комментарий: {comment}\n"
            
            response += f"🕐 Дата: {created_at}\n"
            response += "─" * 30 + "\n\n"
        
        # Если сообщение слишком длинное, разбиваем на части
        if len(response) > 4000:
            parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
            for part in parts:
                await update.message.reply_text(part)
        else:
            await update.message.reply_text(response)
            
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при чтении базы данных: {str(e)}")

async def stats_debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats_debug - отладочная статистика"""
    try:
        reviews = get_recent_reviews(20)  # Последние 20 записей для статистики
        total_count = get_reviews_count()
        
        if not reviews:
            await update.message.reply_text("📭 База данных пуста")
            return
        
        # Простая статистика
        barista_stats = {}
        category_stats = {"Эспрессо/Фильтр": 0, "Молочный напиток": 0}
        
        for review in reviews:
            barista = review[2]  # barista_name
            category = review[4]  # category
            
            if barista not in barista_stats:
                barista_stats[barista] = 0
            barista_stats[barista] += 1
            
            if category in category_stats:
                category_stats[category] += 1
        
        response = f"📈 Отладочная статистика\n\n"
        response += f"📊 Всего оценок: {total_count}\n\n"
        
        response += "👥 Оценки по бариста:\n"
        for barista, count in barista_stats.items():
            response += f"• {barista}: {count} оценок\n"
        
        response += f"\n☕ Распределение по категориям:\n"
        response += f"• Эспрессо/Фильтр: {category_stats['Эспрессо/Фильтр']}\n"
        response += f"• Молочные напитки: {category_stats['Молочный напиток']}\n"
        
        await update.message.reply_text(response)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка статистики: {str(e)}")

async def show_photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /show_photo [id] - показать фото по ID записи через file_id"""
    try:
        if not context.args:
            await update.message.reply_text("❌ Укажите ID записи: /show_photo [id]")
            return
        
        record_id = context.args[0]
        
        conn = sqlite3.connect('coffee_quality.db')
        cursor = conn.cursor()
        cursor.execute('SELECT photo_file_id FROM drink_reviews WHERE id = ?', (record_id,))
        result = cursor.fetchone()
        conn.close()
        
        if not result or not result[0]:
            await update.message.reply_text(f"❌ Для записи {record_id} фото не найдено")
            return
        
        photo_file_id = result[0]
        
        # Отправляем фото используя file_id
        await update.message.reply_photo(
            photo=photo_file_id,
            caption=f"📷 Фото для записи {record_id}"
        )
            
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

def get_debug_handlers():
    """Возвращает обработчики для отладки"""
    return [
        CommandHandler("show_db", show_db_command),
        CommandHandler("stats_debug", stats_debug_command),
        CommandHandler("show_photo", show_photo_command)
    ]