"""Readers for the daily department workbooks. Each reader takes raw .xlsx bytes and returns
plain Python structures; nothing here judges the data (that is analysis.py)."""
import io
import re
import datetime as dt
from collections import defaultdict

import openpyxl
from openpyxl.utils import column_index_from_string

from .names import product_key, material_key, norm

KIND_NAMES = {
    'mill_production': 'Mill production', 'process': 'Mill operation process (DCS)',
    'qc': 'Mill product QC', 'acquisition': 'Raw material acquisition (truck scale)',
    'clinker': 'Clinker material', 'additives': 'Additive materials', 'sales': 'Daily sales',
    'plan': 'Annual plan', 'fan': 'Mill fan table', 'qc_summary': 'QC summary',
}
DAILY_KINDS = ['mill_production', 'process', 'qc', 'acquisition', 'clinker', 'additives', 'sales']
STATIC_KINDS = ['plan', 'fan', 'qc_summary']


# ── helpers ───────────────────────────────────────────────────────────────
def load(file_bytes):
    """Workbook → {sheet_name: [row tuples]} (values only, cached formulas)."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    out = {}
    for ws in wb.worksheets:
        rows = []
        blank = 0
        for r in ws.iter_rows(values_only=True):
            if any(v not in (None, '') for v in r):
                rows.extend([()] * blank)
                blank = 0
                rows.append(r)
            else:
                blank += 1
                if blank > 3000:        # formatted-but-empty tails (e.g. 1,048,576 rows)
                    break
        out[ws.title] = rows
    wb.close()
    return out


def cell(rows, r, c):
    """0-based row/col access tolerant of ragged rows."""
    if r < 0 or r >= len(rows):
        return None
    row = rows[r]
    return row[c] if c < len(row) else None


def num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(',', '')
        try:
            return float(s)
        except ValueError:
            return None
    return None


def as_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return None


def as_hour(v):
    """QC/production hour cells are datetimes on a fake 1900 day, or times."""
    if isinstance(v, dt.datetime):
        return v.hour
    if isinstance(v, dt.time):
        return v.hour
    if isinstance(v, (int, float)) and 0 <= v < 1:
        return int(round(v * 24)) % 24
    return None


def find_sheet(sheets, *needles):
    for name in sheets:
        n = norm(name)
        if all(norm(x) in n for x in needles):
            return name
    return None


# ── kind detection ────────────────────────────────────────────────────────
def detect_kind(sheets):
    names = [norm(n) for n in sheets]
    joined = ' | '.join(names)
    first = lambda s: ' '.join(norm(v) for v in (sheets[s][0] if sheets[s] else ()) if v is not None)
    if 'production details' in joined and 'power' in names:
        return 'mill_production'
    if any('badge history' in n for n in names):
        return 'qc'
    if any(n.startswith('additive') for n in names):
        return 'additives'
    if any('white clinker' in n for n in names) and any('grey clinker' in n for n in names):
        return 'clinker'
    if any('forecast' in n for n in names):
        return 'plan'
    if any(n.startswith('clinker ') for n in names) and any('limestone' in n or 'pozz' in n for n in names):
        return 'acquisition'
    for s in sheets:
        f = first(s)
        if 'fresh feed' in f and 'time' in f:
            return 'process'
        if 'cement type' in f and 'net weight' in f:
            return 'sales'
        if ('qc summary' in f or 'qc summary' in norm(s)
                or ('cement type' in f and 'mechanical analyses' in f)):
            return 'qc_summary'
        rows = sheets[s][:12]
        flat = ' '.join(norm(v) for r in rows for v in r if v is not None)
        if 'kwcalc' in flat and 'fan speed' in flat:
            return 'fan'
    return None


# ── mill production (monthly workbook, one tab per day) ───────────────────
def _date_cols(row, start_col):
    """{date: col_index} for the date header row, from start_col on."""
    out = {}
    for c in range(start_col, len(row)):
        d = as_date(row[c])
        if d:
            out[d] = c
    return out


def parse_mill_production(file_bytes, sheets=None):
    sheets = sheets or load(file_bytes)
    pd_name = find_sheet(sheets, 'production details')
    rows = sheets[pd_name]
    dcols = _date_cols(rows[0], 3)

    # blocks: col A = product, col B = 'Material counters'
    starts = [i for i, r in enumerate(rows) if len(r) > 1 and r[0] and 'counter' in norm(r[1])]
    blocks = {}
    for bi, st in enumerate(starts):
        en = starts[bi + 1] if bi + 1 < len(starts) else len(rows)
        key = product_key(rows[st][0])
        if not key:
            continue
        blocks[key] = _parse_block(rows, st, en, dcols, label=str(rows[st][0]).strip())

    power = _parse_power(sheets.get(find_sheet(sheets, 'power')) or [])
    stock = _parse_stock(sheets.get(find_sheet(sheets, 'stock')) or [])
    pi = _parse_pi(sheets.get(find_sheet(sheets, 'pi')) or [])
    tabs = _parse_daily_tabs(sheets)
    days_with_data = sorted({d for b in blocks.values() for d, v in b['production'].items() if v})
    return {'dates': sorted(dcols), 'blocks': blocks, 'power': power, 'stock': stock, 'pi': pi,
            'daily_tabs': tabs, 'days_with_data': days_with_data}


def _parse_block(rows, st, en, dcols, label):
    sec = None
    b = {'label': label, 'counters': {}, 'moisture': {}, 'dry': {}, 'wet': {}, 'proportions': {},
         'hourly': {}, 'production': {}, 'mtd': {}, 'hours': {}, 'tph': {}, 'ck': {}, 'clinker_pct': {},
         'ga_gpt': {}, 'blaine': {}, 'r45': {}, 'whiteness': {}, 'chem_ld': {}, 'chem_density': {}}
    vals = lambda i: {d: num(cell(rows, i, c)) for d, c in dcols.items()}
    i = st
    while i < en:
        a, bb, c, d = (cell(rows, i, k) for k in range(4))
        nb, nc, nd = norm(bb), norm(c), norm(d)
        if nb:
            if 'counter' in nb:
                sec = 'counters'
            elif 'moisture' in nb:
                sec = 'moisture'
            elif 'dry' in nb:
                sec = 'dry'
            elif 'wet' in nb:
                sec = 'wet'
            elif 'proportion' in nb:
                sec = 'proportions'
            elif 'daily production' in nb:
                sec = None
                b['production'] = vals(i)
                if 'mtd' in norm(cell(rows, i + 1, 3)):
                    b['mtd'] = vals(i + 1)
            elif nb.startswith('running h'):
                b['hours'] = vals(i)
            elif 'productivity' in nb:
                b['tph'] = vals(i)
            elif nb == 'c k':
                b['ck'] = vals(i)
                if 'clinker' in norm(cell(rows, i + 1, 3)):
                    b['clinker_pct'] = vals(i + 1)
            elif nb.startswith('grinding aid'):
                b['ga_gpt'] = vals(i)
            elif nb.startswith('blaine'):
                b['blaine'] = vals(i)
            elif nb == 'r45':
                b['r45'] = vals(i)
            elif nb.startswith('whiteness'):
                b['whiteness'] = vals(i)
        if sec == 'counters' and c and nd == 'start':
            name = str(c).strip()
            b['counters'][name] = {'start': vals(i), 'end': vals(i + 1)}
            i += 2
            continue
        if sec == 'counters' and c and nd in ('l d',):
            b['chem_ld'][str(c).strip()] = vals(i)
            if 'density' in norm(cell(rows, i + 1, 2)):
                b['chem_density'][str(c).strip()] = vals(i + 1)
        if (sec in ('moisture', 'dry', 'wet', 'proportions') and isinstance(d, str) and d.strip()
                and not nb.startswith('daily')):
            b[sec][material_key(d) or str(d).strip()] = vals(i)
        if nc in ('blaine', 'r45', 'r63', 'wi') and isinstance(d, (dt.time, dt.datetime)):
            sec = None
            series = {}
            for k in range(24):
                h = as_hour(cell(rows, i + k, 3))
                if h is None:
                    break
                series[h] = vals(i + k)
            b['hourly'][nc] = series
            i += len(series)
            continue
        i += 1
    return b


def _parse_power(rows):
    if not rows:
        return {}
    dcols = _date_cols(rows[0], 2)
    vals = lambda i: {d: num(cell(rows, i, c)) for d, c in dcols.items()}
    out, cur = {}, None
    for i in range(1, len(rows)):
        a, b, c = cell(rows, i, 0), norm(cell(rows, i, 1)), norm(cell(rows, i, 2))
        if a and product_key(a):
            cur = product_key(a)
            out[cur] = {}
        if not cur:
            continue
        if b == 'md power' and c == 'start':
            out[cur]['md'] = {'start': vals(i), 'end': vals(i + 1)}
        elif b == 'total power' and c == 'start':
            out[cur]['total'] = {'start': vals(i), 'end': vals(i + 1)}
        elif b.startswith('md consumed'):
            out[cur]['md_kwh'] = vals(i)
        elif b.startswith('total consumed'):
            out[cur]['total_kwh'] = vals(i)
        elif b.startswith('spc') and 'md' in b:
            out[cur]['spc_md'] = vals(i)
        elif b.startswith('spc') and 'plant' in b:
            out[cur]['spc_plant'] = vals(i)
        elif b.startswith('daily production'):
            out[cur]['production'] = vals(i)
    return out


def _parse_stock(rows):
    if not rows:
        return {}
    dcols = _date_cols(rows[0], 2)
    out, sec = defaultdict(dict), None
    for i in range(1, len(rows)):
        a, c = norm(cell(rows, i, 0)), cell(rows, i, 2)
        if a:
            sec = ('initial' if 'initial' in a else 'import' if 'import' in a else 'moisture' if 'moisture' in a
                   else 'wet' if 'consumption' in a else 'final' if 'final' in a else sec)
        if sec and c:
            name = str(c).strip()
            mk = material_key(name) or name
            if sec == 'moisture' and 'lg' in norm(name):
                mk = 'Limestone LG'
            out[sec][mk] = {d: num(cell(rows, i, col)) for d, col in dcols.items()}
    return dict(out)


def _parse_pi(rows):
    if not rows:
        return {}
    dcols = _date_cols(rows[0], 2)
    out = {}
    for i in range(1, len(rows)):
        b = norm(cell(rows, i, 1))
        if b:
            out[b] = {d: num(cell(rows, i, c)) for d, c in dcols.items()}
    return out


def _parse_daily_tabs(sheets):
    """{date: [ {hours, dept, reason} ]} from the per-day tabs (A1 = date)."""
    out = {}
    for name, rows in sheets.items():
        d = as_date(cell(rows, 0, 0))
        if not d:
            continue
        hdr = next((i for i in range(len(rows)) if 'stoppage' in norm(cell(rows, i, 0))), None)
        stops = []
        if hdr is not None:
            for i in range(hdr + 1, min(hdr + 12, len(rows))):
                if 'mtd' in norm(cell(rows, i, 0)):
                    break
                h, dep, rsn = num(cell(rows, i, 0)), cell(rows, i, 1), cell(rows, i, 2)
                if h or rsn:
                    stops.append({'hours': h, 'dept': str(dep or '').strip(), 'reason': str(rsn or '').strip()})
        out[d] = {'sheet': name, 'stops': stops}
    return out


# ── process data (DCS, one file per production day) ───────────────────────
def parse_process(file_bytes, sheets=None):
    sheets = sheets or load(file_bytes)
    rows = next(iter(sheets.values()))
    hdr, tags = rows[0], rows[1]
    cols = {}
    for c in range(max(len(hdr), len(tags))):
        h = hdr[c] if c < len(hdr) else None
        tg = tags[c] if c < len(tags) else None
        if h:
            cols.setdefault(norm(h), c)
        elif tg:                           # e.g. silo tons / R45 have only a DCS tag
            cols.setdefault(norm(tg), c)
    wf_names = {}
    for k in range(1, 5):
        c = next((cols[h] for h in cols if h.startswith(f'wf{k} feeding') or h.startswith(f'wf {k} feeding')), None)
        if c is not None:
            nm = str(tags[c]).strip() if c < len(tags) and tags[c] else ''
            # E1: names are typed by the process engineer; an untouched DCS tag means "no name"
            wf_names[k] = None if (not nm or 'y value' in norm(nm) or 'weigh feeder' in norm(nm)) else nm
    data = [r for r in rows[2:] if r and isinstance(r[0], dt.datetime)]
    t = [r[0] for r in data]
    series = {h: [num(r[c]) if c < len(r) else None for r in data] for h, c in cols.items() if h != 'time'}
    return {'time': t, 'series': series, 'wf_names': wf_names, 'columns': cols}


def process_day(proc):
    """Production day of a DCS file: the day its 08:00 window starts."""
    if not proc['time']:
        return None
    t0 = proc['time'][0]
    return (t0 - dt.timedelta(hours=8)).date()


# ── QC product report (yearly workbook) ───────────────────────────────────
QC_FIELDS = {  # row-2 header text → field
    'blaine': 'blaine', 'ris 45': 'r45', 'ris 63': 'r63', 'ris 36': 'r36', 'storage silo': 'silo',
    'temperature': 'temp_outlet', 'chemicals': 'chem', 'initial': 'set_initial', 'final': 'set_final',
    '2d': 's2', '7d': 's7', '28d': 's28',
}


def parse_qc(file_bytes, sheets=None, year=None):
    sheets = sheets or load(file_bytes)
    main = next((s for s in sheets if re.fullmatch(r'\s*20\d\d\s*', s)), None)
    if year and str(year) in sheets:
        main = str(year)
    rows = sheets[main]
    r1, r2, r3 = rows[0], rows[1], rows[2]
    cols = _qc_columns(r1, r2, r3)
    hourly = []
    cur_date = None
    for r in rows[4:]:
        d = as_date(r[0] if r else None)
        if d:
            cur_date = d
        h = as_hour(r[1] if len(r) > 1 else None)
        if cur_date is None or h is None:
            continue
        name = r[2] if len(r) > 2 else None
        rec = {'date': cur_date, 'hour': h, 'name': str(name).strip() if name else None,
               'product': product_key(name) if name else None}
        for f, c in cols['fields'].items():
            rec[f] = r[c] if c < len(r) else None
        rec['clinker'] = {k: num(r[c]) for k, c in cols['clinker'].items() if c < len(r) and num(r[c])}
        rec['additives'] = {k: num(r[c]) for k, c in cols['additives'].items() if c < len(r) and num(r[c])}
        rec['chemicals'] = {k: num(r[c]) for k, c in cols['chemicals'].items() if c < len(r) and num(r[c])}
        hourly.append(rec)
    badge = _parse_badge(sheets.get(find_sheet(sheets, 'badge history')) or [])
    return {'hourly': hourly, 'badge': badge}


def _qc_columns(r1, r2, r3):
    group = {}
    g = None
    for c in range(max(len(r1), len(r2), len(r3))):
        v1 = norm(r1[c]) if c < len(r1) else ''
        if v1:
            g = v1
        group[c] = g
    fields, clinker, additives, chemicals = {}, {}, {}, {}
    sub = None
    for c in range(len(r3)):
        v2 = norm(r2[c]) if c < len(r2) else ''
        v3 = norm(r3[c]) if c < len(r3) else ''
        if v2:
            sub = v2
        gc = group.get(c) or ''
        if gc == 'raw material':
            label = str(r3[c] or '').strip()
            if sub.startswith('clk'):
                clinker[label] = c
            elif sub.startswith('chemical'):
                chemicals[label] = c
            elif label:
                additives[f'{sub}:{label}'] = c
            elif sub:
                additives[sub] = c
        if gc == 'operation':
            if 'silo' in sub and v3 == 'number':
                fields['silo'] = c
            elif 'silo' in sub and v3 == 'level':
                fields['silo_level'] = c
            elif v3 == 'mill outlet':
                fields['temp_outlet'] = c
            elif v3 == 'cement':
                fields['temp_cement'] = c
        if gc == 'physical analyses':
            if 'blaine' in v2:
                fields['blaine'] = c
            elif 'ris 45' in v2:
                fields['r45'] = c
            elif 'ris 63' in v2:
                fields['r63'] = c
            elif v3 == 'l*':
                fields['l_star'] = c
            elif v3 == 'wi':
                fields['wi'] = c
        if gc == 'xrf analyses' and v2 == 'so3':
            fields['so3_xrf'] = c
        if gc == 'chemical analyses' and v2 == 'so3':
            fields['so3_chem'] = c
        if gc == 'mechanical analyses' and 'mechanical' in gc and c < len(r3):
            if v3 == 'initial':
                fields['set_initial'] = c
            elif v3 == 'final':
                fields['set_final'] = c
            elif v3 == '2d':
                fields['s2'] = c
            elif v3 == '7d':
                fields['s7'] = c
            elif v3 == '28d' and 's28' not in fields:
                fields['s28'] = c
    return {'fields': fields, 'clinker': clinker, 'additives': additives, 'chemicals': chemicals}


def parse_range(s):
    """'≤1.2' → (None,1.2) · '≥ 91' → (91,None) · '4000 ±100' → (3900,4100) · '95-105' → (95,105)."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return (float(s), float(s))
    t = str(s).strip().replace(',', '.').replace('–', '-').replace(' ', '')
    m = re.fullmatch(r'(?:≤|<=|<)([\d.]+)', t)
    if m:
        return (None, float(m.group(1)))
    m = re.fullmatch(r'(?:≥|>=|>)([\d.]+)', t)
    if m:
        return (float(m.group(1)), None)
    m = re.fullmatch(r'([\d.]+)±([\d.]+)', t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return (a - b, a + b)
    m = re.fullmatch(r'([\d.]+)-([\d.]+)', t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return (min(a, b), max(a, b))
    return None


def _parse_badge(rows):
    if not rows:
        return []
    hdr = [norm(v) for v in rows[0]]
    idx = lambda *ks: next((i for i, h in enumerate(hdr) if all(k in h for k in ks)), None)
    c = {'date': idx('date'), 'serial': idx('serial'), 'product': idx('product type'), 'clinker': idx('clinker'),
         'limestone': idx('limestone'), 'pozzolana': idx('pozzolana'), 'gypsum': idx('gypsum'),
         'basalt': idx('basalt'), 'silica': idx('silica'), 'dose': idx('chemical dose'), 'r45': idx('r45'),
         'blaine': idx('blain'), 'so3': idx('so3'), 'l_star': idx('whitness'), 'temp_outlet': idx('mill outlet'),
         'temp_cement': idx('final product')}
    out = []
    for r in rows[1:]:
        d = as_date(cell([r], 0, c['date']))
        if not d:
            continue
        g = lambda k: r[c[k]] if c[k] is not None and c[k] < len(r) else None
        out.append({'date': d, 'serial': g('serial'), 'name': str(g('product') or '').strip(),
                    'product': product_key(g('product')),
                    'props': {k: num(g(k)) for k in ('clinker', 'limestone', 'pozzolana', 'gypsum', 'basalt', 'silica')
                              if num(g(k))},
                    'dose': num(g('dose')),
                    'limits': {k: parse_range(g(k)) for k in ('r45', 'blaine', 'so3', 'l_star', 'temp_outlet',
                                                              'temp_cement')}})
    return out


# ── truck scale: raw material acquisition ─────────────────────────────────
def parse_acquisition(file_bytes, sheets=None):
    sheets = sheets or load(file_bytes)
    out = defaultdict(lambda: defaultdict(float))      # material → date → tons
    trucks = defaultdict(lambda: defaultdict(int))
    for name, rows in sheets.items():
        mk = material_key(name)
        if not mk or not rows:
            continue
        hdr = [norm(v) for v in rows[0]]
        cd = next((i for i, h in enumerate(hdr) if h == 'التاريخ'), 4)
        cn = next((i for i, h in enumerate(hdr) if 'صافي الحمولة' in h), 9)
        for r in rows[1:]:
            d, w = as_date(cell([r], 0, cd)), num(cell([r], 0, cn))
            if d and w:
                out[mk][d] += w / 1000.0
                trucks[mk][d] += 1
    return {'tons': {k: dict(v) for k, v in out.items()}, 'trucks': {k: dict(v) for k, v in trucks.items()},
            'tabs': [material_key(n) for n in sheets if material_key(n)]}


# ── lab moisture: clinker + additives ─────────────────────────────────────
def _lab_rows(rows, water_col, src_col=1, loc_col=2, default_mat=None):
    out = []
    for r in rows:
        d = as_date(cell([r], 0, 0))
        w = num(cell([r], 0, water_col))
        if not d or w is None:
            continue
        src = str(cell([r], 0, src_col) or '').strip()
        loc = str(cell([r], 0, loc_col) or '').strip()
        mk = material_key(src) or default_mat
        out.append({'date': d, 'source': src, 'location': loc, 'material': mk, 'water_pct': w})
    return out


def _find_col(rows, text, max_rows=4):
    for r in rows[:max_rows]:
        for c, v in enumerate(r):
            if text in norm(v):
                return c
    return None


def parse_clinker(file_bytes, sheets=None):
    sheets = sheets or load(file_bytes)
    out = []
    for name, rows in sheets.items():
        c = _find_col(rows, 'water content')
        if c is not None:
            out += _lab_rows(rows[4:], c)
    return out


def parse_additives(file_bytes, sheets=None):
    sheets = sheets or load(file_bytes)
    out = []
    for name, rows in sheets.items():
        n = norm(name)
        if 'out sour' in n:
            continue                     # outsourced trial samples
        default = 'Limestone HG' if 'limestone' in n else 'Pozzolana' if 'pozz' in n else None
        c = _find_col(rows, 'water content')
        if c is None:
            continue
        for rec in _lab_rows(rows[4:], c, default_mat=default):
            if 'gypsum' in n and rec['material'] not in ('Gypsum Az', 'Gypsum Taf'):
                continue                 # unnamed gypsum sample: source unknown
            out.append(rec)
    return out


# ── daily sales (truck scale out) ─────────────────────────────────────────
def sales_product(name):
    n = str(name or '')
    if 'باور' in n:
        return 'power'
    if 'سوبر' in n:
        return 'super'
    if 'ايكو' in n or 'إيكو' in n:
        return 'eco'
    if 'الترا' in n or 'ألترا' in n:
        return 'ultra'
    return product_key(n)


def parse_sales(file_bytes, sheets=None):
    sheets = sheets or load(file_bytes)
    rows = next(iter(sheets.values()))
    hdr = [norm(v) for v in rows[0]]
    ix = lambda k: next((i for i, h in enumerate(hdr) if k in h), None)
    c = {'card': ix('card'), 'plate': ix('plate'), 'type': ix('cement type'), 'customer': ix('customer'),
         'net': ix('net weight'), 'tin': ix('time in'), 'din': ix('date in'), 'tout': ix('time out'), 'dout': ix('date out')}
    out = []
    for r in rows[1:]:
        net = num(cell([r], 0, c['net']))
        din = as_date(cell([r], 0, c['din']))
        if net is None or din is None:
            continue

        def at(dcol, tcol):
            d, t = as_date(cell([r], 0, c[dcol])), cell([r], 0, c[tcol])
            if d and isinstance(t, dt.time):
                return dt.datetime.combine(d, t)
            if d and isinstance(t, dt.datetime):
                return dt.datetime.combine(d, t.time())
            return None
        out.append({'card': cell([r], 0, c['card']), 'plate': cell([r], 0, c['plate']),
                    'type': str(cell([r], 0, c['type']) or '').strip(),
                    'product': sales_product(cell([r], 0, c['type'])),
                    'customer': str(cell([r], 0, c['customer']) or '').strip(),
                    'net_t': net / 1000.0, 'date': din, 'time_in': at('din', 'tin'), 'time_out': at('dout', 'tout')})
    return out


# ── annual plan ───────────────────────────────────────────────────────────
PLAN_ROWS = {'m50': 'm50', 'low alkali': 'low_alkali', 'cem ii b p 42.5 n': 'm10', 'cem i 52.5 r': 'ultra',
             'cem ii a ll 52.5 n': 'power', 'cem ii b ll 42.5 n': 'super', 'cem ii b ll 32.5 n': 'eco'}


def parse_plan(file_bytes, sheets=None):
    sheets = sheets or load(file_bytes)
    rows = sheets[find_sheet(sheets, 'forecast')]
    months = {}
    for c, v in enumerate(rows[1]):
        d = as_date(v)
        if d and d.day == 1:
            months[(d.year, d.month)] = c
    plan, req_h, holidays = defaultdict(dict), {}, {}
    for r in rows[2:20]:
        code = norm(r[1] if len(r) > 1 else '').replace(',', '.')
        key = next((k for pat, k in PLAN_ROWS.items() if pat in code), None)
        label = ' '.join(norm(v) for v in r[:20] if isinstance(v, str))
        for ym, c in months.items():
            v = num(cell([r], 0, c))
            if key and v is not None:
                plan[ym][key] = v
            elif 'required running' in label and v is not None:
                req_h[ym] = v
            elif 'holiday' in label and v is not None:
                holidays[ym] = v
    return {'plan': {k: dict(v) for k, v in plan.items()}, 'required_hours': req_h, 'holidays': holidays}


# ── mill fan table ────────────────────────────────────────────────────────
def parse_fan(file_bytes, sheets=None):
    sheets = sheets or load(file_bytes)
    rows = next(iter(sheets.values()))
    hr = next(i for i, r in enumerate(rows) if any('kwcalc' in norm(v) for v in r))
    hdr = [norm(v) for v in rows[hr]]
    c_rpm = next(i for i, h in enumerate(hdr) if 'fan speed' in h)
    c_kw = next(i for i, h in enumerate(hdr) if 'kwcalc' in h)
    c_v = next((i for i, h in enumerate(hdr) if 'velocity' in h), None)
    table = []
    for r in rows[hr + 1:]:
        rpm, kw = num(cell([r], 0, c_rpm)), num(cell([r], 0, c_kw))
        if rpm and kw:
            table.append((rpm, kw, num(cell([r], 0, c_v)) if c_v is not None else None))
    return sorted(table)


# ── QC summary: one row per month and product (Eng. Imad's yearly reference, answer D5) ──
QCS_FIELDS = {'blaine': ('physical', 'blaine'), 'r45': ('physical', 'ros 45'), 'l_star': (None, 'l*'),
              'loi': ('chemical', 'loi'), 'so3': ('chemical', 'so3'), 'setting': ('mechanical', 'setting'),
              's2': (None, '2d'), 's7': (None, '7d'), 's28': (None, '28d'), 'dose': ('raw', 'fayhaa'),
              'temp_outlet': (None, 'mill outlet'), 'temp_cement': (None, 'cement')}


def parse_qc_summary(file_bytes, sheets=None):
    sheets = sheets or load(file_bytes)
    name = next((n for n in sheets if norm(n) == 'total'), None) or next(iter(sheets))
    rows = sheets[name]
    r1, r2, r3 = (rows[i] if i < len(rows) else () for i in range(3))
    width = max(len(r1), len(r2), len(r3))
    group, g = {}, ''
    for c in range(width):
        v = norm(r1[c]) if c < len(r1) else ''
        g = v or g
        group[c] = g
    cols = {}
    for f, (grp, label) in QCS_FIELDS.items():
        for c in range(2, width):
            t2 = norm(r2[c]) if c < len(r2) else ''
            t3 = norm(r3[c]) if c < len(r3) else ''
            if (label == t3 or (t2 and t2.startswith(label))) and (grp is None or group.get(c, '').startswith(grp)):
                cols.setdefault(f, c)
                break
    out = []
    for r in rows[3:]:
        d = as_date(r[0] if r else None)
        key = product_key(r[1]) if len(r) > 1 else None
        if not d or not key:
            continue
        vals = {f: num(r[c]) for f, c in cols.items() if c < len(r) and num(r[c]) is not None}
        if vals:
            out.append({'month': (d.year, d.month), 'product': key, **vals})
    return out


PARSERS = {'mill_production': parse_mill_production, 'process': parse_process, 'qc': parse_qc,
           'acquisition': parse_acquisition, 'clinker': parse_clinker, 'additives': parse_additives,
           'sales': parse_sales, 'plan': parse_plan, 'fan': parse_fan, 'qc_summary': parse_qc_summary}


def file_day(kind, parsed):
    """The production day a daily-only file belongs to (None for cumulative workbooks)."""
    if kind == 'process':
        return process_day(parsed)
    if kind == 'sales' and parsed:
        days = sorted({s['date'] for s in parsed})
        return days[-1]
    if kind == 'mill_production':
        return parsed['days_with_data'][-1] if parsed['days_with_data'] else None
    return None
