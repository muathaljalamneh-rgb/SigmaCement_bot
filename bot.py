import os
import io
import json
import logging
import tempfile
from pathlib import Path
from datetime import datetime

import anthropic
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)
import pandas as pd
import numpy as np

# ── Logging ───────────────────────────────────────────────
logging.basicConfig(
    format='%(asctime)s | %(levelname)s | %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USERS    = [int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()]

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── In-memory report store ─────────────────────────────────
# { "2026-04": {"filename": "...", "summary": "...", "raw_text": "..."} }
reports: dict = {}
conversation_history: dict = {}  # user_id -> list of messages

SYSTEM_PROMPT = """You are an expert cement production analyst assistant for a cement plant.
You have access to monthly production reports that have been uploaded by the team.

Your role:
- Answer questions about production data (tonnage, SPC, Blaine, R45, whiteness, raw material proportions, moisture, stoppages)
- Compare data across months when multiple reports are available
- Flag anomalies and out-of-range values
- Give concise, actionable insights
- Support both Arabic and English questions — always reply in the same language as the question

Available reports: {reports_summary}

When answering, cite specific days and values from the data. Be precise and direct.
If data is not available for a question, say so clearly."""

def get_reports_summary() -> str:
    if not reports:
        return "No reports uploaded yet."
    lines = []
    for month, data in sorted(reports.items()):
        lines.append(f"- {month}: {data['filename']} (uploaded {data['uploaded_at']})")
    return "\n".join(lines)

def extract_excel_text(file_bytes: bytes, filename: str) -> str:
    """Extract key data from Excel report into structured text."""
    try:
        xl = pd.ExcelFile(io.BytesIO(file_bytes))
        sheets_text = []
        
        priority_sheets = ['SUMMARY Monthly performance', 'DATA', 'Power', 'Stock', 'PI']
        all_sheets = xl.sheet_names
        
        # Process summary and key sheets first
        for sheet in priority_sheets:
            if sheet in all_sheets:
                try:
                    df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet, header=None)
                    text = f"\n=== Sheet: {sheet} ===\n"
                    text += df.fillna('').to_string(max_rows=80, max_cols=20)
                    sheets_text.append(text)
                except Exception as e:
                    logger.warning(f"Could not read sheet {sheet}: {e}")
        
        # Add a sample of daily reports (first 5)
        daily_sheets = [s for s in all_sheets if s.startswith('Daily report')][:5]
        for sheet in daily_sheets:
            try:
                df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet, header=None)
                text = f"\n=== Sheet: {sheet} ===\n"
                text += df.fillna('').to_string(max_rows=40, max_cols=15)
                sheets_text.append(text)
            except Exception as e:
                logger.warning(f"Could not read sheet {sheet}: {e}")
        
        full_text = f"REPORT FILE: {filename}\nSHEETS AVAILABLE: {', '.join(all_sheets)}\n"
        full_text += "\n".join(sheets_text)
        
        # Truncate to ~50k chars to stay within context limits
        if len(full_text) > 50000:
            full_text = full_text[:50000] + "\n... [truncated for context limit]"
        
        return full_text
    except Exception as e:
        return f"Error reading Excel file: {str(e)}"

def detect_month_from_filename(filename: str) -> str:
    """Try to detect year-month from filename."""
    import re
    # Patterns: april-2026, 2026-04, Apr_2026, etc.
    months_map = {
        'jan':'01','feb':'02','mar':'03','apr':'04','may':'05','jun':'06',
        'jul':'07','aug':'08','sep':'09','oct':'10','nov':'11','dec':'12',
        'january':'01','february':'02','march':'03','april':'04','june':'06',
        'july':'07','august':'08','september':'09','october':'10','november':'11','december':'12',
    }
    fn = filename.lower()
    # Try YYYY-MM
    m = re.search(r'(\d{4})[_\-](\d{2})', fn)
    if m: return f"{m.group(1)}-{m.group(2)}"
    # Try month-name + year
    for name, num in months_map.items():
        if name in fn:
            yr = re.search(r'(\d{4})', fn)
            if yr: return f"{yr.group(1)}-{num}"
    return datetime.now().strftime("%Y-%m")

async def check_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True  # No restriction if env var not set
    if update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text("⛔ Access denied. Contact the administrator.")
        return False
    return True

# ── Handlers ──────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_allowed(update): return
    name = update.effective_user.first_name
    text = (
        f"👋 Hello {name}!\n\n"
        "I'm your **Cement Plant Production Assistant**.\n\n"
        "📊 **What I can do:**\n"
        "• Answer questions about production data\n"
        "• Analyse monthly reports you upload\n"
        "• Compare performance across months\n"
        "• Flag anomalies and out-of-range values\n\n"
        "📁 **To get started:** Upload an Excel production report (.xlsx)\n\n"
        "💬 **Example questions:**\n"
        "• What was the average SPC for M50 in April?\n"
        "• Which days had Blaine below the minimum?\n"
        "• Compare Super white production vs last month\n"
        "• ما هي أيام التوقف في أبريل؟\n\n"
        "Use /reports to see uploaded reports\n"
        "Use /clear to reset conversation history"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def list_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_allowed(update): return
    if not reports:
        await update.message.reply_text("📭 No reports uploaded yet.\n\nSend me an Excel file (.xlsx) to get started.")
        return
    text = "📋 **Uploaded Reports:**\n\n"
    for month, data in sorted(reports.items()):
        text += f"📅 `{month}` — {data['filename']}\n"
        text += f"   Uploaded: {data['uploaded_at']}\n\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_allowed(update): return
    uid = update.effective_user.id
    conversation_history[uid] = []
    await update.message.reply_text("🗑️ Conversation history cleared. Starting fresh!")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_allowed(update): return
    text = (
        "🔧 **Commands:**\n"
        "/start — Welcome message\n"
        "/reports — List uploaded reports\n"
        "/clear — Clear conversation history\n"
        "/help — This message\n\n"
        "📁 **Upload:** Send any .xlsx production report\n\n"
        "💬 **Ask anything** about the uploaded data — in Arabic or English"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_allowed(update): return
    doc = update.message.document
    
    if not doc.file_name.endswith('.xlsx'):
        await update.message.reply_text("⚠️ Please send an Excel file (.xlsx only).")
        return
    
    await update.message.reply_text("⏳ Processing report... please wait.")
    
    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await file.download_as_bytearray())
        
        month_key = detect_month_from_filename(doc.file_name)
        raw_text  = extract_excel_text(file_bytes, doc.file_name)
        
        # Generate a brief summary using Claude
        summary_resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": f"Briefly summarize this cement production report in 5 bullet points (key numbers only):\n\n{raw_text[:8000]}"
            }]
        )
        summary = summary_resp.content[0].text
        
        reports[month_key] = {
            "filename":    doc.file_name,
            "raw_text":    raw_text,
            "summary":     summary,
            "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        
        reply = (
            f"✅ **Report loaded:** `{doc.file_name}`\n"
            f"📅 **Period:** {month_key}\n\n"
            f"**Quick Summary:**\n{summary}\n\n"
            f"You can now ask me anything about this report!"
        )
        await update.message.reply_text(reply, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error processing document: {e}")
        await update.message.reply_text(f"❌ Error processing file: {str(e)}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_allowed(update): return
    
    uid       = update.effective_user.id
    user_text = update.message.text
    
    if not reports:
        await update.message.reply_text(
            "📭 No reports uploaded yet.\n\nPlease send an Excel production report (.xlsx) first."
        )
        return
    
    # Build context from all reports
    reports_context = ""
    for month, data in sorted(reports.items()):
        reports_context += f"\n\n{'='*60}\nREPORT: {month} ({data['filename']})\n{'='*60}\n"
        reports_context += data['raw_text'][:15000]  # per report limit
    
    system = SYSTEM_PROMPT.format(reports_summary=get_reports_summary())
    system += f"\n\n{reports_context}"
    
    # Maintain conversation history per user
    if uid not in conversation_history:
        conversation_history[uid] = []
    
    conversation_history[uid].append({
        "role":    "user",
        "content": user_text
    })
    
    # Keep last 20 turns
    if len(conversation_history[uid]) > 20:
        conversation_history[uid] = conversation_history[uid][-20:]
    
    # Show typing indicator
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            system=system,
            messages=conversation_history[uid]
        )
        
        answer = response.content[0].text
        
        conversation_history[uid].append({
            "role":    "assistant",
            "content": answer
        })
        
        # Telegram max message length is 4096
        if len(answer) > 4000:
            for i in range(0, len(answer), 4000):
                await update.message.reply_text(answer[i:i+4000])
        else:
            await update.message.reply_text(answer)
            
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        await update.message.reply_text(f"❌ Error getting response: {str(e)}")

# ── Main ──────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("reports", list_reports))
    app.add_handler(CommandHandler("clear",   clear_history))
    app.add_handler(CommandHandler("help",    help_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("🤖 Cement Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

