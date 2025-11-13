import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# Настройка логирования
logging.basicConfig(level=logging.INFO)

async def start(update: Update, context):
    await update.message.reply_text("🤖 Бот работает!")

def main():
    # Временный токен для теста
    application = Application.builder().token("8531765653:AAEWDaM2crEA1ZMLoNFRLFxC-48CAxwMKOE").build()
    
    application.add_handler(CommandHandler("start", start))
    
    print("Запускаю бота...")
    application.run_polling()

if __name__ == "__main__":
    main()