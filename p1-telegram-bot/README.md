# P1 Press-1 Telegram Bot

**Separate from Q1 / Q2 / Credo** — own folder, own Render Web Service, own bot token.

## Create on Render (new Web Service)

1. Render dashboard → **New** → **Web Service**
2. Connect repo `features972-source/bot`, branch `main`
3. **Name:** `p1-telegram-bot`
4. **Root Directory:** `p1-telegram-bot` ← important
5. **Runtime:** Docker (uses `Dockerfile` in this folder)
6. **Health check path:** `/health`
7. Environment variables:
   - `BOT_TOKEN` — your P1 Telegram bot token
   - `TELEGRAM_ALLOWED_IDS` — your Telegram user id
   - `VICIDIAL_SSH_KEY` — SSH private key for `206.189.118.204` (use `\n` for newlines)
8. Deploy

URL: `https://p1-bot.onrender.com/health` or `https://p1-telegram-bot.onrender.com/health`

**Important:** Use **one** Render Web Service for this bot. If you have both `p1-bot` and `p1-telegram-bot`, suspend/delete one — same `BOT_TOKEN` causes Telegram Conflict errors.

## Telegram commands

`/start` `/status` `/run` `/stop` `/testcall` — plus send MP3/voice (audio) or numbers/CSV (leads).

Pushes that only change files **outside** `p1-telegram-bot/` will not redeploy this service if you set a build filter (see `render.yaml`).
