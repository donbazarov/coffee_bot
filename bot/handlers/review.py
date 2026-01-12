from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters, CommandHandler
from bot.config import BotConfig
from bot.database.simple_db import save_review
from bot.database.user_operations import get_users_by_role
from bot.keyboards.menus import get_main_menu
from bot.utils.auth import is_senior_or_mentor
from bot.utils.common_handlers import cancel_conversation, start_cancel_conversation

# Состояния - расширяем для детальной оценки
(SELECTING_BARISTA, SELECTING_POINT, SELECTING_CATEGORY,
 ESPRESSO_DRINK_TYPE, ESPRESSO_BALANCE, ESPRESSO_BOUQUET, ESPRESSO_BODY, ESPRESSO_AFTERTASTE, ESPRESSO_COMMENT,
 MILK_DRINK_TYPE, MILK_BALANCE, MILK_BOUQUET, MILK_FOAM, MILK_LATTE_ART, MILK_PHOTO, MILK_COMMENT) = range(16)

# Клавиатуры для оценок 1-5 с кнопками назад и отмены
BACK_BUTTON = "⬅️ Назад"
rating_keyboard = [[str(i)] for i in range(1, 6)]
rating_keyboard.append([BACK_BUTTON])
rating_keyboard.append(["❌ Отмена"])
rating_markup = ReplyKeyboardMarkup(rating_keyboard, resize_keyboard=True)

async def prompt_point_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать выбор точки."""
    barista_name = context.user_data.get('barista', 'Не выбран')
    keyboard = [[point] for point in BotConfig.points]
    keyboard.append([BACK_BUTTON])
    keyboard.append(["❌ Отмена"])
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"✅ Бариста: {barista_name}\n\nТеперь выберите точку:",
        reply_markup=reply_markup
    )
    return SELECTING_POINT

async def prompt_category_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать выбор категории напитка."""
    point = context.user_data.get('point', 'Не выбрана')
    keyboard = [["Эспрессо/Фильтр"], ["Молочный напиток"]]
    keyboard.append([BACK_BUTTON])
    keyboard.append(["❌ Отмена"])
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        f"✅ Точка: {point}\n\nВыберите категорию напитка:",
        reply_markup=reply_markup
    )
    return SELECTING_CATEGORY

async def prompt_espresso_drink_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать выбор типа эспрессо/фильтра."""
    keyboard = [["Эспрессо", "Фильтр", "Альт."], [BACK_BUTTON], ["❌ Отмена"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "Выберите тип напитка:",
        reply_markup=reply_markup
    )
    return ESPRESSO_DRINK_TYPE

async def prompt_espresso_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать оценку баланса эспрессо/фильтра."""
    drink_type = context.user_data.get('drink_type', '')
    await update.message.reply_text(
        f"☕ {drink_type}\n\nОцените баланс вкуса (1-5):\n"
        "1 - Несбалансированный\n5 - Идеально сбалансированный",
        reply_markup=rating_markup
    )
    return ESPRESSO_BALANCE

async def prompt_espresso_bouquet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать оценку букета эспрессо/фильтра."""
    await update.message.reply_text(
        "Оцените качество букета (1-5):\n"
        "1 - Низкое качество \n5 - Высокое качество",
        reply_markup=rating_markup
    )
    return ESPRESSO_BOUQUET

async def prompt_espresso_body(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать оценку тела эспрессо/фильтра."""
    await update.message.reply_text(
        "Оцените качество тела напитка (1-5):\n"
        "1 - Низкое качество \n5 - Высокое качество",
        reply_markup=rating_markup
    )
    return ESPRESSO_BODY

async def prompt_espresso_aftertaste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать оценку послевкусия эспрессо/фильтра."""
    await update.message.reply_text(
        "Оцените качество послевкусия (1-5):\n"
        "1 - Низкое качество \n5 - Высокое качество",
        reply_markup=rating_markup
    )
    return ESPRESSO_AFTERTASTE

async def prompt_espresso_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать ввод комментария для эспрессо/фильтра."""
    cancel_keyboard = [["-"], [BACK_BUTTON], ["❌ Отмена"]]
    await update.message.reply_text(
        "Добавьте комментарий (или напишите '-' если комментарий не нужен):",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True)
    )
    return ESPRESSO_COMMENT

async def prompt_milk_drink_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать выбор типа молочного напитка."""
    keyboard = [["Капучино", "Флэт Уайт"], [BACK_BUTTON], ["❌ Отмена"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "Выберите тип молочного напитка:",
        reply_markup=reply_markup
    )
    return MILK_DRINK_TYPE

async def prompt_milk_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать оценку баланса молочного напитка."""
    drink_type = context.user_data.get('drink_type', '')
    await update.message.reply_text(
        f"🥛 {drink_type}\n\nОцените баланс вкуса (1-5):\n"
        "1 - Несбалансированный\n5 - Идеально сбалансированный",
        reply_markup=rating_markup
    )
    return MILK_BALANCE

async def prompt_milk_bouquet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать оценку букета молочного напитка."""
    await update.message.reply_text(
        "Оцените качество букета (1-5):\n"
        "1 - Низкое качество \n5 - Высокое качество",
        reply_markup=rating_markup
    )
    return MILK_BOUQUET

async def prompt_milk_foam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать оценку пены молочного напитка."""
    await update.message.reply_text(
        "Оцените качество пены (1-5):\n"
        "1 - Низкое качество \n5 - Высокое качество",
        reply_markup=rating_markup
    )
    return MILK_FOAM

async def prompt_milk_latte_art(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать оценку латте-арта."""
    await update.message.reply_text(
        "Оцените латте-арт (1-5):\n"
        "1 - Низкое качество \n5 - Высокое качество",
        reply_markup=rating_markup
    )
    return MILK_LATTE_ART

async def prompt_milk_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать запрос фото молочного напитка."""
    cancel_keyboard = [["-"], [BACK_BUTTON], ["❌ Отмена"]]
    await update.message.reply_text(
        "Добавьте фото напитка (или отправьте '-' чтобы пропустить):",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True)
    )
    return MILK_PHOTO

async def prompt_milk_comment(update: Update, context: ContextTypes.DEFAULT_TYPE, with_success: bool = False):
    """Показать ввод комментария для молочного напитка."""
    cancel_keyboard = [["-"], [BACK_BUTTON], ["❌ Отмена"]]
    text = "Добавьте комментарий (или напишите '-' если комментарий не нужен):"
    if with_success:
        text = "✅ Фото получено!\n\n" + text
    await update.message.reply_text(
        text,
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True)
    )
    return MILK_COMMENT

async def start_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало оценки напитка"""
    # Проверяем доступ - наставники и старшие могут оценивать
    if not is_senior_or_mentor(update):
        await update.message.reply_text(
            "❌ У вас нет доступа к этой функции.\n"
            "Оценивать напитки могут только наставники и старшие."
        )
        return ConversationHandler.END
    
    context.user_data.clear()
    
    # Получаем бариста из БД
    barista_users = get_users_by_role('barista', active_only=True)
    baristas = [barista.name for barista in barista_users]
    
    # Если в БД нет бариста, используем config.py как fallback
    if not baristas:
        baristas = [barista['name'] for barista in BotConfig.baristas]
    
    keyboard = [[barista] for barista in baristas]
    keyboard.append([BACK_BUTTON])
    keyboard.append(["❌ Отмена"])
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "Выберите бариста для оценки:",
        reply_markup=reply_markup
    )
    return SELECTING_BARISTA

async def select_barista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор бариста"""
    barista_name = update.message.text
    
    if barista_name == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if barista_name == BACK_BUTTON:
        return await cancel_conversation(update, context)
    
    # Получаем бариста из БД
    barista_users = get_users_by_role('barista', active_only=True)
    barista_names = [barista.name for barista in barista_users]
    
    # Если в БД нет бариста, используем config.py как fallback
    if not barista_names:
        barista_names = [barista['name'] for barista in BotConfig.baristas]
    
    if barista_name not in barista_names:
        await update.message.reply_text("❌ Пожалуйста, выберите бариста из списка:")
        return SELECTING_BARISTA
    
    context.user_data['barista'] = barista_name
    
    return await prompt_point_selection(update, context)

async def select_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор точки"""
    point = update.message.text
    
    if point == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if point == BACK_BUTTON:
        return await start_review(update, context)
    
    if point not in BotConfig.points:
        await update.message.reply_text("❌ Пожалуйста, выберите точку из списка:")
        return SELECTING_POINT
    
    context.user_data['point'] = point
    
    return await prompt_category_selection(update, context)

async def select_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор категории напитка - переход к детальной оценке"""
    category = update.message.text
    
    if category == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if category == BACK_BUTTON:
        return await prompt_point_selection(update, context)
    
    valid_categories = ["Эспрессо/Фильтр", "Молочный напиток"]
    if category not in valid_categories:
        await update.message.reply_text("❌ Пожалуйста, выберите категорию из списка:")
        return SELECTING_CATEGORY
    
    context.user_data['category'] = category
    
    # Переходим к соответствующей ветке оценки
    if category == "Эспрессо/Фильтр":
        return await start_espresso_evaluation(update, context)
    else:  # Молочный напиток
        return await start_milk_evaluation(update, context)

async def start_espresso_evaluation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало оценки эспрессо/фильтра"""
    return await prompt_espresso_drink_type(update, context)

async def select_espresso_drink_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор типа эспрессо/фильтра"""
    drink_type = update.message.text
    
    if drink_type == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if drink_type == BACK_BUTTON:
        return await prompt_category_selection(update, context)
    
    valid_types = ["Эспрессо", "Фильтр", "Альт."]
    if drink_type not in valid_types:
        await update.message.reply_text("❌ Пожалуйста, выберите тип напитка из списка:")
        return ESPRESSO_DRINK_TYPE
    
    context.user_data['drink_type'] = drink_type
    
    return await prompt_espresso_balance(update, context)

async def select_espresso_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оценка баланса эспрессо/фильтра"""
    balance = update.message.text
    
    if balance == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if balance == BACK_BUTTON:
        return await prompt_espresso_drink_type(update, context)
    
    if balance not in ['1', '2', '3', '4', '5']:
        await update.message.reply_text("❌ Пожалуйста, выберите оценку от 1 до 5:")
        return ESPRESSO_BALANCE
    
    context.user_data['balance'] = int(balance)
    
    return await prompt_espresso_bouquet(update, context)

async def select_espresso_bouquet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оценка букета эспрессо/фильтра"""
    bouquet = update.message.text
    
    if bouquet == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if bouquet == BACK_BUTTON:
        return await prompt_espresso_balance(update, context)
    
    if bouquet not in ['1', '2', '3', '4', '5']:
        await update.message.reply_text("❌ Пожалуйста, выберите оценку от 1 до 5:")
        return ESPRESSO_BOUQUET
    
    context.user_data['bouquet'] = int(bouquet)
    
    return await prompt_espresso_body(update, context)

async def select_espresso_body(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оценка тела эспрессо/фильтра"""
    body = update.message.text
    
    if body == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if body == BACK_BUTTON:
        return await prompt_espresso_bouquet(update, context)
    
    if body not in ['1', '2', '3', '4', '5']:
        await update.message.reply_text("❌ Пожалуйста, выберите оценку от 1 до 5:")
        return ESPRESSO_BODY
    
    context.user_data['body'] = int(body)
    
    return await prompt_espresso_aftertaste(update, context)

async def select_espresso_aftertaste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оценка послевкусия эспрессо/фильтра"""
    aftertaste = update.message.text
    
    if aftertaste == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if aftertaste == BACK_BUTTON:
        return await prompt_espresso_body(update, context)
    
    if aftertaste not in ['1', '2', '3', '4', '5']:
        await update.message.reply_text("❌ Пожалуйста, выберите оценку от 1 до 5:")
        return ESPRESSO_AFTERTASTE
    
    context.user_data['aftertaste'] = int(aftertaste)
    
    return await prompt_espresso_comment(update, context)

async def select_espresso_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Комментарий для эспрессо/фильтра"""
    comment = update.message.text
    
    if comment == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if comment == BACK_BUTTON:
        return await prompt_espresso_aftertaste(update, context)
    
    context.user_data['comment'] = comment
    
    # Сохраняем оценку
    await save_review_data(update, context)
    return ConversationHandler.END

async def start_milk_evaluation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало оценки молочного напитка"""
    return await prompt_milk_drink_type(update, context)

async def select_milk_drink_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор типа молочного напитка"""
    drink_type = update.message.text
    
    if drink_type == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if drink_type == BACK_BUTTON:
        return await prompt_category_selection(update, context)
    
    valid_types = ["Капучино", "Флэт Уайт"]
    if drink_type not in valid_types:
        await update.message.reply_text("❌ Пожалуйста, выберите тип напитка из списка:")
        return MILK_DRINK_TYPE
    
    context.user_data['drink_type'] = drink_type
    
    return await prompt_milk_balance(update, context)

async def select_milk_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оценка баланса молочного напитка"""
    balance = update.message.text
    
    if balance == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if balance == BACK_BUTTON:
        return await prompt_milk_drink_type(update, context)
    
    if balance not in ['1', '2', '3', '4', '5']:
        await update.message.reply_text("❌ Пожалуйста, выберите оценку от 1 до 5:")
        return MILK_BALANCE
    
    context.user_data['balance'] = int(balance)
    
    return await prompt_milk_bouquet(update, context)

async def select_milk_bouquet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оценка букета молочного напитка"""
    bouquet = update.message.text
    
    if bouquet == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if bouquet == BACK_BUTTON:
        return await prompt_milk_balance(update, context)
    
    if bouquet not in ['1', '2', '3', '4', '5']:
        await update.message.reply_text("❌ Пожалуйста, выберите оценку от 1 до 5:")
        return MILK_BOUQUET
    
    context.user_data['bouquet'] = int(bouquet)
    
    return await prompt_milk_foam(update, context)

async def select_milk_foam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оценка пены молочного напитка"""
    foam = update.message.text
    
    if foam == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if foam == BACK_BUTTON:
        return await prompt_milk_bouquet(update, context)
    
    if foam not in ['1', '2', '3', '4', '5']:
        await update.message.reply_text("❌ Пожалуйста, выберите оценку от 1 до 5:")
        return MILK_FOAM
    
    context.user_data['foam'] = int(foam)
    
    return await prompt_milk_latte_art(update, context)

async def select_milk_latte_art(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Оценка латте-арта"""
    latte_art = update.message.text
    
    if latte_art == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if latte_art == BACK_BUTTON:
        return await prompt_milk_foam(update, context)
    
    if latte_art not in ['1', '2', '3', '4', '5']:
        await update.message.reply_text("❌ Пожалуйста, выберите оценку от 1 до 5:")
        return MILK_LATTE_ART
    
    context.user_data['latte_art'] = int(latte_art)
    
    return await prompt_milk_photo(update, context)

async def select_milk_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка фото молочного напитка через Telegram file_id"""
    if update.message.text == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if update.message.text == BACK_BUTTON:
        return await prompt_milk_latte_art(update, context)
    
    if update.message.text == "-":
        context.user_data['photo_file_id'] = None
        return await prompt_milk_comment(update, context)
    elif update.message.photo:
        try:
            # Получаем file_id самого большого фото (последний элемент в списке)
            photo_file_id = update.message.photo[-1].file_id
            
            # Сохраняем file_id
            context.user_data['photo_file_id'] = photo_file_id
            
            return await prompt_milk_comment(update, context, with_success=True)
            
        except Exception as e:
            cancel_keyboard = [["-"], [BACK_BUTTON], ["❌ Отмена"]]
            await update.message.reply_text(
                f"❌ Ошибка при обработке фото: {str(e)}\n\n"
                "Попробуйте отправить фото еще раз или отправьте '-' чтобы пропустить:",
                reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True)
            )
            return MILK_PHOTO
    else:
        cancel_keyboard = [["-"], [BACK_BUTTON], ["❌ Отмена"]]
        await update.message.reply_text(
            "❌ Пожалуйста, отправьте фото или '-' чтобы пропустить:",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True)
        )
        return MILK_PHOTO

async def select_milk_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Комментарий для молочного напитка"""
    comment = update.message.text
    
    if comment == "❌ Отмена":
        return await cancel_conversation(update, context)
    
    if comment == BACK_BUTTON:
        return await prompt_milk_photo(update, context)
    
    context.user_data['comment'] = comment
    
    # Сохраняем оценку
    await save_review_data(update, context)
    return ConversationHandler.END

async def save_review_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранение полной оценки в базу данных"""
    data = context.user_data
    respondent_name = update.effective_user.first_name
    
    # Формируем данные для сохранения
    review_data = {
        'respondent_name': respondent_name,
        'barista_name': data['barista'],
        'point': data['point'],
        'category': data['category'],
        'drink_type': data.get('drink_type'),
        'balance': data.get('balance'),
        'bouquet': data.get('bouquet'),
        'body': data.get('body'),
        'aftertaste': data.get('aftertaste'),
        'foam': data.get('foam'),
        'latte_art': data.get('latte_art'),
        'photo_file_id': data.get('photo_file_id'),  # 🆕 Сохраняем file_id вместо пути
        'comment': data.get('comment', '-')
    }
    
    save_review(review_data)
    
    # Формируем красивый отчет
    if data['category'] == "Эспрессо/Фильтр":
        report = f"""
✅ Оценка сохранена!

📋 Бариста: {data['barista']}
🏪 Точка: {data['point']}
☕ Напиток: {data.get('drink_type')}

📊 Оценки:
• Баланс: {data.get('balance')}/5
• Букет: {data.get('bouquet')}/5  
• Тело: {data.get('body')}/5
• Послевкусие: {data.get('aftertaste')}/5

💬 Комментарий: {data.get('comment', 'нет')}
        """
    else:  # Молочный напиток
        report = f"""
✅ Оценка сохранена!

📋 Бариста: {data['barista']}
🏪 Точка: {data['point']}
🥛 Напиток: {data.get('drink_type')}

📊 Оценки:
• Баланс: {data.get('balance')}/5
• Букет: {data.get('bouquet')}/5
• Пена: {data.get('foam')}/5
• Латте-арт: {data.get('latte_art')}/5

📷 Фото: {"есть" if data.get('photo_file_id') else "нет"}
💬 Комментарий: {data.get('comment', 'нет')}
        """
    
    # Если есть фото, показываем его вместе с отчетом
    if data.get('photo_file_id'):
        await update.message.reply_photo(
            photo=data['photo_file_id'],
            caption=report,
            reply_markup=get_main_menu()
        )
    else:
        await update.message.reply_text(
            report,
            reply_markup=get_main_menu()
        )
        
def get_review_conversation_handler():
    """Возвращает настроенный ConversationHandler для полной оценки"""
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^☕ Оценить напиток$"), start_review),
            CommandHandler("review", start_review)
        ],
        states={
            SELECTING_BARISTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_barista)],
            SELECTING_POINT: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_point)],
            SELECTING_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_category)],
            
            # Ветка эспрессо/фильтра
            ESPRESSO_DRINK_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_espresso_drink_type)],
            ESPRESSO_BALANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_espresso_balance)],
            ESPRESSO_BOUQUET: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_espresso_bouquet)],
            ESPRESSO_BODY: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_espresso_body)],
            ESPRESSO_AFTERTASTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_espresso_aftertaste)],
            ESPRESSO_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_espresso_comment)],
            
            # Ветка молочных напитков
            MILK_DRINK_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_milk_drink_type)],
            MILK_BALANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_milk_balance)],
            MILK_BOUQUET: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_milk_bouquet)],
            MILK_FOAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_milk_foam)],
            MILK_LATTE_ART: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_milk_latte_art)],
            MILK_PHOTO: [MessageHandler(filters.TEXT | filters.PHOTO, select_milk_photo)],
            MILK_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_milk_comment)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CommandHandler("start", start_cancel_conversation),
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_conversation),
        ],
        allow_reentry=True
    )
