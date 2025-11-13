# simple_test.py
try:
    from telegram import __version__
    print(f"✅ python-telegram-bot версия: {__version__}")
    
    from telegram import Update
    from telegram.ext import Application
    print("✅ Все импорты работают!")
    
    import sqlalchemy
    print(f"✅ SQLAlchemy версия: {sqlalchemy.__version__}")
    
except ImportError as e:
    print(f"❌ Ошибка: {e}").\venv\Scripts\activate