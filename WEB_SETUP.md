# Coffee Quality Web

The primary entry point is now a FastAPI web service. It uses the existing `coffee_quality.db` tables for staff accounts and drink reviews. Schedule and checklist modules remain in the legacy bot code and are not exposed by the web service.

## Local setup

Install dependencies and configure the Telegram bot username. The existing `credentials.json` may continue to provide the bot token; `TELEGRAM_BOT_TOKEN` can be used instead.

```powershell
pip install -r requirements.txt
$env:TELEGRAM_BOT_USERNAME = "your_bot_username"
$env:WEB_SESSION_SECRET = "a-long-random-secret"
$env:WEB_HOST = "127.0.0.1"
$env:WEB_PORT = "8000"
$env:WEB_COOKIE_SECURE = "0"
python run.py
```

Open `http://127.0.0.1:8000`. For production, set `WEB_COOKIE_SECURE=1`, provide a stable random `WEB_SESSION_SECRET`, run behind HTTPS, and configure the public domain for the bot's Telegram Login Widget with BotFather. Set `TELEGRAM_BOT_TOKEN` in the service environment rather than copying it into source files.

Only active staff accounts already present in `users` can sign in. The account is matched by Telegram ID first, then by the existing Telegram username; a verified username match is linked to the Telegram ID. Senior and mentor roles can manage staff accounts. The web service does not initialize or migrate the database at startup.

## Current web areas

- Telegram Login Widget sign-in and signed, HttpOnly session cookies.
- Review overview with period filters, weekly activity, staff averages, and recent evaluations.
- Staff account and role management for senior and mentor users.

Schedule and checklist experiences are intentionally excluded until their planned rewrite.
