# Деплой на VDS

Пошаговая инструкция: как поднять сайт на сервере и потом обновлять его одной командой.

> **Важно:** везде ниже встречается `coffee.example.com` — это заглушка.
> Замените её на свой домен. Достаточно выполнить одну команду после клонирования:
>
> ```bash
> grep -rl 'coffee.example.com' .env deploy/ | xargs sed -i 's/coffee.example.com/ВАШ-ДОМЕН/g'
> ```
>
> Имя пользователя `coffee` и путь `/opt/coffee_bot` тоже можно поменять — тогда
> поправьте их во всех командах и в `deploy/coffee-bot.service`.

---

## Что понадобится

* VDS с Ubuntu 22.04 / 24.04 (на Debian 12 тоже работает, команды те же).
* Домен, у которого A-запись указывает на IP сервера.
* Репозиторий на GitHub: `https://github.com/donbazarov/coffee_bot` — приватный или публичный.

---

## Шаг 1. Подготовка сервера

Подключитесь по SSH и поставьте нужные пакеты:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git nginx sqlite3
```

Проверьте версию Python — нужна 3.10 или новее:

```bash
python3 --version
```

---

## Шаг 2. Пользователь для приложения

Отдельный пользователь без прав root — правильная практика: если сайт взломают,
вредитель не получит весь сервер.

```bash
sudo adduser --system --group --home /opt/coffee_bot coffee
```

---

## Шаг 3. Код с GitHub

```bash
sudo mkdir -p /opt/coffee_bot
sudo chown coffee:coffee /opt/coffee_bot

# Публичный репозиторий:
sudo -u coffee git clone https://github.com/donbazarov/coffee_bot.git /opt/coffee_bot
```

**Если репозиторий приватный**, нужен ключ доступа. Сгенерируйте его от имени
пользователя `coffee` и добавьте публичную часть в GitHub → *Settings → SSH keys*:

```bash
sudo -u coffee ssh-keygen -t ed25519 -C "coffee-bot@vds" -f /opt/coffee_bot/.ssh/id_ed25519 -N ""
sudo -u coffee cat /opt/coffee_bot/.ssh/id_ed25519.pub   # этот текст вставить в GitHub
sudo -u coffee git clone git@github.com:donbazarov/coffee_bot.git /tmp/coffee_bot_tmp
sudo mv /tmp/coffee_bot_tmp/* /tmp/coffee_bot_tmp/.[!.]* /opt/coffee_bot/ && sudo rm -rf /tmp/coffee_bot_tmp
```

---

## Шаг 4. Виртуальное окружение и зависимости

```bash
sudo -u coffee python3 -m venv /opt/coffee_bot/venv
sudo -u coffee /opt/coffee_bot/venv/bin/pip install --upgrade pip
sudo -u coffee /opt/coffee_bot/venv/bin/pip install -r /opt/coffee_bot/requirements.txt
```

---

## Шаг 5. Настройки

Создайте `.env` из шаблона и откройте на редактирование:

```bash
sudo -u coffee cp /opt/coffee_bot/.env.example /opt/coffee_bot/.env
sudo -u coffee nano /opt/coffee_bot/.env
```

Заполните:

| Переменная | Что писать |
|---|---|
| `WEB_SESSION_SECRET` | длинная случайная строка: `openssl rand -hex 32` |
| `TELEGRAM_BOT_USERNAME` | имя бота без `@`, например `NeftCoffeeBot` |
| `TELEGRAM_AUTH_CALLBACK_URL` | `https://ВАШ-ДОМЕН/auth/telegram/callback` |
| `TELEGRAM_BOT_TOKEN` | токен бота (можно оставить пустым, если рядом лежит `credentials.json`) |
| `WEB_COOKIE_SECURE` | `1` — обязательно, сайт работает по HTTPS |
| `NEFT_DATA_DIR` | `/opt/coffee_bot/data/stories` |

`.env` не хранится в git, поэтому переживает любые обновления — заполняется один раз.

Файл с секретами должен быть доступен только владельцу:

```bash
sudo chmod 600 /opt/coffee_bot/.env
```

---

## Шаг 6. Автозапуск через systemd

```bash
sudo cp /opt/coffee_bot/deploy/coffee-bot.service /etc/systemd/system/coffee-bot.service
sudo nano /etc/systemd/system/coffee-bot.service   # если путь или пользователь другие
sudo systemctl daemon-reload
sudo systemctl enable --now coffee-bot
```

Проверка:

```bash
sudo systemctl status coffee-bot
curl -I http://127.0.0.1:8001/stories   # ожидаем 200 OK
```

Если что-то не так — смотрите лог:

```bash
sudo journalctl -u coffee-bot -n 50 --no-pager
```

---

## Шаг 7. nginx

```bash
sudo cp /opt/coffee_bot/deploy/nginx.conf /etc/nginx/sites-available/coffee-bot
sudo sed -i 's/coffee.example.com/ВАШ-ДОМЕН/g' /etc/nginx/sites-available/coffee-bot
sudo ln -s /etc/nginx/sites-available/coffee-bot /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

На этом шаге HTTPS-блок ещё не работает — сертификата нет. Это нормально, идём дальше.

---

## Шаг 8. HTTPS-сертификат

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d ВАШ-ДОМЕН
```

Certbot сам поправит конфиг nginx и настроит автопродление. Проверьте его:

```bash
sudo certbot renew --dry-run
```

Теперь сайт открывается по `https://ВАШ-ДОМЕН`.

---

## Шаг 9. Вход через Telegram

1. Откройте [@BotFather](https://t.me/BotFather) → `/mybots` → ваш бот → **Bot Settings → Domain**.
   Укажите домен **без** `https://` и без слэша: `ВАШ-ДОМЕН`.
2. Откройте `https://ВАШ-ДОМЕН` — на экране входа должен появиться виджет Telegram.
3. Войдите. Если вашего аккаунта нет в базе, заявка попадёт в раздел
   **Команда** (панель наставника) — там её нужно одобрить и выдать роль.

> Telegram отдаёт виджет только по HTTPS и только для домена, указанного у BotFather.
> Пока домен не задан, кнопка входа просто не отрисуется.

---

## Страница историй гостей

Публичная страница живёт по адресу:

```
https://ВАШ-ДОМЕН/stories
```

Она **не** появляется в навигации панели сотрудников — это страница для гостей.
Дайте гостям прямую ссылку или QR-код на неё.

Модерация — в **Личном кабинете** у наставника и старшего: раздел
«Истории гостей». Там можно опубликовать историю, поправить текст или фото и удалить лишнее.
Отдельный приватный ключ `Neft_moderation_key` больше не нужен: доступ закрыт
обычной авторизацией сайта и ролью.

---

## Обновление сайта (рутинная операция)

Вы пушите изменения в GitHub, на сервере выполняете одну команду:

```bash
cd /opt/coffee_bot && ./deploy/deploy.sh
```

Скрипт заберёт свежий код из ветки `main`, доустановит зависимости из
`requirements.txt`, перезапустит сервис и покажет его статус.

Сделать команду ещё короче — один раз добавьте алиас:

```bash
echo "alias deploy-coffee='cd /opt/coffee_bot && ./deploy/deploy.sh'" >> ~/.bashrc
source ~/.bashrc
# дальше достаточно:  deploy-coffee
```

### Автодеплой по кнопке из GitHub (по желанию)

1. Сгенерируйте ключ и добавьте его в GitHub → репозиторий → *Settings → Deploy keys* (с правом на запись):

   ```bash
   sudo -u coffee ssh-keygen -t ed25519 -f /opt/coffee_bot/.ssh/deploy_key -N ""
   sudo -u coffee cat /opt/coffee_bot/.ssh/deploy_key.pub
   ```

2. Склонируйте репозиторий по SSH (если ещё клонирован по HTTPS):

   ```bash
   sudo -u coffee git -C /opt/coffee_bot remote set-url origin git@github.com:donbazarov/coffee_bot.git
   ```

3. В репозитории → *Settings → Webhooks* → **Add webhook**:
   * Payload URL: `https://ВАШ-ДОМЕН/deploy-hook` — либо просто запускайте деплой вручную.

Для маленького проекта проще ручной запуск: он предсказуем и занимает секунды.

---

## Резервные копии

Что важно сохранить: `coffee_quality.db` (оценки, сотрудники, графики) и `data/stories/`
(истории гостей и фотографии). Всё остальное восстанавливается из GitHub.

```bash
cd /opt/coffee_bot && ./deploy/backup.sh
```

Архивы складываются в `/var/backups/coffee_bot`, старше 30 дней удаляются.
Автоматизировать — добавьте в crontab пользователя `coffee`:

```bash
sudo -u coffee crontab -e
# строка:
0 4 * * * /opt/coffee_bot/deploy/backup.sh >> /var/log/coffee-bot-backup.log 2>&1
```

---

## Как перенести данные с локальной машины

Если на компьютере уже есть рабочие данные, скопируйте их на сервер:

```bash
# запускать на СВОЁМ компьютере, не на сервере
scp coffee_quality.db coffee@ВАШ-IP:/opt/coffee_bot/coffee_quality.db
scp -r data coffee@ВАШ-IP:/opt/coffee_bot/
```

После этого перезапустите сервис:

```bash
sudo systemctl restart coffee-bot
```

---

## Частые проблемы

| Симптом | Причина и решение |
|---|---|
| `502 Bad Gateway` | Сервис не запущен. `sudo systemctl status coffee-bot`, затем `sudo journalctl -u coffee-bot -n 50` |
| Кнопка входа Telegram не появилась | Не задан `TELEGRAM_BOT_USERNAME` или домен не указан у BotFather в *Bot Settings → Domain* |
| `auth_error=verify` после входа | `TELEGRAM_BOT_TOKEN` не совпадает с ботом, чей домен указан у BotFather |
| Сессия сбрасывается при каждом входе | Пустой `WEB_SESSION_SECRET`. Задайте его и перезапустите сервис |
| Фото истории не загружается | Размер больше 5 МБ либо `client_max_body_size` в nginx меньше 8 МБ |
| `Permission denied` при записи | Владелец файлов — не `coffee`: `sudo chown -R coffee:coffee /opt/coffee_bot` |
| После деплоя старый интерфейс | Браузер закешировал статику — обновите страницу с Ctrl+F5 |

---

## Полезные команды

```bash
sudo systemctl status coffee-bot          # состояние сервиса
sudo systemctl restart coffee-bot         # перезапуск
sudo journalctl -u coffee-bot -f          # живой лог
sudo nginx -t && sudo systemctl reload nginx
sudo certbot renew                        # продлить сертификат
cd /opt/coffee_bot && ./deploy/deploy.sh  # обновить сайт из GitHub
```
