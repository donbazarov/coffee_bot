import sys
import os

# Добавляем папку bot в путь Python
sys.path.append(os.path.join(os.path.dirname(__file__), '.'))

from bot.main import CoffeeBot

if __name__ == "__main__":
    print("🚀 Запуск Coffee Quality Bot...")
    bot = CoffeeBot()
    bot.run()