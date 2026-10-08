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

Затем **на сервере** сложите базу рядом с остальными данными и отдайте каталог
пользователю контейнера — иначе изменения не будут сохраняться:

```bash
cd /opt/coffee_bot
mv coffee_quality.db data/
chown -R 10001:10001 data
```

> Почему именно так: приложение в контейнере работает от uid **10001**, а не от root.
> Всё, во что оно пишет — база, истории, фото — должно принадлежать ему. Каталог
> `data/` монтируется в контейнер как `/app/data`, и база тоже лежит внутри него,
> поэтому одного `chown` достаточно.

Проверьте, что запись действительно есть:

```bash
docker compose exec web python -c "
from bot.database.models import engine
from sqlalchemy import text
with engine.begin() as c:
    c.execute(text('CREATE TABLE IF NOT EXISTS _probe (id INTEGER)'))
    c.execute(text('DROP TABLE _probe'))
print('БАЗА: запись работает')
"
```

### A4. Запустить

```bash
cd /opt/coffee_bot
docker compose up -d --build
docker compose ps
```

Поднимаются **два контейнера**: `coffee-bot` (сайт) и `coffee-bot-login` (бот,
подтверждающий вход). Оба должны быть в состоянии `running`.

Проверка, что приложение живо:

```bash
curl -s http://127.0.0.1:8001/healthz     # ожидаем {"ok":true}
```

Логи, если что-то не так:

```bash
docker compose logs -f                # оба сервиса
docker compose logs -f bot            # только бот входа
```

Убедитесь, что бот видит Telegram — это обязательное условие входа:

```bash
curl -s -m 10 "https://api.telegram.org/bot$(grep TELEGRAM_BOT_TOKEN /opt/coffee_bot/.env | cut -d= -f2)/getMe"
```

Ответ должен содержать `"ok":true`. Если связи нет — вход через Telegram работать
не будет, нужен прокси или сервер в другой стране.

### A5. nginx и HTTPS

Выполните **шаги 7 и 8** ниже — они одинаковы для обоих путей. Порядок важен:
сначала HTTP-конфиг, потом сертификат.

### A6. Обновление в дальнейшем

```bash
cd /opt/coffee_bot && ./deploy/deploy.sh
```

Скрипт сам увидит, что проект развёрнут в Docker, пересоберёт образ, перезапустит
оба контейнера и дождётся ответа `/healthz`. Вручную то же самое короче:

```bash
git pull && docker compose up -d --build
```

### Полезное про Docker

```bash
docker compose logs -f            # живой лог обоих контейнеров
docker compose restart            # перезапуск без пересборки
docker compose down               # остановить и удалить контейнеры
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

**Важен порядок:** сначала ставим HTTP-конфиг, и только потом выпускаем сертификат.
Если сразу положить конфиг со `ssl_certificate`, nginx не запустится — он читает
файл сертификата на этапе проверки конфига, а того ещё нет.

```bash
sudo mkdir -p /var/www/html
sudo cp /opt/coffee_bot/deploy/nginx-http.conf /etc/nginx/sites-available/coffee-bot
sudo ln -sf /etc/nginx/sites-available/coffee-bot /etc/nginx/sites-enabled/coffee-bot
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

Проверьте, что сайт уже проксируется по HTTP:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: neftcoffee.shop' http://127.0.0.1/
```

Ожидаем `200`. Если `404` или `502` — смотрите `sudo nginx -T | grep -E 'server_name|proxy_pass'`
и убедитесь, что конфиг подхватился, а приложение слушает 8001.

> Про `include /etc/nginx/sites-enabled/*;` в `/etc/nginx/nginx.conf`: он там уже есть
> по умолчанию. **Не добавляйте его повторно** — из-за дубликата nginx разберёт
> конфиг дважды и упадёт с `duplicate listen options`.

---

## Шаг 8. HTTPS-сертификат

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot certonly --webroot -w /var/www/html -d neftcoffee.shop
```

Теперь, когда сертификат готов, ставим итоговый конфиг с HTTPS и редиректом:

```bash
sudo cp /opt/coffee_bot/deploy/nginx.conf /etc/nginx/sites-available/coffee-bot
sudo nginx -t && sudo systemctl reload nginx
```

> В `deploy/nginx.conf` HTTP/2 включается директивой `listen 443 ssl http2`.
> Отдельная строка `http2 on;` появилась только в nginx 1.25.1, а на Ubuntu 22.04
> стоит 1.18 — там она падает с `unknown directive "http2"`.

Проверьте автопродление:

```bash
sudo certbot renew --dry-run
```

Теперь сайт открывается по `https://neftcoffee.shop`.

---

## Шаг 9. Вход через Telegram

Вход устроен как **ссылка на бота**: браузер уходит на `t.me/@NeftCoffeeBot?start=<токен>`,
открывается приложение Telegram, сотрудник жмёт **Start** — и контейнер
`coffee-bot-login` подтверждает вход, после чего сайт выдаёт сессию.

Почему не штатный виджет: он грузится с `telegram.org`, который в России
блокируется, поэтому у сотрудников без VPN кнопка просто не появляется.
Приложение Telegram ходит своим протоколом и работает независимо от этого.

Что нужно проверить:

1. Контейнер бота запущен и видит Telegram:

   ```bash
   docker compose logs bot | tail -5
   ```

   Ожидаем строку вида `Бот @NeftCoffeeBot готов принимать входы`.

2. Откройте `https://neftcoffee.shop` — на экране входа кнопка **«Войти через Telegram»**.
3. Нажмите её: откроется Telegram, нажмите **Start**. Вернитесь в браузер — вход
   произойдёт автоматически (страница сама опрашивает статус).
4. Если вашего аккаунта нет в базе, заявка попадёт в раздел **Команда**
   (панель наставника) — там нужно выдать роль, после чего войти снова.

**Дополнительно (по желанию):** можно указать домен у BotFather
([@BotFather](https://t.me/BotFather) → `/mybots` → `@NeftCoffeeBot` → **Bot Settings → Domain** →
`neftcoffee.shop`). Тогда заработает и резервный вход через виджет на десктопе —
он спрятан в свёрнутый блок «Вход через виджет Telegram».

> Виджет отдаётся только по HTTPS и только для домена из BotFather. Без домена
> он не отрисуется — но основной вход через приложение работает и так.

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

Скрипт заберёт свежий код из ветки `main`, обновит зависимости, пересоберёт и
перезапустит сервисы (Docker или systemd — определит сам) и дождётся ответа `/healthz`.

Сделать команду ещё короче — один раз добавьте алиас:

```bash
echo "alias deploy-coffee='cd /opt/coffee_bot && ./deploy/deploy.sh'" >> ~/.bashrc
source ~/.bashrc
# дальше достаточно:  deploy-coffee
```

### Автодеплой при пуше в GitHub (по желанию)

В репозитории лежит готовый workflow `.github/workflows/deploy.yml`: на каждый
пуш в `main` он заходит по SSH на сервер и запускает `deploy/deploy.sh`.

> **Пока секреты не заданы, workflow будет падать.** Это нормально — он выводит
> понятную подсказку, чего не хватает. Если автодеплой не нужен вовсе, удалите
> файл `.github/workflows/deploy.yml` или оставьте в нём только триггер
> `workflow_dispatch:` — тогда он запускается лишь вручную, а пуши ничего не ломают.

**Шаг 1. Создать ключ.** На сервере (для Docker-развёртывания — от `root`,
он владеет `/opt/coffee_bot`):

```bash
ssh-keygen -t ed25519 -f /root/.ssh/github_deploy -N ""
cat /root/.ssh/github_deploy.pub >> /root/.ssh/authorized_keys
cat /root/.ssh/github_deploy
```

Последняя команда печатает **приватный** ключ — он понадобится целиком,
вместе со строками `-----BEGIN` и `-----END`.

Проверьте, что вход по нему работает:

```bash
ssh -i /root/.ssh/github_deploy -o IdentitiesOnly=yes root@ВАШ-IP 'echo вход работает'
```

**Шаг 2. Добавить секреты.** GitHub → репозиторий → *Settings → Secrets and
variables → Actions* → **New repository secret**:

| Secret | Значение | Обязателен |
|---|---|---|
| `SSH_HOST` | IP или домен сервера | да |
| `SSH_USER` | `root` для Docker | да |
| `SSH_KEY` | приватный ключ из шага 1 целиком | да |
| `SSH_PORT` | порт SSH, если не 22 | нет |

> `SSH_KEY` вставляйте вместе с завершающим переводом строки. Если ключ
> «съелся» при копировании, вход упадёт с `invalid format`.

**Шаг 3 (только для пути без Docker).** Если сервис работает под systemd от
пользователя `coffee`, разрешите ему перезапуск без пароля:

```bash
echo "coffee ALL=(ALL) NOPASSWD: /bin/systemctl restart coffee-bot, /bin/systemctl is-active coffee-bot, /bin/systemctl status coffee-bot" | sudo tee /etc/sudoers.d/coffee-bot
sudo chmod 440 /etc/sudoers.d/coffee-bot
```

Этот же файл нужен и для ручного `./deploy/deploy.sh` в режиме systemd — без него
скрипт спросит пароль и не сможет работать из Actions.

После этого любой пуш в `main` сам обновляет сайт. Проверить можно на вкладке
**Actions** в репозитории.

Ручной запуск всё равно остаётся: `cd /opt/coffee_bot && ./deploy/deploy.sh`.

**Если workflow недоступен, а деплой нужен срочно** — выполните вручную то же
самое: зайдите по SSH и запустите `./deploy/deploy.sh`.

---

## Скрипты должны быть исполняемыми

`.sh`-файлы в репозитории лежат с битом `755`. Если при клонировании он потерялся
(на Windows git по умолчанию не хранит этот бит), скрипт не запустится и скажет
`Permission denied`. Лечится одной командой на сервере:

```bash
chmod +x /opt/coffee_bot/deploy/*.sh
```

Либо просто передайте скрипт интерпретатору: `bash deploy/deploy.sh`.

---

## Резервные копии

Что важно сохранить: `data/coffee_quality.db` (оценки, сотрудники, графики)
и `data/stories/` (истории гостей и фотографии). Всё остальное восстанавливается из GitHub.

```bash
cd /opt/coffee_bot && ./deploy/backup.sh
```

Архивы складываются в `/var/backups/coffee_bot`, старше 30 дней удаляются.
Автоматизировать — добавьте в crontab:

```bash
crontab -e
# строка:
0 4 * * * /opt/coffee_bot/deploy/backup.sh >> /var/log/coffee-bot-backup.log 2>&1
```

---

## Как перенести данные с локальной машины

Если на компьютере уже есть рабочие данные, скопируйте их на сервер:

```bash
# запускать на СВОЁМ компьютере, не на сервере
scp -r data ВАШ-ПОЛЬЗОВАТЕЛЬ@ВАШ-IP:/opt/coffee_bot/
# база лежит внутри data/
```

Затем на сервере отдайте каталог пользователю контейнера и перезапустите сервисы:

```bash
cd /opt/coffee_bot
chown -R 10001:10001 data
docker compose restart          # или: sudo systemctl restart coffee-bot
```

---

## Частые проблемы

| Симптом | Причина и решение |
|---|---|
| **Изменения не сохраняются** | Каталог `data/` принадлежит не контейнеру. `sudo chown -R 10001:10001 /opt/coffee_bot/data` |
| `502 Bad Gateway` | Сервис не запущен. `docker compose ps` и `docker compose logs -f` (или `systemctl status coffee-bot`) |
| Кнопки входа нет вообще | Не задан `TELEGRAM_BOT_USERNAME` — проверьте `docker compose exec web printenv \| grep TELEGRAM` и пересоздайте контейнер: `docker compose up -d --force-recreate` |
| Кнопка «Войти через Telegram» есть, но вход не подтверждается | Контейнер бота не видит Telegram: `docker compose logs bot`. Проверьте `curl https://api.telegram.org/bot<токен>/getMe` |
| Бот отвечает ошибкой 409 в логах | С тем же токеном работает старый бот (`python -m bot.main`) — остановите его |
| Виджет Telegram не появился | Он грузится с `telegram.org`: нужен VPN или домен у BotFather. Основной вход через приложение работает и без него |
| `auth_error=verify` после входа | `TELEGRAM_BOT_TOKEN` не совпадает с ботом, чей домен указан у BotFather |
| Сессия сбрасывается при каждом входе | Пустой `WEB_SESSION_SECRET`. Задайте его и перезапустите сервис |
| Фото истории не загружается | Размер больше 5 МБ либо `client_max_body_size` в nginx меньше 8 МБ |
| `nginx: unknown directive "http2"` | Взяли конфиг для nginx 1.25+ на nginx 1.18. Используйте `deploy/nginx.conf` — там `listen 443 ssl http2` |
| `nginx: duplicate listen options` | Дважды подключён `sites-enabled`. Проверьте `grep -n sites-enabled /etc/nginx/nginx.conf` — строка должна быть одна |
| `nginx: cannot load certificate` | Конфиг с HTTPS поставлен до выпуска сертификата. Сначала `deploy/nginx-http.conf`, потом certbot |
| После деплоя старый интерфейс | Браузер закешировал статику — обновите страницу с Ctrl+F5 |

---

## Полезные команды

```bash
# Docker
docker compose ps                         # состояние контейнеров
docker compose logs -f                    # живой лог сайта и бота
docker compose up -d --force-recreate     # пересоздать (перечитать .env)
cd /opt/coffee_bot && ./deploy/deploy.sh  # обновить сайт из GitHub

# systemd (путь без Docker)
sudo systemctl status coffee-bot
sudo systemctl restart coffee-bot
sudo journalctl -u coffee-bot -f

# общее
sudo nginx -t && sudo systemctl reload nginx
sudo certbot renew                        # продлить сертификат
```
