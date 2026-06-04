import os, io, logging, re
from datetime import datetime

import anthropic
import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import pandas as pd

logging.basicConfig(format='%(asctime)s | %(levelname)s | %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USERS  = [int(x) for x in os.environ.get("ALLOWED_USER_IDS","").split(",") if x.strip()]
ADMIN_USER_ID  = int(os.environ.get("ADMIN_USER_ID","0"))
DATABASE_URL   = os.environ.get("DATABASE_URL","")

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── DB ────────────────────────────────────────────────────
def get_db(): return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    month_key TEXT PRIMARY KEY, filename TEXT,
                    raw_text TEXT, structured TEXT, summary TEXT, uploaded_at TEXT
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    user_id BIGINT, role TEXT, content TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS structured TEXT;")
        conn.commit()

def save_report(mk, fn, structured, summary):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO reports (month_key,filename,structured,summary,uploaded_at)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (month_key) DO UPDATE SET
                filename=EXCLUDED.filename, structured=EXCLUDED.structured,
                summary=EXCLUDED.summary, uploaded_at=EXCLUDED.uploaded_at""",
                (mk, fn, structured, summary, datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()

def load_all_reports():
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM reports ORDER BY month_key")
            return {r['month_key']: dict(r) for r in cur.fetchall()}

def save_msg(uid, role, content):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO conversations (user_id,role,content) VALUES (%s,%s,%s)",
                        (uid, role, content))
        conn.commit()

def load_history(uid, limit=10):
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT role,content FROM conversations
                WHERE user_id=%s ORDER BY created_at DESC LIMIT %s""", (uid, limit))
            return [{"role":r["role"],"content":r["content"]} for r in reversed(cur.fetchall())]

def clear_db(uid):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM conversations WHERE user_id=%s", (uid,))
        conn.commit()

# ── Excel parser ──────────────────────────────────────────
def safe(x):
    try:
        v = float(x)
        return None if v != v else v
    except: return None

def extract_structured(file_bytes, filename):
    xl = pd.ExcelFile(io.BytesIO(file_bytes))
    products = ['Power white','Super white','Eco white','CEM I 52,5 R','M50','M10','Flushing','flushing']
    lines = [f"REPORT: {filename}"]

    # ── SUMMARY SHEET — authoritative monthly totals ──────
    # IMPORTANT: Always use Summary for monthly totals — daily sheets may be incomplete
    summary_sheet = next((s for s in xl.sheet_names if 'summary' in s.lower()), None)
    if summary_sheet:
        try:
            df_sum = pd.read_excel(io.BytesIO(file_bytes), sheet_name=summary_sheet, header=None)
            lines.append("\n--- MONTHLY TOTALS (from Summary — USE THESE for production figures) ---")
            for i in range(2, 12):
                try:
                    prod  = str(df_sum.iloc[i, 0]) if pd.notna(df_sum.iloc[i, 0]) else ''
                    total = safe(df_sum.iloc[i, 1])
                    tph   = safe(df_sum.iloc[i, 2])
                    spc_m = safe(df_sum.iloc[i, 4])
                    spc_t = safe(df_sum.iloc[i, 5])
                    hrs   = safe(df_sum.iloc[i, 6])
                    ck    = safe(df_sum.iloc[i, 7])
                    if prod and prod not in ['nan',''] and total and total > 0:
                        lines.append(f"MONTHLY|{prod}|Total={total:.2f}t|Hours={hrs:.1f}h|"
                                     f"Avg_tph={tph:.2f}|SPC_mill={spc_m:.2f}|SPC_plant={spc_t:.2f}|CK={ck:.3f}")
                except: pass
            try:
                avail = safe(df_sum.iloc[11, 0]); util  = safe(df_sum.iloc[11, 1])
                grand = safe(df_sum.iloc[11, 2]); kwh   = safe(df_sum.iloc[11, 4])
                spc   = safe(df_sum.iloc[11, 5]); tot_h = safe(df_sum.iloc[11, 6])
                cost  = safe(df_sum.iloc[11, 7])
                if grand:
                    lines.append(f"GRAND_TOTAL|Production={grand:.2f}t|Hours={tot_h:.1f}h|"
                                 f"SPC_plant={spc:.2f}kWh/t|PowerUsed={kwh:.0f}kWh|"
                                 f"Cost={cost:.0f}JD|Availability={avail*100:.1f}%|Utilization={util*100:.1f}%")
            except: pass
            try:
                mats = df_sum.iloc[14, :12].tolist(); vals = df_sum.iloc[15, :12].tolist()
                lines.append("FINAL_STOCK|" + "|".join(
                    f"{str(m).strip()}={float(v):.1f}t" for m,v in zip(mats,vals)
                    if pd.notna(m) and pd.notna(v) and str(m) not in ['nan','']))
            except: pass
        except Exception as e:
            logger.warning(f"Summary: {e}")

    # ── Daily sheets — for detailed daily breakdown ────────
    # NOTE: these may NOT cover all days; gaps = zero/shutdown days
    lines.append("\n--- DAILY DATA (Day|Product|Prod_t|Hours|t/h|SPC_mill|SPC_plant|CK|Blaine|R45|WI) ---")
    all_sheet_days = sorted([int(s.replace('Daily report','').strip())
                             for s in xl.sheet_names if s.startswith('Daily report')])
    max_day = max(all_sheet_days) if all_sheet_days else 31
    missing_sheets = [d for d in range(1, max_day+1) if d not in all_sheet_days]
    if missing_sheets:
        lines.append(f"MISSING_DAILY_SHEETS (no report sheets): {missing_sheets}")

    for sheet in sorted([s for s in xl.sheet_names if s.startswith('Daily report')],
                        key=lambda s: int(s.replace('Daily report','').strip())):
        day = int(sheet.replace('Daily report','').strip())
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet, header=None)
            headers = df.iloc[0].tolist()
            day_has_prod = False
            for ci, hdr in enumerate(headers):
                if hdr not in products: continue
                pt = safe(df.iloc[1,ci]); hrs = safe(df.iloc[2,ci])
                if not pt or pt <= 0: continue
                day_has_prod = True
                tph = round(pt/hrs,2) if hrs and hrs>0 else '-'
                vals = [day, hdr, f"{pt:.1f}", f"{hrs:.1f}", tph,
                        safe(df.iloc[3,ci]) or '-', safe(df.iloc[4,ci]) or '-',
                        safe(df.iloc[9,ci]) or '-', safe(df.iloc[11,ci]) or '-',
                        safe(df.iloc[12,ci]) or '-', safe(df.iloc[14,ci]) or '-']
                lines.append("|".join(str(v) for v in vals))
            if not day_has_prod:
                lines.append(f"{day}|ZERO_PRODUCTION|All products = 0t")
        except Exception as e: logger.warning(f"{sheet}: {e}")

    # Power sheet
    if 'Power' in xl.sheet_names:
        lines.append("\n--- POWER SHEET ---")
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name='Power', header=None)
            lines.append(df.fillna('').to_string(max_rows=60, max_cols=12))
        except: pass

    # PI sheet
    if 'PI' in xl.sheet_names:
        lines.append("\n--- STOPPAGES (PI) ---")
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name='PI', header=None)
            lines.append(df.fillna('').to_string(max_rows=15, max_cols=35))
        except: pass

    # Stock
    if 'Stock' in xl.sheet_names:
        lines.append("\n--- STOCK ---")
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name='Stock', header=None)
            lines.append(df.fillna('').to_string(max_rows=40, max_cols=12))
        except: pass

    # DATA sheet — proportions only (rows with % data)
    if 'DATA' in xl.sheet_names:
        lines.append("\n--- PROPORTIONS & MOISTURE (DATA) ---")
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name='DATA', header=None)
            lines.append(df.fillna('').to_string(max_rows=120, max_cols=35))
        except: pass

    text = "\n".join(lines)
    return text[:80000]

def detect_month(fn):
    months = {'jan':'01','feb':'02','mar':'03','apr':'04','may':'05','jun':'06',
              'jul':'07','aug':'08','sep':'09','oct':'10','nov':'11','dec':'12',
              'january':'01','february':'02','march':'03','april':'04','june':'06',
              'july':'07','august':'08','september':'09','october':'10',
              'november':'11','december':'12'}
    f = fn.lower()
    m = re.search(r'(\d{4})[_\-](\d{2})', f)
    if m: return f"{m.group(1)}-{m.group(2)}"
    for name, num in months.items():
        if name in f:
            yr = re.search(r'(\d{4})', f)
            if yr: return f"{yr.group(1)}-{num}"
    return datetime.now().strftime("%Y-%m")

def is_allowed(uid): return not ALLOWED_USERS or uid in ALLOWED_USERS
def is_admin(uid):   return uid == ADMIN_USER_ID

SYSTEM = """You are an expert cement production analyst with access to detailed daily production data.

Data format per day: Day|Product|Production_t|Hours|t/h|SPC_mill|SPC_plant|C/K|Blaine|R45|Whiteness
All clinker types (ROY,SFW,J,RAK,ALB,M) = "Total Clinker"

STRICT RULES:
- ALWAYS reply in ENGLISH only, regardless of question language
- Always use EXACT values from the data — never approximate
- Format responses clearly:
  • Use headers with emoji: 📊 ⚡ 🏭 ⚠️ ✅ 🔴
  • Use markdown tables for rankings/comparisons
  • Bold key numbers: *59.4 kWh/t*
  • ✅ normal | ⚠️ warning | 🔴 critical
- For "worst/best days" questions: sort and show top results in a table
- Keep answers concise and structured

Available reports: {reports_summary}"""

# ── Handlers ──────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied."); return
    reports = load_all_reports()
    await update.message.reply_text(
        f"👋 Hello {update.effective_user.first_name}!\n\n"
        "🏭 *Cement Plant Production Assistant*\n\n"
        f"📊 {len(reports)} report(s) loaded\n\n"
        "*Ask anything:*\n"
        "• Worst 3 days SPC for M50?\n"
        "• Which days Blaine below minimum?\n"
        "• Compare production across months\n"
        "• Stoppage hours in April?\n\n"
        "/reports — list reports | /clear — reset chat",
        parse_mode='Markdown')

async def list_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    reports = load_all_reports()
    if not reports:
        await update.message.reply_text("📭 No reports loaded yet."); return
    text = "📋 *Loaded Reports:*\n\n"
    for m,d in sorted(reports.items()):
        text += f"📅 `{m}` — {d['filename']}\n   _{d['uploaded_at']}_\n\n"
    await update.message.reply_text(text, parse_mode='Markdown')

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    clear_db(update.effective_user.id)
    await update.message.reply_text("🗑️ Conversation cleared!")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid): await update.message.reply_text("⛔ Access denied."); return
    if not is_admin(uid): await update.message.reply_text("⛔ Only admin can upload."); return
    doc = update.message.document
    if not doc.file_name.endswith('.xlsx'):
        await update.message.reply_text("⚠️ Please send .xlsx file."); return

    await update.message.reply_text("⏳ Processing... please wait.")
    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await file.download_as_bytearray())
        mk = detect_month(doc.file_name)
        structured = extract_structured(file_bytes, doc.file_name)
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=600,
            messages=[{"role":"user","content":
                f"Summarize in 5 bullet points with exact numbers:\n\n{structured[:8000]}"}])
        summary = resp.content[0].text
        save_report(mk, doc.file_name, structured, summary)
        await update.message.reply_text(
            f"✅ Saved: {doc.file_name}\nPeriod: {mk}\n\nSummary:\n{summary}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid): await update.message.reply_text("⛔ Access denied."); return
    reports = load_all_reports()
    if not reports: await update.message.reply_text("📭 No reports loaded yet."); return

    # Build compact context — max 35k chars per report
    reports_data = ""
    for month, data in sorted(reports.items()):
        content = data.get('structured') or data.get('raw_text','')
        reports_data += f"\n\n{'='*50}\nREPORT: {month} — {data['filename']}\n{'='*50}\n"
        reports_data += content[:35000]

    system = SYSTEM.format(
        reports_summary="\n".join([f"- {m}: {d['filename']}" for m,d in sorted(reports.items())])
    ) + f"\n\n{reports_data}"

    history = load_history(uid)
    history.append({"role":"user","content":update.message.text})
    save_msg(uid,"user",update.message.text)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1000,
            system=system, messages=history)
        answer = response.content[0].text
        save_msg(uid,"assistant",answer)
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
    logger.info("🤖 Cement Bot v3 running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
