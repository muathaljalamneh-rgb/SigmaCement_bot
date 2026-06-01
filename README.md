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

## Notes
- Reports are stored **in memory** — if the bot restarts, re-upload reports
- For persistent storage, a future version can use a database
- The bot keeps the last 20 messages of conversation context per user
