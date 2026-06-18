# Deploy the full bot on Render (24/7)

Run all bots on Render instead of your PC. Each bot (Q1, Q2, Q1 Australia) is a **separate Web Service** with its own token, env vars, and persistent disk.

## 24/7 requirements

1. **Starter plan or higher** on each service — free tier sleeps after inactivity
2. **Persistent disk** mounted at `/data` on each service (included in `render.yaml`)
3. **Stop bots on your PC** — never run the same `BOT_TOKEN` locally and on Render
4. **Windows startup task disabled** — do not re-enable `3CX Telegram Bot` scheduled task on PC

## Before you start

1. **Stop the bot on your PC** — the same `BOT_TOKEN` cannot poll from two places.
2. **Paid plan recommended** — free tier sleeps; the bot must stay always-on for calls and payments.
3. **Persistent disk (1 GB)** — stores SQLite, Telethon sessions, and a local Excel backup at `/data/exports/q1.xlsx`.

## Option A — Blueprint (recommended)

1. Push this repo to GitHub (e.g. `features972-source/bot`).
2. In [Render Dashboard](https://dashboard.render.com/) → **New** → **Blueprint**.
3. Connect the repo — Render reads `render.yaml`.
4. Fill in secret env vars when prompted (`BOT_TOKEN`, 3CX keys, etc.).
5. Deploy. Note your service URL: `https://YOUR-APP.onrender.com`.

## Option B — Manual Web Service

1. **New → Web Service** → connect repo.
2. **Runtime:** Docker (uses `Dockerfile`).
3. **Health check path:** `/health`
4. Add a **persistent disk**: mount `/data`, 1 GB.
5. Copy env vars from `.env.render.example`.

## Required environment variables

| Variable | Notes |
|----------|--------|
| `BOT_TOKEN` | From @BotFather |
| `WEBHOOK_SECRET` | Random string |
| `ADMIN_CHAT_ID` | Your Telegram user ID |
| `NOTIFY_CHAT_ID` | Group for call/payment announcements |
| `THREECX_*` | Same as your PC `.env` |
| `MS_GRAPH_CLIENT_ID` / `MS_GRAPH_CLIENT_SECRET` | For `/syncpayments` → OneDrive |
| `PAYMENTS_ONEDRIVE_WORKSHEET` | e.g. `Payments Automatic` |

Render sets automatically: `PORT`, `RENDER_EXTERNAL_URL`, `RENDER=true`.

`LISTEN_PUBLIC_URL` defaults to your Render HTTPS URL (for live listen in Telegram).

## Q2 Call Manager (second bot, 24/7)

Q1 and Q2 share **one GitHub repo** — every push to `main` deploys the same code to each service. Q2 is **not** included in a single-service deploy; you need a **second Render Web Service**.

### Create Q2 on Render (one time)

1. **New → Web Service** → same GitHub repo (`features972-source/bot`).
2. **Name:** e.g. `q2-telegram-bot` · **Runtime:** Docker · **Branch:** `main`
3. **Health check:** `/health` · **Plan:** Starter (required for 24/7)
4. **Disk:** mount `/data`, 1 GB
5. **Environment:** copy from `.env.render.q2.example`, then paste values from your local `.env.bot2`:
   - `BOT_TOKEN` — **Q2 token only** (never reuse Q1's)
   - `NOTIFY_CHAT_ID` — **Q2 Telegram group**
   - `THREECX_*` — Q2 PBX credentials
   - `DATA_DIR=/data`, `DATABASE_PATH=/data/links-bot2.db`, `CLOUD_DEPLOYED=true`
6. Deploy. Open `https://YOUR-Q2-APP.onrender.com/health` — expect:
   ```json
   {"ok":true,"bot":"Q2 Call Manager","persistent_data":true,"payments_logged":...}
   ```
7. In the **Q2 payment group**, admin runs **`/setnotify`** once (saved to disk — survives redeploys).

### Restore Q2 database (if empty)

```powershell
curl.exe -X POST "https://YOUR-Q2-APP.onrender.com/admin/restore-db?secret=WEBHOOK_SECRET" -F "file=@C:\Users\User\3cx-telegram-bot\links-bot2.db"
```

---

## Payments not logging?

1. **Check `/health`** — `payments_logged` and `notify_chat_id` must look right.
2. **Payments only work in the notify group** — admin run **`/setnotify`** in that group (now saved to DB).
3. If you post `500 out` in the wrong group, the bot now **replies** telling you which group to use.
4. You must **reply** to the starter's notes or the bot's **ON CALL** post, then send the amount.
5. **Empty database?** Restore `links.db` / `links-bot2.db` via `/admin/restore-db` (see above).
6. **Bot down?** Render → Logs — look for crashes; confirm Starter plan (not free tier sleep).

## One-time setup after deploy

### Excel / payments

1. In Azure App Registration, add redirect URI:
   `https://YOUR-APP.onrender.com/oauth/msgraph/callback`
2. In Telegram (admin): `/excelwebauth` → open the link → sign in with Microsoft.
3. `/syncpayments` — pushes to your OneDrive `q1.xlsx` (Excel on the web).

### Mailer bridge (`/mail`)

Telethon needs a session file on the persistent disk:

1. **Locally once:** `python scripts/telethon_login.py` (with same `TELETHON_*` keys).
2. Copy `mailer-links.session` to Render:
   - Render Shell → place at `/data/mailer-links.session`
   - Or upload via SFTP if enabled.

### Migrate existing database

Copy your local `links.db` to `/data/links.db` on the disk (Render Shell or one-time upload) so payment history and links are preserved.

## What works on Render

- All Telegram commands and payment flows
- Payment streaks, leaderboards, shadow DMs, celebrations
- `/syncpayments` → Microsoft Graph → OneDrive / Excel on the web
- 3CX Call Control WebSocket (outbound from Render to your PBX)
- Live listen at `https://YOUR-APP.onrender.com/listen/...`
- Ready check, missed calls export, admin tools

## What does not work on cloud

- Local OneDrive folder sync (use Graph + `/excelwebauth` instead)
- Browser popup OAuth on localhost (use the Render callback link from `/excelwebauth`)
- Running **two instances** (Q1 + Q2) in one Render service — use separate services with separate disks and tokens if needed

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `Conflict: another process is polling` | Stop PC bot; only one deploy active |
| `/syncpayments` fails | Run `/excelwebauth`; check Azure redirect URI |
| Listen button not HTTPS | Ensure service is live; `LISTEN_PUBLIC_URL` should be Render URL |
| Mailer not connecting | Session file must exist at `/data/mailer-links.session` |
| Data lost on redeploy | Confirm persistent disk mounted at `/data` |

Health check: `https://YOUR-APP.onrender.com/health` → `{"ok": true}`
