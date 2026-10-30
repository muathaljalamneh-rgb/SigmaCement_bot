# 🏭 Cement Plant Production Bot

A Telegram bot that answers questions about your monthly cement production reports.

## Features
- Upload Excel (.xlsx) production reports directly to Telegram
- Ask questions in Arabic or English
- Compare data across multiple months
- Detects anomalies and out-of-range values
- Maintains conversation context per user
- Supports team of 2-5 users

## Setup (5 minutes)

### Step 1 — Create Telegram Bot
1. Open Telegram → search **@BotFather**
2. Send `/newbot`
3. Choose a name: e.g. `Cement Plant Bot`
4. Choose a username: e.g. `CementPlantBot`
5. Copy the **token** (looks like: `7123456789:AAHxxxxx`)

### Step 2 — Get Anthropic API Key
1. Go to **console.anthropic.com**
2. Sign up / log in
3. Go to **API Keys** → **Create Key**
4. Copy the key (starts with `sk-ant-...`)

### Step 3 — Get Your Telegram User IDs
1. Open Telegram → search **@userinfobot**
2. Send `/start`
3. It shows your user ID (e.g. `123456789`)
4. Repeat for each team member

### Step 4 — Deploy on Railway (Free)
1. Go to **railway.app** → Sign up with GitHub
2. Click **New Project** → **Deploy from GitHub repo**
   - Or use **Deploy from template** if you don't want GitHub
3. Upload this folder OR connect your GitHub repo
4. Go to **Variables** tab and add:
   ```
   TELEGRAM_TOKEN     = (your token from Step 1)
   ANTHROPIC_API_KEY  = (your key from Step 2)
   ALLOWED_USER_IDS   = 123456789,987654321  (comma-separated, no spaces)
   ```
5. Click **Deploy** — done!

### Alternative: Run Locally
```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your values
export $(cat .env | xargs)
python bot.py
```

## Usage

| Action | How |
|--------|-----|
| Upload report | Send .xlsx file to the bot |
| Ask a question | Just type it (Arabic or English) |
| See loaded reports | `/reports` |
| Clear chat history | `/clear` |
| Get help | `/help` |

## Example Questions
- "What was the total M50 production in April?"
- "Which days had SPC above average for Super white?"
- "Compare Blaine values between months"
- "ما هي أيام التوقف الطارئة؟"
- "كم بلغ متوسط البياض لـ Super white؟"
- "هل في أيام كانت نسبة الكلنكر أقل من المعتاد؟"

## Daily mill report (التقرير اليومي)

Every morning the bot builds one report per **production day (08:00 → 08:00)** from the
department workbooks and publishes it as an Arabic web dashboard.

**Upload on Telegram** (admin, `UPLOADER_IDS`, or by default every `ALLOWED_USER_IDS` user) — the bot recognises each file by its content,
names do not matter, and replies with what is still missing:

| Daily | Mill production (month workbook), Mill Operation Process (DCS), Mill product QC, Raw material acquisition, Clinker material, Additive materials, Daily sales |
|---|---|
| Once / when changed | Annual plan, mill fan table |

**Schedule (Asia/Amman):** `REMINDER_TIME` (09:30) reminds the uploaders of missing files;
`REPORT_TIME` (10:00) builds the report and sends the summary + dashboard link to
`REPORT_NOTIFY_IDS`. A file uploaded later rebuilds the report and sends an update.
`/daily` or `/daily 25-8-2026` returns the summary on demand.
`/day 25-8-2026` pins a working day: every upload (including the cumulative QC/truck-scale/lab
workbooks) counts for that day and the morning messages pause; `/day auto` resumes the schedule.
The dashboard is self-contained: a date picker is always shown; a day whose production data
(mill workbook) or daily DCS/sales file was uploaded shows its report, any other day shows
"no files for this day". Reports are built on demand and rebuilt when a newer file arrives.

**Dashboard:** served by the same Railway service on `$PORT`. In Railway → *Settings → Networking*
click **Generate Domain** once. The bot's messages carry a private link (`?k=…`) that signs the
browser in; `DASHBOARD_PASSWORD` optionally adds a password login. `PUBLIC_URL` overrides
`RAILWAY_PUBLIC_DOMAIN`. The summary goes to `REPORT_NOTIFY_IDS` (default: admin + uploaders).

The checks implement Eng. Imad's *Analyses* specification: counter chains (including a product
made twice in a day), the 24-hour rule and repeated stoppages, flushing losses, month-to-date vs
plan and spare stop days, stock weeks and truck-scale receipts, moisture vs lab, wet-basis
proportions and clinker source vs QC, grinding-aid dosage (ml/min → g/t), exact R45/Blaine/L*
matching, the six process charts with alarms, silo filling and truck loading vs sales, and the
monthly strength / setting / SO3 trends. Code: `daily/` · tests: `pytest tests`.

## Notes
- Reports are stored **in memory** — if the bot restarts, re-upload reports
- For persistent storage, a future version can use a database
- The bot keeps the last 20 messages of conversation context per user
