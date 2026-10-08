# Деплой на VDS

Пошаговая инструкция: как поднять сайт на сервере и потом обновлять его одной командой.

> **Домен проекта: `neftcoffee.shop`** — он уже прописан в `deploy/nginx.conf`,
> `.env.example` и в вашем `.env`.
>
> Все секреты и настройки лежат в одном файле `.env` в корне проекта
> (токен бота, ключ подписи сессий, данные Google, домен). Он не попадает в git
> и переносится на сервер как есть — см. шаг 5.
>
> Имя пользователя `coffee` и путь `/opt/coffee_bot` можно поменять — тогда
> поправьте их во всех командах и в `deploy/coffee-bot.service`.

---

## Что понадобится

* VDS с Ubuntu 22.04 / 24.04 (на Debian 12 тоже работает, команды те же).
* Домен, у которого A-запись указывает на IP сервера.
* Репозиторий на GitHub: `https://github.com/donbazarov/coffee_bot` — приватный или публичный.

---

**Два пути.** Путь А (Docker) короче: пять шагов, и на сервере не нужны ни `venv`,
ни подходящая версия Python. Путь Б (systemd) подробнее, зато без Docker.
Шаги с nginx и сертификатом у путей общие.

---

## Путь А. Docker (рекомендуется)

### A1. Установить Docker

```bash
sudo apt update && sudo apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

Выйдите из SSH и зайдите снова — иначе команды `docker` будут требовать `sudo`.

### A2. Забрать код

```bash
sudo mkdir -p /opt/coffee_bot
sudo chown "$USER":"$USER" /opt/coffee_bot
git clone https://github.com/donbazarov/coffee_bot.git /opt/coffee_bot
cd /opt/coffee_bot
```

Для приватного репозитория сначала настройте SSH-ключ — см. шаг 3 в пути Б.

### A3. Перенести настройки и данные

Выполняйте **на своём компьютере**, не на сервере:

```bash
scp -r .env coffee_quality.db data user@ВАШ-IP:/opt/coffee_bot/
```

Что переносится:

| Файл | Зачем |
|---|---|
| `.env` | все секреты: токен бота, ключ сессий, домен |
| `coffee_quality.db` | оценки, сотрудники, графики смен |
| `data/` | истории гостей и загруженные фотографии |

> `coffee_quality.db` должна существовать **до первого запуска**: compose монтирует
> её как файл, и если файла нет, Docker создаст на его месте каталог.

### A4. Запустить

```bash
cd /opt/coffee_bot
docker compose up -d --build
docker compose ps
```

Проверка, что приложение живо:

```bash
curl -s http://127.0.0.1:8001/healthz     # ожидаем {"ok":true}
```

Логи, если что-то не так:

```bash
docker compose logs -f
```

### A5. nginx и HTTPS

Выполните **шаги 7 и 8** ниже — они одинаковы для обоих путей.

### A6. Обновление в дальнейшем

```bash
cd /opt/coffee_bot && ./deploy/deploy.sh
```

Скрипт сам увидит, что проект развёрнут в Docker, пересоберёт образ, перезапустит
контейнер и дождётся ответа `/healthz`. Вручную то же самое короче:

```bash
git pull && docker compose up -d --build
```

### Полезное про Docker

```bash
docker compose logs -f            # живой лог
docker compose restart            # перезапуск без пересборки
docker compose down               # остановить и удалить контейнер
docker compose up -d --build      # пересобрать после обновления кода
docker image prune -f             # убрать старые слои образа
```

---

## Путь Б. Без Docker (systemd)

Шаги 1–9 ниже. Сервису нужен Python 3.10 или новее.

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

Все ключи и приватные данные лежат в одном файле `.env` в корне проекта.
Домен вписан в тексте ниже как `neftcoffee.shop` — если он изменится, поправьте
значение `TELEGRAM_AUTH_CALLBACK_URL`.

**Вариант А — скопировать готовый `.env` со своей машины** (проще всего, он уже заполнен):

```bash
# запускать на СВОЁМ компьютере, не на сервере
scp .env coffee@ВАШ-IP:/opt/coffee_bot/.env
```

**Вариант Б — собрать на сервере из шаблона:**

```bash
sudo -u coffee cp /opt/coffee_bot/.env.example /opt/coffee_bot/.env
sudo -u coffee nano /opt/coffee_bot/.env
```

Заполните:

| Переменная | Что писать |
|---|---|
| `WEB_SESSION_SECRET` | длинная случайная строка: `openssl rand -hex 32` |
| `TELEGRAM_BOT_USERNAME` | `NeftCoffeeBot` |
| `TELEGRAM_AUTH_CALLBACK_URL` | `https://neftcoffee.shop/auth/telegram/callback` |
| `TELEGRAM_BOT_TOKEN` | токен бота. Если пусто — берётся `bot_token` из `credentials.json` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | сервисный аккаунт Google одной строкой JSON. Нужен только старому боту, сайту не требуется |
| `WEB_COOKIE_SECURE` | `1` — обязательно, сайт работает по HTTPS |
| `NEFT_DATA_DIR` | `/opt/coffee_bot/data/stories` (или оставьте пустым — путь по умолчанию совпадает) |

`.env` не хранится в git, поэтому переживает любые обновления — заполняется один раз.
Значения из `.env` имеют приоритет над переменными окружения, так что устаревшая
переменная в терминале не сможет подменить домен или токен.

Файл с секретами должен быть доступен только владельцу:

```bash
sudo chmod 600 /opt/coffee_bot/.env
```

После переноса `.env` отдельный `credentials.json` на сервере не нужен —
токен бота уже внутри `.env`.

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
sudo certbot --nginx -d neftcoffee.shop -d www.neftcoffee.shop
```

Certbot сам поправит конфиг nginx и настроит автопродление. Проверьте его:

```bash
sudo certbot renew --dry-run
```

Теперь сайт открывается по `https://neftcoffee.shop`.

---

## Шаг 9. Вход через Telegram

1. Откройте [@BotFather](https://t.me/BotFather) → `/mybots` → `@NeftCoffeeBot` → **Bot Settings → Domain**.
   Укажите домен **без** `https://` и без слэша: `neftcoffee.shop`.
2. Откройте `https://neftcoffee.shop` — на экране входа должен появиться виджет Telegram.
3. Войдите. Если вашего аккаунта нет в базе, заявка попадёт в раздел
   **Команда** (панель наставника) — там её нужно одобрить и выдать роль.

> Telegram отдаёт виджет только по HTTPS и только для домена, указанного у BotFather.
> Пока домен не задан, кнопка входа просто не отрисуется.

---

## Страница историй гостей

Публичная страница живёт по адресу:

```
https://neftcoffee.shop/stories
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

### Автодеплой при пуше в GitHub (по желанию)

В репозитории лежит готовый workflow `.github/workflows/deploy.yml`: на каждый
пуш в `main` он заходит по SSH на сервер и запускает `deploy/deploy.sh`.

Включить — три шага:

1. Сгенерируйте отдельный ключ **на сервере** и разрешите вход по нему:

   ```bash
   sudo -u coffee ssh-keygen -t ed25519 -f /home/coffee/.ssh/github_deploy -N ""
   sudo -u coffee sh -c 'cat /home/coffee/.ssh/github_deploy.pub >> /home/coffee/.ssh/authorized_keys'
   sudo -u coffee cat /home/coffee/.ssh/github_deploy       # приватный ключ — целиком, с BEGIN и END
   ```

2. В GitHub → репозиторий → *Settings → Secrets and variables → Actions* добавьте:

   | Secret | Значение |
   |---|---|
   | `SSH_HOST` | IP или домен сервера |
   | `SSH_USER` | `coffee` |
   | `SSH_KEY` | приватный ключ из шага 1 целиком |

3. Разрешите пользователю `coffee` перезапускать сервис без пароля:

   ```bash
   echo "coffee ALL=(ALL) NOPASSWD: /bin/systemctl restart coffee-bot, /bin/systemctl is-active coffee-bot, /bin/systemctl status coffee-bot" | sudo tee /etc/sudoers.d/coffee-bot
   sudo chmod 440 /etc/sudoers.d/coffee-bot
   ```

   Этот же файл нужен и для ручного `./deploy/deploy.sh` — без него скрипт
   спросит пароль и не сможет работать из Actions.

После этого любой пуш в `main` сам обновляет сайт. Проверить можно на вкладке
**Actions** в репозитории.

Ручной запуск всё равно остаётся: `cd /opt/coffee_bot && ./deploy/deploy.sh`.

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
