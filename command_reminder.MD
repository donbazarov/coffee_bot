# Залить один файл
scp credentials.json root@45.10.244.32:/opt/coffee_bot/

# Залить всю папку
scp -r ./bot root@45.10.244.32:/opt/coffee_bot/

# Залить несколько файлов
scp file1.py file2.py root@45.10.244.32:/opt/coffee_bot/
scp checklist_operations.py checklist_migrations.py root@45.10.244.32:/opt/coffee_bot/bot/database

# Скачать файл БД (бэкап)
scp root@45.10.244.32:/opt/coffee_bot/coffee_quality.db ./backup/

# Скачать логи
scp root@45.10.244.32:/var/log/coffee-bot.log ./

# Подключение к серверу
ssh root@45.10.244.32

# Управление ботом (на сервере):
# Статус бота
sudo systemctl status coffee-bot

# Запуск бота
sudo systemctl start coffee-bot

# Остановка бота
sudo systemctl stop coffee-bot

# Перезагрузка бота
sudo systemctl restart coffee-bot

# Включить автозапуск
sudo systemctl enable coffee-bot

# Логи в реальном времени
sudo journalctl -u coffee-bot -f

# Последние 100 строк логов
sudo journalctl -u coffee-bot -n 100

# Логи за сегодня
sudo journalctl -u coffee-bot --since today

# Перейти в папку проекта
cd /opt/coffee_bot

# Обновить код с GitHub
git pull origin main

# Проверить статус
git status

# Посмотреть историю
git log --oneline -5

# Активировать venv
source /opt/coffee_bot/venv/bin/activate

# Установить зависимости
pip install -r requirements.txt

# Проверить установленные пакеты
pip list

# 🔄 ПОЛНЫЙ РАБОЧИЙ ЦИКЛ ОБНОВЛЕНИЯ:
#Локально на компьютере:
# 1. Редактируете код в Cursor
# 2. Заливаете изменения на сервер
scp -r ./bot root@45.10.244.32:/opt/coffee_bot/
scp requirements.txt root@45.10.244.32:/opt/coffee_bot/

# На сервере:
# 3. Подключаетесь к серверу
ssh root@45.10.244.32

# 4. Обновляем зависимости (если нужно)
cd /opt/coffee_bot
source venv/bin/activate
pip install -r requirements.txt

# 5. Перезапускаем бота
sudo systemctl restart coffee-bot

# 6. Проверяем логи
sudo journalctl -u coffee-bot -n 20 --no-pager

#Работа с базой данных:
# Бэкап БД
cp /opt/coffee_bot/coffee_quality.db /opt/coffee_bot/coffee_quality.db.backup

# Восстановление БД
cp /opt/coffee_bot/coffee_quality.db.backup /opt/coffee_bot/coffee_quality.db

# Мониторинг сервера:
# Память и CPU
htop
free -h

# Дисковое пространство
df -h

# Процессы бота
ps aux | grep python

# Принудительно убить процессы бота
sudo pkill -f "python.*coffee"

# Если бот не запускается - проверить синтаксис
cd /opt/coffee_bot
python -m py_compile run.py

# Подключение: ssh root@45.10.244.32
# zXXEr25k96
# Статус бота: sudo systemctl status coffee-bot
# Рестарт бота: sudo systemctl restart coffee-bot
# Логи: sudo journalctl -u coffee-bot -f
# Git обновление: cd /opt/coffee_bot && git pull origin main
# git config --global --add safe.directory /opt/coffee_bot
# Залить файл: scp file.py root@45.10.244.32:/opt/coffee_bot/