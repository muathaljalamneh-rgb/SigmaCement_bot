import os
import io
import logging
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
import numpy as np

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
                    structured  TEXT,
                    summary     TEXT,
                    uploaded_at TEXT
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    user_id    BIGINT,
                    role       TEXT,
                    content    TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
        conn.commit()
    logger.info("DB ready ✅")

def save_report(month_key, filename, structured, summary):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO reports (month_key,filename,structured,summary,uploaded_at)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (month_key) DO UPDATE SET
                    filename=EXCLUDED.filename,
                    structured=EXCLUDED.structured,
                    summary=EXCLUDED.summary,
                    uploaded_at=EXCLUDED.uploaded_at
            """, (month_key, filename, structured, summary,
                  datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()

def load_all_reports():
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM reports ORDER BY month_key")
            return {r['month_key']: dict(r) for r in cur.fetchall()}

def save_message(user_id, role, content):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO conversations (user_id,role,content) VALUES (%s,%s,%s)",
                        (user_id, role, content))
        conn.commit()

def load_history(user_id, limit=16):
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT role,content FROM conversations
                WHERE user_id=%s ORDER BY created_at DESC LIMIT %s""",
                (user_id, limit))
            return [{"role":r["role"],"content":r["content"]}
                    for r in reversed(cur.fetchall())]

def clear_history_db(user_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM conversations WHERE user_id=%s",(user_id,))
        conn.commit()

# ── Excel parser — extracts structured daily data ─────────
def safe_float(x):
    try:
        v = float(x)
        return None if (v != v) else v  # NaN check
    except:
        return None

def extract_structured(file_bytes, filename):
    """Extract all daily data into a rich structured text."""
    xl = pd.ExcelFile(io.BytesIO(file_bytes))
    lines = [f"=== REPORT: {filename} ===",
             f"Sheets available: {', '.join(xl.sheet_names)}\n"]

    products = ['Power white','Super white','Eco white','CEM I 52,5 R','M50']

    # ── Daily reports: all 31 sheets ──────────────────────
    lines.append("=== DAILY PRODUCTION DATA ===")
    daily_rows = []
    for sheet in sorted([s for s in xl.sheet_names if s.startswith('Daily report')],
                        key=lambda s: int(s.replace('Daily report','').strip())):
        day = int(sheet.replace('Daily report','').strip())
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet, header=None)
            headers = df.iloc[0].tolist()
            for col_idx, header in enumerate(headers):
                if header not in products:
                    continue
                prod_t  = safe_float(df.iloc[1, col_idx])
                hours   = safe_float(df.iloc[2, col_idx])
                spc_md  = safe_float(df.iloc[3, col_idx])
                spc_tot = safe_float(df.iloc[4, col_idx])
                ck      = safe_float(df.iloc[9, col_idx])
                blaine  = safe_float(df.iloc[11, col_idx])
                r45     = safe_float(df.iloc[12, col_idx])
                wi      = safe_float(df.iloc[14, col_idx])
                if prod_t and prod_t > 0:
                    tph = round(prod_t/hours, 2) if hours and hours > 0 else None
                    row = (f"Day {day:02d} | {header:20s} | "
                           f"Prod={prod_t:.1f}t | Hours={hours:.1f}h | "
                           f"t/h={tph or '-'} | "
                           f"SPC_mill={spc_md or '-'} | SPC_plant={spc_tot or '-'} | "
                           f"C/K={ck or '-'} | Blaine={blaine or '-'} | "
                           f"R45={r45 or '-'} | Whiteness={wi or '-'}")
                    daily_rows.append(row)
        except Exception as e:
            logger.warning(f"Sheet {sheet}: {e}")

    lines += daily_rows

    # ── Power sheet ───────────────────────────────────────
    if 'Power' in xl.sheet_names:
        lines.append("\n=== POWER CONSUMPTION ===")
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name='Power', header=None)
            lines.append(df.fillna('').to_string(max_rows=80, max_cols=15))
        except: pass

    # ── Stock sheet ───────────────────────────────────────
    if 'Stock' in xl.sheet_names:
        lines.append("\n=== RAW MATERIAL STOCK ===")
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name='Stock', header=None)
            lines.append(df.fillna('').to_string(max_rows=60, max_cols=15))
        except: pass

    # ── PI sheet ──────────────────────────────────────────
    if 'PI' in xl.sheet_names:
        lines.append("\n=== PERFORMANCE INDICATORS (STOPPAGES) ===")
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name='PI', header=None)
            lines.append(df.fillna('').to_string(max_rows=30, max_cols=35))
        except: pass

    # ── Summary sheet ─────────────────────────────────────
    for sname in ['SUMMARY Monthly performance','Summary','SUMMARY']:
        if sname in xl.sheet_names:
            lines.append(f"\n=== MONTHLY SUMMARY ===")
            try:
                df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sname, header=None)
                lines.append(df.fillna('').to_string(max_rows=60, max_cols=20))
            except: pass
            break

    # ── DATA sheet — proportions & moisture ───────────────
    if 'DATA' in xl.sheet_names:
        lines.append("\n=== RAW MATERIAL PROPORTIONS & MOISTURE (DATA sheet) ===")
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name='DATA', header=None)
            lines.append(df.fillna('').to_string(max_rows=200, max_cols=35))
        except: pass

    full = "\n".join(lines)
    # Store up to 120k chars — covers full month in detail
    return full[:120000]

def detect_month(filename):
    import re
    months = {'jan':'01','feb':'02','mar':'03','apr':'04','may':'05','jun':'06',
              'jul':'07','aug':'08','sep':'09','oct':'10','nov':'11','dec':'12',
              'january':'01','february':'02','march':'03','april':'04','june':'06',
              'july':'07','august':'08','september':'09','october':'10',
              'november':'11','december':'12'}
    fn = filename.lower()
    m = re.search(r'(\d{4})[_\-](\d{2})', fn)
    if m: return f"{m.group(1)}-{m.group(2)}"
    for name, num in months.items():
        if name in fn:
            yr = re.search(r'(\d{4})', fn)
            if yr: return f"{yr.group(1)}-{num}"
    return datetime.now().strftime("%Y-%m")

def is_allowed(uid): return not ALLOWED_USERS or uid in ALLOWED_USERS
def is_admin(uid):   return uid == ADMIN_USER_ID

def reports_summary_text(reports):
    if not reports: return "No reports uploaded yet."
    return "\n".join([f"- {m}: {d['filename']} ({d['uploaded_at']})"
                      for m,d in sorted(reports.items())])

SYSTEM = """You are an expert cement production analyst.
You have full access to detailed daily production data from the cement plant.

Data includes for each product each day:
- Production (tons), running hours, t/h productivity
- SPC mill and SPC plant (kWh/t)
- C/K ratio, Blaine fineness (cm2/g), R45 residue (%), Whiteness (%)
- Raw material proportions (Total Clinker %, Limestone %, Gypsum %, Pozzolana %)
- Calculated moisture % per material
- Stoppages: planned, incidents, availability %, utilization %
- Raw material stock levels

Rules:
- Reply in the SAME language as the question (Arabic or English)
- Always cite SPECIFIC days and EXACT values
- For anomalies, compare against monthly average
- All clinker types (ROY, SFW, J, RAK, ALB, M) = "Total Clinker"
- Be precise and detailed — never give vague summaries when exact data is available

Available reports:
{reports_summary}

{reports_data}"""

# ── Handlers ──────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied."); return
    reports = load_all_reports()
    status = f"📊 {len(reports)} report(s) loaded" if reports else "📭 No reports yet"
    await update.message.reply_text(
        f"👋 Hello {update.effective_user.first_name}!\n\n"
        "🏭 *Cement Plant Production Assistant*\n\n"
        f"{status}\n\n"
        "*Ask me anything — Arabic or English:*\n"
        "• أعلى 3 أيام استهلاك كهرباء لـ M50؟\n"
        "• Which days had Blaine below minimum?\n"
        "• ما نسبة الكلنكر يوم 15 في Super white؟\n"
        "• Compare SPC across all products\n\n"
        "/reports — list loaded reports\n"
        "/clear — reset conversation",
        parse_mode='Markdown')

async def list_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied."); return
    reports = load_all_reports()
    if not reports:
        await update.message.reply_text("📭 No reports yet."); return
    text = "📋 *Loaded Reports:*\n\n"
    for m,d in sorted(reports.items()):
        text += f"📅 `{m}` — {d['filename']}\n   _{d['uploaded_at']}_\n\n"
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

    await update.message.reply_text("⏳ Processing report in detail... please wait (30-60 sec).")
    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await file.download_as_bytearray())
        month_key  = detect_month(doc.file_name)
        structured = extract_structured(file_bytes, doc.file_name)

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=800,
            messages=[{"role":"user","content":
                f"Summarize this cement report in 6 bullet points with exact numbers:\n\n{structured[:10000]}"}])
        summary = resp.content[0].text

        save_report(month_key, doc.file_name, structured, summary)
        await update.message.reply_text(
            f"✅ *Saved permanently:* `{doc.file_name}`\n"
            f"📅 *Period:* {month_key}\n"
            f"📦 *Data size:* {len(structured):,} characters\n\n"
            f"*Summary:*\n{summary}",
            parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("⛔ Access denied."); return
    reports = load_all_reports()
    if not reports:
        await update.message.reply_text("📭 No reports available yet."); return

    # Build context — include full structured data per report
    reports_data = ""
    for month, data in sorted(reports.items()):
        reports_data += f"\n\n{'='*60}\nREPORT: {month} — {data['filename']}\n{'='*60}\n"
        reports_data += data.get('structured', data.get('raw_text',''))[:40000]

    system = SYSTEM.format(
        reports_summary=reports_summary_text(reports),
        reports_data=reports_data)

    history = load_history(uid)
    history.append({"role":"user","content":update.message.text})
    save_message(uid,"user",update.message.text)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=1200,
            system=system, messages=history)
        answer = response.content[0].text
        save_message(uid,"assistant",answer)
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
    logger.info("🤖 Cement Bot v2 running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
