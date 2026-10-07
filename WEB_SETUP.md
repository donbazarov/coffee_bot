# Coffee Quality Web

The primary entry point is now a FastAPI web service. It uses the existing `coffee_quality.db` tables for staff accounts and drink reviews. Schedule and checklist modules remain in the legacy bot code and are not exposed by the web service. The web service does not start Telegram polling or send bot messages.

## Local setup

Telegram's website login requires a bot registered in BotFather as the site's Telegram Login identity, with the public domain linked there. This bot is only the identity-provider configuration; the web app does not use its commands or messaging. Configure the bot username and token. The existing `credentials.json` may continue to provide the token; `TELEGRAM_BOT_TOKEN` can be used instead.

```powershell
pip install -r requirements.txt
$env:TELEGRAM_BOT_USERNAME = "NeftCoffeeBot"
$env:TELEGRAM_AUTH_CALLBACK_URL = "https://your-public-host/auth/telegram/callback"
$env:WEB_SESSION_SECRET = "a-long-random-secret"
$env:WEB_HOST = "127.0.0.1"
$env:WEB_PORT = "8000"
$env:WEB_COOKIE_SECURE = "0"
python run.py
```

Open `http://127.0.0.1:8000` for local preview. If port 8000 is already occupied, set `WEB_PORT=8001`, open `http://127.0.0.1:8001`, and point the HTTPS tunnel at port 8001 as well. Telegram login requires a public HTTPS callback, including during local development; set `TELEGRAM_AUTH_CALLBACK_URL` to that tunnel URL. Register the same hostname for Telegram Login with BotFather. For production, set `WEB_COOKIE_SECURE=1`, provide a stable random `WEB_SESSION_SECRET`, run behind HTTPS, and set the callback URL to the public HTTPS callback. Set `TELEGRAM_BOT_TOKEN` in the service environment rather than copying it into source files.

The login widget redirects to Telegram for authentication, then returns to `/auth/telegram/callback`. It does not request permission to message the user. After Telegram's signature and account are verified, the site sets a persistent seven-day HttpOnly session cookie. Login first matches the Telegram username; a matching account is linked to the verified Telegram ID. If the username changed, the saved Telegram ID is used as a fallback and the account's username is refreshed.

Unknown Telegram accounts are stored in `users` as inactive `guest` records with their verified Telegram ID and profile name. They cannot enter the dashboard. Senior and mentor users can review them in **Команда**: edit a guest to assign a staff role, then grant access. Existing iiko IDs are retained in `iiko_id`; a one-time startup migration backs up the SQLite database, moves any legacy values from `telegram_id` into empty `iiko_id` fields, and clears the legacy column values. The migration is idempotent and stops without changing records if it detects conflicting IDs.

## Shift calendar

The calendar reads the existing `schedule` and `shift_types` tables. It opens in view mode for every active staff member. Baristas, seniors, and mentors can all enter edit mode, change any employee's cells, copy/paste a rectangular range, and save the complete draft at once. Saving records each created, changed, or cleared cell in `web_shift_change_log`, including who changed it and the before/after shift values. There is no approval step for changes. The calendar edit API writes only to the local SQLite database; it does not write back to Google Sheets.

The **Личный кабинет** page stores per-user switches for the quality and calendar modules. Seniors and mentors also have a **Показывать сайт как бариста** preview switch; preview changes the visible controls only and does not change the account's real permissions.

The month summary treats elapsed scheduled time as worked time; it is a schedule estimate, not attendance tracking. `WEB_TIMEZONE` controls local shift times and calendar export and defaults to `Europe/Moscow`. The **Мой календарь** link is a five-year signed iCalendar subscription URL that can be added to Apple Calendar, Google Calendar, or another calendar client.

## Guest stories

The public guest-facing page lives at `/stories` and is intentionally absent from the staff navigation — guests reach it through a direct link or QR code. It has no authentication: anyone with the URL can read the published carousel and submit one story per device (tracked by the HttpOnly `neft_guest` cookie, stored only as a SHA-256 hash).

Moderation moved out of the standalone prototype and into the mentor's **Личный кабинет**: seniors and mentors see a **Истории гостей** panel where they can publish, edit, and delete stories. Access uses the normal site session plus CSRF token, so the old `Neft_moderation_key` private link is gone. Submitted stories stay hidden until published.

Data lives in `data/stories/` (`db.json` plus `uploads/`), overridable with `NEFT_DATA_DIR`. A one-time idempotent migration on startup rewrites legacy `/uploads/...` photo paths to `/stories/uploads/...` and removes the obsolete moderation key.

## Current web areas

- Telegram Login Widget sign-in and signed, HttpOnly session cookies.
- Review overview with period filters, weekly activity, staff averages, and recent evaluations.
- Staff, pending guest, and role management for senior and mentor users.
- Native shift calendar with direct editing, change history, monthly hours, and iCalendar subscriptions.
- Per-user module preferences and mentor barista-preview mode.
- Guest stories carousel at `/stories` with moderation in the personal cabinet.

Checklist experience remains excluded until its planned rewrite.

## Deployment

See `README-DEPLOY.md` for the VDS setup walkthrough (systemd, nginx, HTTPS, backups, and GitHub-based updates). The site runs as a single uvicorn worker because SQLite and the stories JSON file are not safe for concurrent writers.
