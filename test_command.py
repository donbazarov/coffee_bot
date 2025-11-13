import sqlite3
from bot.handlers.debug import show_db_command, stats_debug_command, show_photo_command
from telegram import Update
from telegram.ext import ContextTypes
import asyncio

# Создаем mock объекты для тестирования
class MockUpdate:
    def __init__(self):
        self.message = MockMessage()

class MockMessage:
    def reply_text(self, text):
        print(f"Бот ответил: {text}")
        return True

class MockContext:
    pass

async def test_commands():
    print("🧪 Тестируем отладочные команды...")
    
    update = MockUpdate()
    context = MockContext()
    
    print("\n1. Тестируем /show_db:")
    await show_db_command(update, context)
    
    print("\n2. Тестируем /stats_debug:")
    await stats_debug_command(update, context)
    
    print("\n3. Тестируем /show_photo 1:")
    # Для этой команды нужны аргументы
    context.args = ['1']
    await show_photo_command(update, context)

if __name__ == "__main__":
    asyncio.run(test_commands())