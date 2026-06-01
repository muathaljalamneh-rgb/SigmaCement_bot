import os
import io
import logging
import json
from datetime import datetime

import anthropic
import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)
import pandas as pd

logging.basicConfig(format='%(asctime)s | %(levelname)s | %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USERS  = [int(x) for x in os.environ.get("ALLOWED_USER_IDS","").split(",") if x.strip()]
ADMIN_USER_ID  = int(os.environ.get("ADMIN_USER_ID","0"))
DATABASE_URL   = os.environ.get("DATABASE_URL","")

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── Database ──────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    month_key   TEXT PRIMARY KEY,
                    filename    TEXT,
                    raw_text    TEXT,
                    summary     TEXT,
                    uploaded_at TEXT
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    user_id     BIGINT,
                    role        TEXT,
                    content     TEXT,
                    created_at  TIMESTAMP DEFAULT NOW()
                );
            """)
        conn.commit()
    logger.info("DB initialized ✅")

def save_report(month_key, filename, raw_text, summary):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO reports (month_key, filename, raw_text, summary, uploaded_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (month_key) DO UPDATE SET
                    filename=EXCLUDED.filename,
                    raw_text=EXCLUDED.raw_text,
                    summary=EXCLUDED.summary,
                    uploaded_at=EXCLUDED.uploaded_at
            """, (month_key, filename, raw_text, summary, datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()

def load_all_reports():
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM reports ORDER BY month_key")
            return {r['month_key']: dict(r) for r in cur.fetchall()}

def save_message(user_id, role, content):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO conversations (user_id, role, content)
                VALUES (%s, %s, %s)
            """, (user_id, role, content))
        conn.commit()

def load_history(user_id, limit=20):
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT role, content FROM conversations
                WHERE user_id = %s
                ORDER BY created_at DESC LIMIT %s
            """, (user_id, limit))
            rows = cur.fetchall()
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def clear_history_db(user_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM conversations WHERE user_id = %s", (user_id,))
        conn.commit()

# ── Helpers ───────────────────────────────────────────────
def is_allowed(uid): return not ALLOWED_USERS or uid in ALLOWED_USERS
def is_admin(uid):   return uid == ADMIN_USER_ID

def get_reports_summary(reports):
    if not reports: return "No reports uploaded yet."
    return "\n".join([f"- {m}: {d['filename']} (uploaded {d['uploaded_at']})"
                      for m, d in sorted(reports.items())])

def extract_excel(file_bytes, filename):
    try:
        xl = pd.ExcelFile(io.BytesIO(file_bytes))
        parts = [f"FILE: {filename}\nSHEETS: {', '.join(xl.sheet_names)}"]
        for sheet in ['SUMMARY Monthly performance','DATA','Power','Stock','PI']:
            if sheet in xl.sheet_names:
                try:
                    df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet, header=None)
                    parts.append(f"\n=== {sheet} ===\n{df.fillna('').to_string(max_rows=80,max_cols=20)}")
                except: pass
        for sheet in [s for s in xl.sheet_names if s.startswith('Daily report')][:5]:
            try:
                df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet, header=None)
                parts.append(f"\n=== {sheet} ===\n{df.fillna('').to_string(max_rows=40,max_cols=15)}")
            except: pass
        text = "\n".join(parts)
        return text[:50000] + "\n...[truncated]" if len(text) > 50000 else text
    except Exception as e:
        return f"Error: {e}"

def detect_month(filename):
    import re
    months = {'jan':'01','feb':'02','mar':'03','apr':'04','may':'05','jun':'06',
              'jul':'07','aug':'08','sep':'09','oct':'10','nov':'11','dec':'12',
              'january':'01','february':'02','march':'03','april':'04','june':'06',
              'july':'07','august':'08','september':'09','october':'10','november':'11','december':'12'}
    fn = filename.lower()
    m = re.search(r'(\d{4})[_\-](\d{2})', fn)
    if m: return f"{m.group(1)}-{m.group(2)}"
    for name, num in months.items():
        if name in fn:
            yr = re.search(r'(\d{4})', fn)
            if yr: return f"{yr.group(1)}-{num}"
    return datetime.now().strftime("%Y-%m")

SYSTEM = """You are an expert cement production analyst for a cement plant.
Answer questions about monthly production reports.
- Topics: tonnage, SPC, Blaine, R45, whiteness, raw material proportions, moisture, stoppages
- Compare across months when multiple reports are available
- Flag anomalies and out-of-range values
- Reply in the same language as the question (Arabic or English)
- Be precise, cite specific days and values

Available reports:
{reports_summary}

{reports_data}"""

# ── Handlers ──────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied."); return
    reports = load_all_reports()
    status = f"📊 {len(reports)} report(s) available" if reports else "📭 No reports yet"
    await update.message.reply_text(
        f"👋 Hello {update.effective_user.first_name}!\n\n"
        "🏭 *Cement Plant Production Assistant*\n\n"
        f"{status}\n\n"
        "*Example questions:*\n"
        "• What was M50 total production in April?\n"
        "• Which days had SPC above average?\n"
        "• ما هي أيام التوقف الطارئة؟\n"
        "• قارن Blaine بين الأشهر\n\n"
        "/reports — see all reports\n"
        "/clear — reset conversation",
        parse_mode='Markdown')

async def list_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied."); return
    reports = load_all_reports()
    if not reports:
        await update.message.reply_text("📭 No reports available yet."); return
    text = "📋 *Available Reports:*\n\n"
    for month, data in sorted(reports.items()):
        text += f"📅 `{month}` — {data['filename']}\n"
        text += f"   _{data['uploaded_at']}_\n\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    clear_history_db(update.effective_user.id)
    await update.message.reply_text("🗑️ Conversation cleared!")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("⛔ Access denied."); return
    if not is_admin(uid):
        await update.message.reply_text("⛔ Only the administrator can upload reports."); return

    doc = update.message.document
    if not doc.file_name.endswith('.xlsx'):
        await update.message.reply_text("⚠️ Please send an .xlsx file."); return

    await update.message.reply_text("⏳ Processing report... please wait.")
    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await file.download_as_bytearray())
        month_key = detect_month(doc.file_name)
        raw_text  = extract_excel(file_bytes, doc.file_name)

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=600,
            messages=[{"role":"user","content":
                f"Summarize this cement production report in 5 bullet points (key numbers only):\n\n{raw_text[:8000]}"}])
        summary = resp.content[0].text

        save_report(month_key, doc.file_name, raw_text, summary)

        await update.message.reply_text(
            f"✅ *Report saved permanently:* `{doc.file_name}`\n"
            f"📅 *Period:* {month_key}\n\n"
            f"*Summary:*\n{summary}",
            parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("⛔ Access denied."); return

    reports = load_all_reports()
    if not reports:
        await update.message.reply_text("📭 No reports available yet."); return

    reports_data = ""
    for month, data in sorted(reports.items()):
        reports_data += f"\n\n{'='*50}\nREPORT: {month} ({data['filename']})\n{'='*50}\n"
        reports_data += data['raw_text'][:15000]

    system = SYSTEM.format(
        reports_summary=get_reports_summary(reports),
        reports_data=reports_data)

    history = load_history(uid)
    history.append({"role":"user","content":update.message.text})
    save_message(uid, "user", update.message.text)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=1000,
            system=system, messages=history)
        answer = response.content[0].text
        save_message(uid, "assistant", answer)

        if len(answer) > 4000:
            for i in range(0, len(answer), 4000):
                await update.message.reply_text(answer[i:i+4000])
        else:
            await update.message.reply_text(answer)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("reports", list_reports))
    app.add_handler(CommandHandler("clear",   clear_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🤖 Cement Bot running with persistent DB...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
