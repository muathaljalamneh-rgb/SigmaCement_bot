"""Glue between Telegram uploads, storage, the report builder and the morning schedule."""
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from . import parsers as P
from .analysis import build_report
from .names import PRODUCT_AR

log = logging.getLogger(__name__)
TZ = ZoneInfo('Asia/Amman')
CUMULATIVE_DAILY = ('qc', 'acquisition', 'clinker', 'additives')   # whole workbook re-sent every day


def now_local():
    return dt.datetime.now(TZ).replace(tzinfo=None)


def report_day(now=None):
    """Latest complete production day: day D ends at D+1 08:00."""
    now = now or now_local()
    return (now - dt.timedelta(hours=8)).date() - dt.timedelta(days=1)


def is_daily_workbook(sheets):
    """True for the new daily files; the old monthly template (with a 'Summary' sheet) stays on the monthly path."""
    if any('summary' in s.lower() for s in sheets) and P.detect_kind(sheets) != 'qc_summary':
        return False
    return P.detect_kind(sheets) is not None


def ingest(store, data, filename, uid=None, now=None):
    """Parse + store one uploaded workbook. Returns (kind, day, parsed)."""
    now = now or now_local()
    sheets = P.load(data)
    kind = P.detect_kind(sheets)
    if not kind:
        raise ValueError('لم أتعرف على نوع الملف')
    parsed = P.PARSERS[kind](data, sheets) if kind in P.PARSERS else None
    day = P.file_day(kind, parsed) if parsed is not None else None
    if day is None:
        day = report_day(now)
    store.save_file(kind, day, filename, data, uid, now)
    return kind, day, parsed


def load_files(store, day):
    files, meta = {}, {}
    for kind in P.DAILY_KINDS + P.STATIC_KINDS:
        f = store.load_file(kind, day)
        if not f:
            files[kind] = None
            continue
        try:
            files[kind] = P.PARSERS[kind](f['data']) if kind in P.PARSERS else None
        except Exception as e:                      # one broken file must not kill the report
            log.exception('parse %s failed', kind)
            files[kind] = None
            meta[kind] = {'filename': f['filename'], 'error': str(e)}
            continue
        up = f['uploaded_at']
        fresh = True
        if kind in CUMULATIVE_DAILY:
            fresh = up >= dt.datetime.combine(day + dt.timedelta(days=1), dt.time(6, 0))
        meta[kind] = {'filename': f['filename'], 'uploaded_at': up.strftime('%Y-%m-%d %H:%M'), 'fresh': fresh}
    return files, meta


def rebuild(store, day):
    files, meta = load_files(store, day)
    rep = build_report(day, files, meta)
    for k, m in meta.items():
        if m.get('error'):
            rep['alarms'].insert(0, {'sev': 'red', 'area': 'الملفات', 'text': f"تعذّرت قراءة {P.KINDS_AR.get(k, k)}: {m['error']}"})
        elif m.get('fresh') is False:
            rep['alarms'].append({'sev': 'info', 'area': 'الملفات',
                                  'text': f"{P.KINDS_AR.get(k, k)} مرفوع قبل نهاية يوم الإنتاج ({m['uploaded_at']}) — قد لا يكون محدّثًا"})
    store.save_report(day, rep)
    return rep


def missing_daily(rep):
    """Daily files still missing (or stale) for the report day."""
    out = []
    for k in P.DAILY_KINDS:
        f = (rep.get('files') or {}).get(k) or {}
        if not f.get('present') or f.get('covers') is False or f.get('fresh') is False:
            out.append(P.KINDS_AR[k])
    return out


def summary_text(rep, url=None, title='📊 التقرير اليومي'):
    k = rep.get('kpis') or {}
    lines = [f"{title} — يوم الإنتاج {rep['day']}"]
    if k:
        prods = '، '.join(f"{p['name']} {p['prod']:g} ط" for p in rep.get('products') or [])
        lines += [f"🏭 الإنتاج: {k.get('production') or 0:g} طن ({prods or 'لا إنتاج'})",
                  f"⚙️ الإنتاجية {k.get('tph') or '—'} ط/س · الطاقة {k.get('spc_plant') or '—'} kWh/ط",
                  f"📅 المنجز من خطة الشهر: {k.get('mtd_pct') or '—'}%",
                  f"⛔ {k.get('red', 0)} حرج · ⚠️ {k.get('amber', 0)} تنبيه"]
    reds = [a['text'] for a in rep.get('alarms') or [] if a['sev'] == 'red'][:3]
    if reds:
        lines.append('')
        lines += [f'⛔ {t}' for t in reds]
    miss = missing_daily(rep)
    if miss:
        lines += ['', '📭 ناقص: ' + '، '.join(miss)]
    if url:
        lines += ['', f"🔗 {url.rstrip('/')}/day/{rep['day']}"]
    return '\n'.join(lines)


def ack_text(kind, day, rep):
    miss = missing_daily(rep)
    s = f"✅ استلمت {P.KINDS_AR.get(kind, kind)} — يوم الإنتاج {day}"
    if kind == 'mill_production':
        s += f"\n🏭 إنتاج اليوم {(rep.get('kpis') or {}).get('production') or 0:g} طن"
    s += '\n' + ('📭 ناقص لهذا اليوم: ' + '، '.join(miss) if miss else '🎉 كل ملفات اليوم وصلت')
    return s
