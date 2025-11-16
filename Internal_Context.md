# Coffee Quality Bot - Internal Context

## 🏗️ ARCHITECTURE
**Framework**: python-telegram-bot v20+ (async)
**Database**: SQLite + SQLAlchemy
**External APIs**: Google Sheets API, Telegram Bot API

## 📁 CORE STRUCTURE
bot/
├── main.py # Entry point, handler setup
├── config.py # Bot token, user config (legacy)
├── database/
│ ├── models.py # SQLAlchemy ORM models
│ ├── simple_db.py # DB operations (SessionLocal pattern)
│ ├── user_operations.py # User management
│ ├── schedule_operations.py # Shift operations
│ └── migrations.py # DB migrations
├── handlers/
│ ├── settings.py # Settings conversation handler (MAIN)
│ ├── review.py # Drink assessment flow
│ ├── stats.py # Statistics
│ └── debug.py # Debug commands
├── keyboards/
│ └── menus.py # Reply keyboard menus
└── utils/
├── auth.py # Role-based access control
└── google_sheets.py # Google Sheets integration

## 🗄️ DATABASE SESSION MANAGEMENT

### Explicit Session Pattern (No session_scope)
```python
# Все функции БД используют явное управление сессиями
def get_shift_types():
    db = SessionLocal()  # Создаем сессию
    try:
        return db.query(ShiftType).order_by(ShiftType.point, ShiftType.start_time).all()
    finally:
        db.close()  # Всегда закрываем соединение

def create_shift_type(shift_type_data):
    db = SessionLocal()
    try:
        shift_type = ShiftType(...)
        db.add(shift_type)
        db.flush()  # Получаем ID перед коммитом
        shift_type_id = shift_type.id
        db.commit()  # Явный коммит
        return shift_type_id
    except Exception as e:
        db.rollback()  # Явный откат при ошибках
        raise e
    finally:
        db.close()

Причины такого подхода:

Прямой контроль над жизненным циклом сессии

Легче отлаживать проблемы с соединениями

Избегаем проблем с контекстными менеджерами в сложных сценариях

🎯 CRITICAL TECHNICAL DECISIONS
1. User Identification System
Primary Key: Iiko ID (из корпоративной системы)
Fallback: Telegram username (для функционала замен)
Legacy: telegram_id (мигрирован в iiko_id)

# Определение пользователя в разных контекстах
user = get_user_by_iiko_id(iiko_id)  # Основной метод
user = get_user_by_telegram_id(tg_id)  # Для legacy
user = get_user_by_username(username)  # Для замен смен

2. Time Handling Strategy
Проблема: SQLite не поддерживает TIME тип
Решение: Хранение времени как TEXT в формате "HH:MM"

# В таблице shift_types
start_time TEXT NOT NULL,  # "07:00"
end_time TEXT NOT NULL,    # "15:00"

# Функции для работы со временем
def get_shift_type_by_time_strings(start_str, end_str):
    # Поиск по строковому сравнению

3. Conversation Handler Architecture
Централизация: Весь функционал настроек в одном ConversationHandler
Состояния: 27 уникальных состояний в settings.py
Порядок обработчиков: Критически важен в main.py

# CORRECT ORDER в main.py
1. Command handlers (start, cancel, stats)
2. CallbackQuery handlers 
3. Review ConversationHandler
4. Settings ConversationHandler  ← ДОЛЖЕН БЫТЬ ПЕРЕД общим обработчиком
5. Specific message handlers
6. General fallback handler      ← ПОСЛЕДНИМ

4. Menu System Structure
Главное меню: Функции в keyboards/menus.py
Меню настроек: Динамически создаются в handlers/settings.py
Причина: Меню настроек слишком сложное для статического определения

# НЕПРАВИЛЬНО - использовать классы для меню настроек
# ПРАВИЛЬНО - создавать динамически в обработчиках
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("👥 Управление пользователями")],
        [KeyboardButton("📅 Управление расписанием")],
        [KeyboardButton("🕒 Управление типами смен")],  # Динамически
        [KeyboardButton("🗑️ Очистить таблицу оценок")],
        [KeyboardButton("⬅️ Назад")]
    ]
    # Создается на лету, а не импортируется из menus.py

🔧 KEY INTEGRATION POINTS
Google Sheets Parser Logic
# Алгоритм парсинга:
1. Получить лист по названию месяца ("Декабрь 24")
2. Извлечь iiko_id из колонки A (строки 4-30)
3. Извлечь даты из строки 1 (столбцы C-BL)
4. Обработать пары столбцов (приход/уход)
5. Нормализовать формат времени ("7:00" → "07:00")
6. Найти matching shift_type по времени
7. Создать смену с shift_type_id

Shift Type Matching
# Критически важная функция
def get_shift_type_by_time_strings(start_time_str, end_time_str):
    # Ищет точное совпадение времени
    # Форматы должны быть идентичны: "07:00" == "07:00"
    # Не сработает: "7:00" != "07:00"

🚨 CURRENT PAIN POINTS
1. Time Format Inconsistency
Проблема: Разные форматы времени между Google Sheets и БД
Решение: Функция нормализации _normalize_time_format()

2. Menu Handler Registration
Проблема: Кнопки попадают в общий обработчик
Решение: Правильный порядок регистрации + проверка фильтров

3. Session Management Overhead
Проблема: Много boilerplate кода для сессий
Принято решение: Явное управление лучше для отладки

📊 DATA FLOW PATTERNS
Для управления типами смен:
User → "⚙️ Настройки" → "🕒 Управление типами смен" → 
→ ConversationHandler (settings.py) → 
→ SHIFT_TYPES_MENU состояние → 
→ Кнопки создаются динамически → 
→ Обработчики в том же файле

Для парсинга расписания:
User → "⚙️ Настройки" → "📅 Управление расписанием" → 
→ "🔄 Парсить текущий месяц" → 
→ google_sheets.parse_schedule_from_sheet() → 
→ get_shift_type_by_time_strings() → 
→ bulk_create_shifts()

🔐 SECURITY MODEL
Role-Based Access Control
# Декораторы проверки прав
@require_roles([ROLE_MENTOR])  # Только наставники
@require_roles([ROLE_SENIOR, ROLE_MENTOR])  # Старшие и наставники

# Или явная проверка в коде
if not is_senior_or_mentor(update):
    await update.message.reply_text("❌ Доступ запрещен")
    return
