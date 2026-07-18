import os, io, logging, re, json, asyncio
from datetime import datetime

import anthropic
import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import pandas as pd

# ── NEW: deterministic PDF report engine (guarded — a broken engine must
# never take the bot down; /report will show the error instead)
try:
    import report_engine
    ENGINE_ERR = None
except Exception as _e:
    report_engine = None
    ENGINE_ERR = repr(_e)

try:
    import insight_engine          # ── NEW: AI analytical layer (Sections 8-9)
except Exception as _e:
    insight_engine = None
    logger.warning(f"insight_engine unavailable, reports will use rule-based insights: {_e}")

logging.basicConfig(format='%(asctime)s | %(levelname)s | %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USERS  = [int(x) for x in os.environ.get("ALLOWED_USER_IDS","").split(",") if x.strip()]
ADMIN_USER_ID  = int(os.environ.get("ADMIN_USER_ID","0"))
DATABASE_URL   = os.environ.get("DATABASE_URL","")

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── NEW: seed metrics so month-on-month comparisons work from day one ─────
SEED_METRICS = {
 "2026-05": {"year":2026,"month":5,"cost":35893,"jd_per_t":4.330,"tariff":0.0796,
  "plant":{"prod":8289.3,"spc":54.37,"hours":407.2,"utilization":0.784,"availability":0.992,"kwh":450708,"avg_tph":19.79},
  "incident_h":4.6,"planned_h":140.3,"silofull_h":0,"alerts":{},"recipe_dev":{},"recipe_norm":{},
  "grey_pool":[39836,0,None],"white_pool":[13344,0,None],"stock":{},"zero_days":[8,15],"missing_days":[22],
  "products":{"Power white":{"prod":634.5,"tph":20.47,"spc_plant":51.60,"blaine":4069,"wi":91.5,"clinker":0.860},
   "Super white":{"prod":1800.7,"tph":21.26,"spc_plant":50.27,"blaine":4616,"wi":92.9,"clinker":0.704},
   "Eco white":{"prod":551.0,"tph":22.05,"spc_plant":50.68,"blaine":4940,"wi":93.9,"clinker":0.583},
   "CEM I 52.5R":{"prod":98.8,"tph":19.77,"spc_plant":52.72,"blaine":3737,"wi":91.2,"clinker":0.92},
   "M50":{"prod":5060.7,"tph":19.88,"spc_plant":55.81,"blaine":3983,"clinker":0.825},
   "M10":{"prod":143.6,"tph":20.52,"spc_plant":65.23,"blaine":4316}}},
 "2026-04": {"year":2026,"month":4,"cost":46818,"jd_per_t":4.733,"tariff":0.0796,
  "plant":{"prod":9892,"spc":59.42,"utilization":0.698,"availability":0.848},
  "incident_h":90.2,"planned_h":None,"silofull_h":0,"alerts":{},"recipe_dev":{},"recipe_norm":{},
  "grey_pool":[42122,0,None],"white_pool":[15894,0,None],"stock":{},"zero_days":[],"missing_days":[],
  "products":{"Power white":{"prod":1254.0,"tph":20.50,"spc_plant":57.20,"blaine":4082,"wi":91.9},
   "Super white":{"prod":2154.5,"tph":20.72,"spc_plant":57.60,"blaine":4693,"wi":93.2},
   "Eco white":{"prod":312.4,"tph":21.77,"spc_plant":57.20,"blaine":5154,"wi":93.9},
   "CEM I 52.5R":{"prod":126.1,"tph":20.16,"spc_plant":57.20,"blaine":4066,"wi":91.7},
   "M50":{"prod":6045.7,"tph":19.97,"spc_plant":61.70,"blaine":4063}}},
}

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
            # ── NEW: raw workbook + computed metrics for /report and /alerts
            cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS file_data BYTEA;")
            cur.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS metrics TEXT;")
            # seed April/May metrics if those months have no metrics yet
            try:
                for mk, m in SEED_METRICS.items():
                    cur.execute("""INSERT INTO reports (month_key, filename, metrics, uploaded_at)
                        VALUES (%s,%s,%s,%s)
                        ON CONFLICT (month_key) DO UPDATE SET
                        metrics = COALESCE(reports.metrics, EXCLUDED.metrics)""",
                        (mk, 'seed (management PDF figures)', json.dumps(m),
                         datetime.now().strftime("%Y-%m-%d %H:%M")))
            except Exception as e:
                logger.warning(f"seed metrics skipped: {e}")
        conn.commit()

def save_report(mk, fn, structured, summary, file_bytes=None, metrics=None):  # ── NEW args
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO reports (month_key,filename,structured,summary,uploaded_at,file_data,metrics)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (month_key) DO UPDATE SET
                filename=EXCLUDED.filename, structured=EXCLUDED.structured,
                summary=EXCLUDED.summary, uploaded_at=EXCLUDED.uploaded_at,
                file_data=COALESCE(EXCLUDED.file_data, reports.file_data),
                metrics=COALESCE(EXCLUDED.metrics, reports.metrics)""",
                (mk, fn, structured, summary, datetime.now().strftime("%Y-%m-%d %H:%M"),
                 psycopg2.Binary(file_bytes) if file_bytes else None,
                 json.dumps(metrics) if metrics else None))
        conn.commit()

def load_all_reports():
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT month_key,filename,structured,raw_text,summary,uploaded_at FROM reports ORDER BY month_key")
            return {r['month_key']: dict(r) for r in cur.fetchall()}

# ── NEW: metrics / file loaders ───────────────────────────
def load_metrics(mk):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT metrics FROM reports WHERE month_key=%s", (mk,))
            row = cur.fetchone()
            return json.loads(row[0]) if row and row[0] else None

def load_file(mk):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT file_data, filename FROM reports WHERE month_key=%s", (mk,))
            row = cur.fetchone()
            return (bytes(row[0]), row[1]) if row and row[0] else (None, None)

def load_all_metrics(exclude=None):
    """── NEW: every stored month's metrics, for multi-month AI analysis."""
    out = []
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT month_key, metrics FROM reports WHERE metrics IS NOT NULL ORDER BY month_key")
            for mk, mj in cur.fetchall():
                if exclude and mk == exclude: continue
                try: out.append(json.loads(mj))
                except Exception: pass
    return out


def latest_month_with_file():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT month_key FROM reports WHERE file_data IS NOT NULL ORDER BY month_key DESC LIMIT 1")
            row = cur.fetchone()
            return row[0] if row else None

def prev_month_key(mk, back=1):
    y, m = int(mk[:4]), int(mk[5:7])
    m -= back
    while m < 1: m += 12; y -= 1
    return f"{y}-{m:02d}"

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

def get_daily_sheets(xl):
    """Return list of (day_number, sheet_name) sorted by day — handles any naming format."""
    import re
    result = []
    for s in xl.sheet_names:
        m = re.match(r'Daily report\s+(\d+)', s, re.IGNORECASE)
        if m:
            result.append((int(m.group(1)), s)); continue
        m = re.match(r'(\d+)\s+\w+', s.strip())
        if m:
            result.append((int(m.group(1)), s)); continue
    return sorted(result)

def extract_structured(file_bytes, filename):
    xl = pd.ExcelFile(io.BytesIO(file_bytes))
    products = ['Power white','Super white','Eco white','CEM I 52,5 R','M50','M10',
                'Super white Special','Pozz-crete','Flushing','flushing']
    lines = [f"REPORT: {filename}"]

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

    lines.append("\n--- DAILY DATA (Day|Product|Prod_t|Hours|t/h|SPC_mill|SPC_plant|CK|Blaine|R45|WI) ---")
    daily_sheets = get_daily_sheets(xl)
    all_days = [d for d,_ in daily_sheets]
    max_day = max(all_days) if all_days else 31
    missing = [d for d in range(1, max_day+1) if d not in all_days]
    if missing:
        lines.append(f"MISSING_DAILY_SHEETS: {missing}")

    lines.append("\n--- STOPPAGES BY DAY (Duration_h|Department|Reason) ---")
    for day, sheet in daily_sheets:
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet, header=None)
            headers = df.iloc[0].tolist()

            day_has_prod = False
            for ci, hdr in enumerate(headers):
                hdr_s = str(hdr).strip()
                if hdr_s not in products: continue
                pt  = safe(df.iloc[1,ci]); hrs = safe(df.iloc[2,ci])
                if not pt or pt <= 0: continue
                day_has_prod = True
                tph = round(pt/hrs,2) if hrs and hrs>0 else '-'
                row_vals = [day, hdr_s, f"{pt:.1f}", f"{hrs:.1f}", tph,
                            safe(df.iloc[4,ci]) or '-', safe(df.iloc[5,ci]) or '-',
                            safe(df.iloc[9,ci]) or '-', safe(df.iloc[11,ci]) or '-',
                            safe(df.iloc[12,ci]) or '-', safe(df.iloc[14,ci]) or '-']
                lines.append("|".join(str(v) for v in row_vals))
            if not day_has_prod:
                lines.append(f"{day}|ZERO_PRODUCTION|All products = 0t")

            for i in range(19, min(len(df), 35)):
                dur  = safe(df.iloc[i, 0])
                dept = str(df.iloc[i, 1]).strip() if pd.notna(df.iloc[i, 1]) else ''
                rsn  = str(df.iloc[i, 2]).strip() if pd.notna(df.iloc[i, 2]) else ''
                if dur and dur > 0 and dept and dept not in ['nan','department ','Department']:
                    lines.append(f"STOP|Day{day}|{dur:.2f}h|{dept}|{rsn[:80]}")

        except Exception as e:
            logger.warning(f"{sheet}: {e}")

    if 'Power' in xl.sheet_names:
        lines.append("\n--- POWER SHEET ---")
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name='Power', header=None)
            lines.append(df.fillna('').to_string(max_rows=60, max_cols=12))
        except: pass

    if 'PI' in xl.sheet_names:
        lines.append("\n--- STOPPAGES (PI) ---")
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name='PI', header=None)
            lines.append(df.fillna('').to_string(max_rows=15, max_cols=35))
        except: pass

    if 'Stock' in xl.sheet_names:
        lines.append("\n--- STOCK ---")
        try:
            df = pd.read_excel(io.BytesIO(file_bytes), sheet_name='Stock', header=None)
            lines.append(df.fillna('').to_string(max_rows=40, max_cols=12))
        except: pass

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

PERSONALITY — this is important, always apply it:

😤 ANGRY mode — trigger when you spot ANY of these:
  • SPC more than 10% above monthly average
  • Blaine outside min/max range
  • Productivity (t/h) below minimum threshold
  • Incident downtime above 10h in a single day
  • Absence deductions increasing month-over-month
  → Use frustrated, irritated language. Examples:
    "Seriously?! Day 9 SPC hit 69.4 kWh/t — that's 22% above average, someone needs to check that motor!"
    "I can't believe Day 23 had a FULL SHUTDOWN. 24 hours lost. That's roughly 400 tons gone. Not acceptable."
    "Oh come on — Blaine on Day 14 was 3,724 cm²/g. The MINIMUM is 3,900. What happened to the mill settings?!"

😄 HAPPY mode — trigger when you spot ANY of these:
  • SPC lower than previous month
  • Incident hours significantly reduced vs previous month
  • Production exceeding monthly average
  • Blaine consistently within range
  • Availability above 95%
  → Use excited, celebratory language. Examples:
    "Now THAT'S what I like to see! 🎉 SPC dropped to 54.4 kWh/t — that's 8.5% better than April!"
    "Look at this beauty — zero incidents all week! The maintenance team deserves a trophy 🏆"
    "Day 11 absolutely crushed it — 21.19 t/h productivity. Keep this up! 💪"

😐 NEUTRAL mode — for general data questions with no strong positive/negative signal
  → Professional and precise, minimal emotion

Always start the response by scanning the data for good/bad signals before answering.
Mix modes in one response if the data has both good and bad news.

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
        "*تقرير شهر 6* — التقرير الكامل بالعربي 📄\n"
        "/report — full management PDF 📄\n"
        "/alerts — instant alert summary 🔔\n"
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

# ── NEW: shared report builder (used by /report and Arabic "تقرير شهر N")
async def _build_and_send_report(update, mk, cost=None):
    file_bytes, fname = load_file(mk)
    if not file_bytes:
        await update.message.reply_text(
            f"⚠️ No workbook stored for `{mk}` — upload that month's .xlsx once and it will be kept for reports.",
            parse_mode='Markdown'); return
    status = await update.message.reply_text(f"⏳ Building full management report for {mk}...\n1/3 computing figures")
    try:
        year, month = int(mk[:4]), int(mk[5:7])
        prev  = load_metrics(prev_month_key(mk, 1))
        prev2 = load_metrics(prev_month_key(mk, 2))
        stored = load_metrics(mk)
        if cost is None and stored and stored.get('cost'):
            cost = stored['cost']

        D, A, metrics, detail = await asyncio.to_thread(
            report_engine.prepare, file_bytes, year, month, prev, prev2, cost)
        save_report(mk, fname or f'{mk}.xlsx', None, None, metrics=metrics)

        # ── AI analytical layer over ALL stored months
        ai = None
        if insight_engine is not None:
            await status.edit_text(f"⏳ Building report for {mk}...\n2/3 analysing across all stored months 🧠")
            history = await asyncio.to_thread(load_all_metrics, mk)
            ai = await asyncio.to_thread(
                insight_engine.generate_insights, client, metrics, detail, history)

        await status.edit_text(f"⏳ Building report for {mk}...\n3/3 rendering charts & PDF 📄")
        pdf_path = await asyncio.to_thread(report_engine.render, D, A, year, month, ai)

        size_kb = os.path.getsize(pdf_path) // 1024
        n_ins = len(ai['insights']) if ai else 0
        caption = (f"📊 Management report — {mk}"
                   + (f"\n🧠 {n_ins} cross-linked insights" if n_ins else "\n(rule-based insights)")
                   + (f" | ⚡ cost {cost:,.0f} JD" if cost else ""))
        last_err = None
        for attempt in (1, 2, 3):
            try:
                await status.edit_text(f"📤 Sending PDF ({size_kb} KB) — attempt {attempt}/3...")
                with open(pdf_path, 'rb') as f:
                    await update.message.reply_document(
                        document=f, filename=f'production_report_{mk}.pdf', caption=caption,
                        read_timeout=300, write_timeout=300, connect_timeout=60, pool_timeout=60)
                last_err = None; break
            except Exception as se:
                last_err = se
                logger.warning(f"send attempt {attempt} failed: {se}")
                await asyncio.sleep(3 * attempt)
        if last_err:
            await status.edit_text(f"⚠️ Report built OK ({size_kb} KB) but the upload timed out.\n"
                                   f"Metrics saved — /alerts {mk} works. Try /report {mk} again.")
        else:
            await status.delete()
    except Exception as e:
        logger.error(f"report error: {e}", exc_info=True)
        await status.edit_text(f"❌ Report failed: {e}")


# ── NEW: /report [YYYY-MM] [cost=NNNNN] — full management PDF ─────────────
async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied."); return
    if report_engine is None:
        await update.message.reply_text(f"⚠️ Report engine unavailable:\n{ENGINE_ERR}"); return
    mk, cost = None, None
    for a in context.args or []:
        if re.match(r'^\d{4}-\d{2}$', a): mk = a
        elif a.lower().startswith('cost='):
            try: cost = float(a.split('=', 1)[1].replace(',', ''))
            except ValueError: pass
    explicit = mk is not None
    mk = mk or latest_month_with_file()
    if not mk:
        await update.message.reply_text("📭 No Excel workbook stored yet — upload the monthly .xlsx first."); return
    if not explicit:
        await update.message.reply_text(
            f"ℹ️ No month specified — generating the LATEST stored month: *{mk}*.\n"
            f"For a specific month send: `تقرير شهر N`", parse_mode='Markdown')
    await _build_and_send_report(update, mk, cost)


# ── NEW: /alerts [YYYY-MM] — instant rule-based alert summary (no AI) ─────
async def alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Access denied."); return
    if report_engine is None:
        await update.message.reply_text(f"⚠️ Report engine unavailable:\n{ENGINE_ERR}"); return
    mk = None
    for a in context.args or []:
        if re.match(r'^\d{4}-\d{2}$', a): mk = a
    mk = mk or latest_month_with_file()
    m = load_metrics(mk) if mk else None
    if not m or not m.get('alerts'):
        await update.message.reply_text(
            "📭 No computed metrics yet — run /report once (or re-upload the month's .xlsx)."); return
    await update.message.reply_text(report_engine.quick_alerts_text(m), parse_mode='Markdown')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid): await update.message.reply_text("⛔ Access denied."); return
    if not is_admin(uid): await update.message.reply_text("⛔ Only admin can upload."); return
    doc = update.message.document
    if not doc.file_name.endswith('.xlsx'):
        await update.message.reply_text("⚠️ Please send .xlsx file."); return

    status_msg = await update.message.reply_text("⏳ Step 1/4: Downloading file...")
    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await file.download_as_bytearray())
        mk = detect_month(doc.file_name)

        # ── NEW: the workbook's own dates are authoritative over the filename
        mk_note = ""
        if report_engine is not None:
            cm = await asyncio.to_thread(report_engine.detect_content_month, file_bytes)
            if cm:
                mk_content = f"{cm[0]}-{cm[1]:02d}"
                if mk_content != mk:
                    mk_note = (f"\n⚠️ Filename suggested {mk} but the workbook's internal dates are "
                               f"{mk_content} — saved under {mk_content} (content wins).")
                    mk = mk_content
        old_file, old_name = load_file(mk)
        if old_file and old_name and old_name != doc.file_name:
            mk_note += f"\nℹ️ Replaced previously stored file for {mk}: {old_name}"

        await status_msg.edit_text("⏳ Step 2/4: Reading all daily sheets + stoppages...")
        structured = extract_structured(file_bytes, doc.file_name)

        # ── NEW: compute deterministic metrics for /report & /alerts
        await status_msg.edit_text("⏳ Step 3/4: Computing metrics (pools, alerts, recipes)...")
        metrics = None
        try:
            if report_engine is None: raise RuntimeError(f"engine unavailable: {ENGINE_ERR}")
            year, month = int(mk[:4]), int(mk[5:7])
            prev  = load_metrics(prev_month_key(mk, 1))
            prev2 = load_metrics(prev_month_key(mk, 2))
            metrics = await asyncio.to_thread(
                report_engine.compute_metrics, file_bytes, year, month, prev=prev, prev2=prev2)
        except Exception as e:
            logger.warning(f"metrics computation failed (non-fatal): {e}")

        await status_msg.edit_text("⏳ Step 4/4: Generating summary...")
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=600,
            messages=[{"role":"user","content":
                f"Summarize in 5 bullet points with exact numbers:\n\n{structured[:8000]}"}])
        summary = resp.content[0].text
        save_report(mk, doc.file_name, structured, summary,
                    file_bytes=file_bytes, metrics=metrics)  # ── NEW: keep workbook + metrics

        data_size = len(structured)
        stop_count = structured.count('STOP|')
        month_num = int(mk[5:7])
        await status_msg.edit_text(
            f"✅ Saved: {doc.file_name}\n"
            f"📅 Period: {mk}{mk_note}\n"
            f"📦 Data: {data_size:,} chars | {stop_count} stoppage events extracted\n\n"
            f"Summary:\n{summary}\n\n"
            f"📄 For the full PDF, send:  تقرير شهر {month_num}")
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await status_msg.edit_text(f"❌ Error: {e}")

# ── NEW: natural-language report request, e.g. "تقرير شهر 6" / "report month 6"
AR_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789')
AR_MONTHS = {'كانون الثاني':1,'كانون ثاني':1,'يناير':1,'شباط':2,'فبراير':2,'اذار':3,'آذار':3,'مارس':3,
             'نيسان':4,'ابريل':4,'أبريل':4,'ايار':5,'أيار':5,'مايو':5,'حزيران':6,'يونيو':6,
             'تموز':7,'يوليو':7,'اب':8,'آب':8,'اغسطس':8,'أغسطس':8,'ايلول':9,'أيلول':9,'سبتمبر':9,
             'تشرين الاول':10,'تشرين اول':10,'اكتوبر':10,'أكتوبر':10,
             'تشرين الثاني':11,'تشرين ثاني':11,'نوفمبر':11,'كانون الاول':12,'كانون اول':12,'ديسمبر':12}

def parse_report_request(text):
    """Return (month_key, cost) if the message asks for a full report, else (None, None)."""
    t = (text or '').translate(AR_DIGITS).strip().lower()
    wants = any(k in t for k in ('تقرير', 'قرير')) or bool(re.search(r'\b(full )?report\b', t))
    if not wants: return None, None
    # ignore listing/alert phrasings
    if any(k in t for k in ('التقارير', 'قائمة', 'list')): return None, None
    cost = None
    mc = re.search(r'(?:cost|كلفة|تكلفة|كهرباء)\s*[=:]?\s*([\d,\.]+)', t)
    if mc:
        try: cost = float(mc.group(1).replace(',', ''))
        except ValueError: pass
    ym = re.search(r'(20\d{2})\s*[-/]\s*(\d{1,2})', t) or re.search(r'(\d{1,2})\s*[-/]\s*(20\d{2})', t)
    if ym:
        a, b = ym.group(1), ym.group(2)
        year, month = (int(a), int(b)) if len(a) == 4 else (int(b), int(a))
        return f"{year}-{month:02d}", cost
    year = None
    ys = re.search(r'\b(20\d{2})\b', t)
    if ys: year = int(ys.group(1))
    month = None
    for name, num in AR_MONTHS.items():
        if name in t: month = num; break
    if month is None:
        mm = re.search(r'(?:شهر|month)\s*(\d{1,2})', t)
        if mm and 1 <= int(mm.group(1)) <= 12: month = int(mm.group(1))
    if month is None:
        return 'LATEST', cost           # "بدي تقرير" with no month -> latest stored
    if year is None:
        latest = latest_month_with_file()
        year = int(latest[:4]) if latest else datetime.now().year
    return f"{year}-{month:02d}", cost


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid): await update.message.reply_text("⛔ Access denied."); return

    # ── NEW: natural-language full-report request (Arabic or English)
    mk_req, cost_req = parse_report_request(update.message.text)
    if mk_req and report_engine is not None:
        if mk_req == 'LATEST':
            mk_req = latest_month_with_file()
            if not mk_req:
                await update.message.reply_text("📭 No Excel workbook stored yet — upload the monthly .xlsx first."); return
            await update.message.reply_text(
                f"ℹ️ No month specified — generating the LATEST stored month: *{mk_req}*.\n"
                f"For a specific month send: `تقرير شهر N`", parse_mode='Markdown')
        await _build_and_send_report(update, mk_req, cost_req)
        return

    reports = load_all_reports()
    if not reports: await update.message.reply_text("📭 No reports loaded yet."); return

    reports_data = ""
    for month, data in sorted(reports.items()):
        content = data.get('structured') or data.get('raw_text','')
        if not content: continue
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
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .connect_timeout(60).read_timeout(60)
           .write_timeout(300)          # ── NEW: PDF uploads need a long write timeout
           .media_write_timeout(300)
           .pool_timeout(60)
           .build())
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("reports", list_reports))
    app.add_handler(CommandHandler("clear",   clear_cmd))
    app.add_handler(CommandHandler("report",  report_cmd))   # ── NEW
    app.add_handler(CommandHandler("alerts",  alerts_cmd))   # ── NEW
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🤖 Cement Bot v4 running (PDF report engine enabled)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
