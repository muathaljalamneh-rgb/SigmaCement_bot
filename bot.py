import os
import io
import logging
from datetime import datetime

import anthropic
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)
import pandas as pd

logging.basicConfig(format='%(asctime)s | %(levelname)s | %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USERS   = [int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()]
ADMIN_USER_ID   = int(os.environ.get("ADMIN_USER_ID", "0"))  # only admin can upload

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

reports: dict = {}
conversation_history: dict = {}

SYSTEM_PROMPT = """You are an expert cement production analyst for a cement plant.
You answer questions about monthly production reports.

Your role:
- Answer questions about production data: tonnage, SPC, Blaine, R45, whiteness, raw material proportions, moisture, stoppages
- Compare data across months when multiple reports are available
- Flag anomalies and out-of-range values
- Give concise, actionable insights
- Always reply in the same language as the question (Arabic or English)

Available reports:
{reports_summary}

Be precise, cite specific days and values. If data is not available, say so clearly."""

def get_reports_summary():
    if not reports:
        return "No reports uploaded yet."
    return "\n".join([f"- {m}: {d['filename']} (uploaded {d['uploaded_at']})"
                      for m, d in sorted(reports.items())])

def extract_excel_text(file_bytes, filename):
    try:
        xl = pd.ExcelFile(io.BytesIO(file_bytes))
        sheets_text = []
        priority = ['SUMMARY Monthly performance', 'DATA', 'Power', 'Stock', 'PI']
        for sheet in priority:
            if sheet in xl.sheet_names:
                try:
                    df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet, header=None)
                    sheets_text.append(f"\n=== {sheet} ===\n{df.fillna('').to_string(max_rows=80, max_cols=20)}")
                except: pass
        for sheet in [s for s in xl.sheet_names if s.startswith('Daily report')][:5]:
            try:
                df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet, header=None)
                sheets_text.append(f"\n=== {sheet} ===\n{df.fillna('').to_string(max_rows=40, max_cols=15)}")
            except: pass
        text = f"FILE: {filename}\nSHEETS: {', '.join(xl.sheet_names)}\n" + "\n".join(sheets_text)
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

def is_allowed(uid):
    return not ALLOWED_USERS or uid in ALLOWED_USERS

def is_admin(uid):
    return uid == ADMIN_USER_ID

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return
    await update.message.reply_text(
        f"👋 Hello {update.effective_user.first_name}!\n\n"
        "🏭 *Cement Plant Production Assistant*\n\n"
        "Ask me anything about the production reports in Arabic or English.\n\n"
        "*Example questions:*\n"
        "• What was M50 total production in April?\n"
        "• Which days had SPC above average?\n"
        "• ما هي أيام التوقف الطارئة؟\n"
        "• قارن Blaine بين المنتجات\n\n"
        "Use /reports to see available reports\n"
        "Use /clear to reset your conversation",
        parse_mode='Markdown'
    )

async def list_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied.")
        return
    if not reports:
        await update.message.reply_text("📭 No reports available yet.")
        return
    text = "📋 *Available Reports:*\n\n"
    for month, data in sorted(reports.items()):
        text += f"📅 `{month}` — {data['filename']}\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    conversation_history[update.effective_user.id] = []
    await update.message.reply_text("🗑️ Conversation cleared!")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("⛔ Access denied.")
        return
    # Only admin can upload
    if not is_admin(uid):
        await update.message.reply_text("⛔ You don't have permission to upload reports.\nOnly the administrator can upload files.")
        return

    doc = update.message.document
    if not doc.file_name.endswith('.xlsx'):
        await update.message.reply_text("⚠️ Please send an Excel file (.xlsx)")
        return

    await update.message.reply_text("⏳ Processing report... please wait.")
    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await file.download_as_bytearray())
        month_key = detect_month(doc.file_name)
        raw_text  = extract_excel_text(file_bytes, doc.file_name)

        summary_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role":"user","content":f"Summarize this cement production report in 5 bullet points (key numbers only):\n\n{raw_text[:8000]}"}]
        )
        summary = summary_resp.content[0].text

        reports[month_key] = {
            "filename": doc.file_name,
            "raw_text": raw_text,
            "summary":  summary,
            "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

        await update.message.reply_text(
            f"✅ *Report loaded:* `{doc.file_name}`\n"
            f"📅 *Period:* {month_key}\n\n"
            f"*Summary:*\n{summary}\n\n"
            f"The team can now ask questions about this report!",
            parse_mode='Markdown'
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("⛔ Access denied.")
        return
    if not reports:
        await update.message.reply_text("📭 No reports available yet. Please wait for the administrator to upload a report.")
        return

    reports_context = ""
    for month, data in sorted(reports.items()):
        reports_context += f"\n\n{'='*50}\nREPORT: {month} ({data['filename']})\n{'='*50}\n"
        reports_context += data['raw_text'][:15000]

    system = SYSTEM_PROMPT.format(reports_summary=get_reports_summary()) + f"\n\n{reports_context}"

    if uid not in conversation_history:
        conversation_history[uid] = []
    conversation_history[uid].append({"role":"user","content":update.message.text})
    if len(conversation_history[uid]) > 20:
        conversation_history[uid] = conversation_history[uid][-20:]

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            system=system,
            messages=conversation_history[uid]
        )
        answer = response.content[0].text
        conversation_history[uid].append({"role":"assistant","content":answer})

        if len(answer) > 4000:
            for i in range(0, len(answer), 4000):
                await update.message.reply_text(answer[i:i+4000])
        else:
            await update.message.reply_text(answer)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("reports", list_reports))
    app.add_handler(CommandHandler("clear",   clear_history))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🤖 Cement Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
