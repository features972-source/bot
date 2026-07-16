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

### `BOT_TOKEN is not set` on deploy

You likely have **two** P1 services from the same GitHub repo:

| Service | Status |
|---------|--------|
| **`p1-bot-m9an`** (or `p1-bot`) | ✅ Working — has secrets in Render env |
| **`p1-telegram-bot`** | ❌ Blueprint auto-deploy — secrets are `sync: false`, never filled in |

**Fix (pick one):**

1. **Recommended:** Render dashboard → open the **failing** `p1-telegram-bot` service → **Suspend** (or Delete). Keep `p1-bot-m9an`.
2. **Or sync secrets:** From repo root, with [Render API key](https://dashboard.render.com/u/settings/api-keys):
   ```powershell
   $env:RENDER_API_KEY = "rnd_..."
   powershell -File scripts/ready-p1-render.ps1
   ```
   This copies `BOT_TOKEN`, SSH key, etc. from `.env.press1` and suspends broken duplicates.

Campaigns use the **healthy** URL only: `https://p1-bot-m9an.onrender.com/health` must return `"ok": true`.

## Telegram commands

`/start` opens **THE FLOOR** (operator pad). `/go` preflight+launch · `/pulse` heat intel · `/run` `/stop` `/testcall` — plus MP3/voice (audio) or numbers/CSV (leads).

Pushes that only change files **outside** `p1-telegram-bot/` will not redeploy this service if you set a build filter (see `render.yaml`).
