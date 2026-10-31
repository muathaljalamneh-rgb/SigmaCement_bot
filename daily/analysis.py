"""Cross-department checks for one production day (D 08:00 → D+1 08:00).

Every rule here comes from Eng. Imad's "Analyses" document and his answers to the
question sheet (references like B1, D3 point to that sheet). build_report() returns a
plain JSON-able dict that the dashboard renders; it never raises for a missing file —
that section is marked missing instead."""
import calendar
import datetime as dt
import math
import re
from collections import defaultdict, Counter

from .names import (PRODUCT_NAMES, PRODUCT_ORDER, WHITE, GREY, TARGET_TPH, SILOS, MATERIAL_NAMES, WHITE_CLINKERS,
                    GREY_CLINKERS, PRICE_WHITE_CLINKER, PRICE_GREY_CLINKER, material_key)
from .parsers import KIND_NAMES

TOL = {'Running h': 0.05, 'md': 50.0, 'total': 50.0, 'material': 0.5}
GA_DENSITY = 1.06            # C5: 1.06 ± 0.02 g/ml
GA_DENSITY_TOL = 0.02
GA_TOL_PCT = 10.0            # QC (ml/min) vs production (g/t) dosage
PROP_TOL_PP = 1.0            # D4: wet-basis proportion deviation, percentage points
MOIST_TOL_PP = 0.5           # C3
STOCK_WEEKS_ALARM = 2.0      # C4
GAS_T = (90.0, 100.0)        # mill outlet gas temperature target
EN197 = {  # minimum strength MPa / initial setting min by class (D5)
    '32.5n': {'s7': 16, 's28': 32.5, 'set': 75}, '42.5n': {'s2': 10, 's28': 42.5, 'set': 60},
    '42.5r': {'s2': 20, 's28': 42.5, 'set': 60}, '52.5n': {'s2': 20, 's28': 52.5, 'set': 45},
    '52.5r': {'s2': 30, 's28': 52.5, 'set': 45},
}
PRODUCT_CLASS = {'ultra': '52.5r', 'power': '52.5n', 'super': '42.5n', 'eco': '32.5n', 'm50': '52.5n',
                 'low_alkali': '42.5n', 'm10': '42.5n'}


# ── small helpers ─────────────────────────────────────────────────────────
def r2(x, n=2):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return int(round(float(x))) if n == 0 else round(float(x), n)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def pearson(a, b):
    pts = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pts) < 10:
        return None
    mx, my = mean([p[0] for p in pts]), mean([p[1] for p in pts])
    sx = math.sqrt(sum((p[0] - mx) ** 2 for p in pts))
    sy = math.sqrt(sum((p[1] - my) ** 2 for p in pts))
    if sx == 0 or sy == 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / (sx * sy)


def pname(k):
    return PRODUCT_NAMES.get(k, k)


def mname(k):
    return MATERIAL_NAMES.get(k, k)


class Alarms(list):
    def add(self, sev, area, text):
        self.append({'sev': sev, 'area': area, 'text': text})


# ── 1. production per product ─────────────────────────────────────────────
def products_today(mp, day):
    rows = []
    for k in PRODUCT_ORDER:
        b = mp['blocks'].get(k)
        if not b:
            continue
        prod = b['production'].get(day) or 0
        if prod <= 0:
            continue
        pw = mp['power'].get(k, {})
        spc_md, spc_pl = (pw.get('spc_md') or {}).get(day), (pw.get('spc_plant') or {}).get(day)
        aux = (spc_pl - spc_md) / spc_pl * 100 if spc_md and spc_pl else None   # B6
        tph = b['tph'].get(day)
        tgt = TARGET_TPH.get(k)
        rows.append({'key': k, 'name': pname(k), 'prod': r2(prod, 1), 'hours': r2(b['hours'].get(day)),
                     'tph': r2(tph), 'target_tph': tgt,
                     'tph_gap_pct': r2((tph - tgt) / tgt * 100, 1) if tph and tgt else None,
                     'spc_md': r2(spc_md), 'spc_plant': r2(spc_pl), 'aux_pct': r2(aux, 1),
                     'md_kwh': r2((pw.get('md_kwh') or {}).get(day), 0),
                     'total_kwh': r2((pw.get('total_kwh') or {}).get(day), 0),
                     'ck': r2(b['ck'].get(day), 3), 'clinker_pct': r2((b['clinker_pct'].get(day) or 0) * 100, 1),
                     'ga_gpt': r2(b['ga_gpt'].get(day), 0), 'blaine': r2(b['blaine'].get(day), 0),
                     'r45': r2(b['r45'].get(day)), 'whiteness': r2(b['whiteness'].get(day)),
                     'mtd': r2(b['mtd'].get(day), 1)})
    return rows


# ── 2. counters: chain, multi-interval products (B1), product order (B2) ──
def _intervals(series_by_product, day):
    """[(product, start, end)] for counters that have values on `day`."""
    out = []
    for k, ser in series_by_product.items():
        s, e = ser['start'].get(day), ser['end'].get(day)
        if s is not None and e is not None and (s or e):
            out.append((k, s, e))
    return out


def tile_day(iv, d0, tol):
    """Check that the day's intervals tile [d0, d1] of the cumulative counter.

    A product made twice in a day reports the SUM of its starts/ends (B1); we detect it by
    its start being ~k× the counter level and reconstruct its pieces from the gaps."""
    findings, segments = [], []
    if not iv:
        return d0, findings, segments
    singles_lvl = sorted(s for _, s, _ in iv)
    base = singles_lvl[0] if d0 is None else d0
    base = base or 1.0
    multi, single = [], []
    for k, s, e in iv:
        n = max(1, round(s / base)) if base > 0 else 1
        (multi if n > 1 else single).append((k, s, e, n))
    total = sum(e - s for _, s, e, _ in multi + single)
    if d0 is None:
        d0 = min([s for _, s, _, _ in single] or [base])
    d1 = d0 + total
    single.sort(key=lambda x: x[1])
    if single:                       # a gap inside the day pushes the real end further
        d1 = max(d1, single[-1][2])
    # gaps inside [d0, d1] not covered by single intervals
    gaps, cur = [], d0
    for k, s, e, _ in single:
        if s - cur > tol:
            gaps.append((cur, s))
        elif cur - s > tol:
            findings.append({'type': 'overlap', 'product': k, 'expected': r2(cur, 3), 'found': r2(s, 3)})
        cur = max(cur, e)
    if d1 - cur > tol:
        gaps.append((cur, d1))
    if not multi:
        for g0, g1 in gaps:
            findings.append({'type': 'gap', 'expected': r2(g0, 3), 'found': r2(g1, 3)})
        segments = [(k, s, e) for k, s, e, _ in single]
    elif len(multi) == 1:
        k, S, E, n = multi[0]
        gs, ge = sum(g[0] for g in gaps), sum(g[1] for g in gaps)
        if len(gaps) == n and abs(gs - S) <= tol * n and abs(ge - E) <= tol * n:
            segments = sorted([(kk, s, e) for kk, s, e, _ in single] + [(k, g0, g1) for g0, g1 in gaps],
                              key=lambda x: x[1])
            findings.append({'type': 'multi_ok', 'product': k, 'pieces': n})
        else:
            findings.append({'type': 'multi_mismatch', 'product': k, 'pieces': n,
                             'expected': r2(gs, 3), 'found': r2(S, 3)})
            segments = [(kk, s, e) for kk, s, e, _ in single]
    else:
        glen = sum(g1 - g0 for g0, g1 in gaps)
        mlen = sum(e - s for _, s, e, _ in multi)
        findings.append({'type': 'multi_many', 'products': [m[0] for m in multi],
                         'ok': abs(glen - mlen) <= tol * 2})
        segments = [(kk, s, e) for kk, s, e, _ in single]
    return d1, findings, segments


def counter_chain(series_by_product, days, target, tol, prod_days=None):
    """Walk the month day by day; return findings for `target` and the timeline of every day.

    The link to the previous day is checked only when the previous production day also used
    this counter: a weigh-feeder totalizer is re-assigned to other materials in between."""
    d1_prev, out, segs_by_day, last_used = None, {}, {}, None
    prod_days = sorted(prod_days or days)
    for d in days:
        iv = _intervals(series_by_product, d)
        if not iv:
            continue
        prev_prod = next((x for x in reversed(prod_days) if x < d), None)
        if last_used is None or last_used != prev_prod:
            d1_prev = None
        last_used = d
        has_multi = False
        d0 = None
        if d1_prev is not None:
            base = d1_prev
            has_multi = any(round(s / base) > 1 for _, s, _ in iv if base)
            observed = min(s for _, s, _ in iv if not base or round(s / base) <= 1) if not has_multi else d1_prev
            link = None
            if abs(observed - d1_prev) > tol:
                link = {'type': 'day_link', 'expected': r2(d1_prev, 3), 'found': r2(observed, 3)}
            d0 = observed
        d1, findings, segs = tile_day(iv, d0, tol)
        if d1_prev is not None and link:
            findings.insert(0, link)
        out[d] = findings
        segs_by_day[d] = segs
        d1_prev = d1
        if d == target:
            break
    return out.get(target, []), segs_by_day


def counters_section(mp, day, al):
    days = [d for d in mp['dates'] if d <= day]
    blocks = mp['blocks']
    prod_days = [d for d in days if any((b['production'].get(d) or 0) > 0 for b in blocks.values())]
    res = {'checks': [], 'timeline': [], 'prev_last': None}
    # running hours drive the product order
    rh = {k: b['counters']['Running h'] for k, b in blocks.items() if 'Running h' in b['counters']}
    f, segs = counter_chain(rh, days, day, TOL['Running h'], prod_days)
    tl = segs.get(day, [])
    res['timeline'] = [{'key': k, 'name': pname(k), 'start': r2(s, 2), 'end': r2(e, 2), 'hours': r2(e - s, 2)}
                       for k, s, e in tl]
    prev_days = [d for d in segs if d < day and segs[d]]
    if prev_days:
        res['prev_last'] = segs[prev_days[-1]][-1][0]
    checks = [('Running h', 'Running-hours counter', rh, TOL['Running h'], f)]
    for key, label in (('md', 'Mill drive (MD) power counter'), ('total', 'Total power counter')):
        ser = {k: p[key] for k, p in mp['power'].items() if key in p}
        ff, _ = counter_chain(ser, days, day, TOL[key], prod_days)
        checks.append((key, label, ser, TOL[key], ff))
    mats = sorted({m for b in blocks.values() for m in b['counters'] if m != 'Running h'})
    for m in mats:
        ser = {k: b['counters'][m] for k, b in blocks.items() if m in b['counters']}
        ff, _ = counter_chain(ser, days, day, TOL['material'], prod_days)
        if _intervals(ser, day):
            checks.append((m, f'{mname(m)} counter', ser, TOL['material'], ff))
    for key, label, ser, tol, ff in checks:
        for x in ff:
            t = x['type']
            if t == 'multi_ok':
                res['checks'].append({'counter': label, 'ok': True,
                                      'text': f"{pname(x['product'])} was produced in {x['pieces']} runs — the counter holds the sum of the starts, verified"})
                continue
            if t == 'multi_many':
                res['checks'].append({'counter': label, 'ok': x['ok'],
                                      'text': 'More than one product made in several runs — total check only'})
                if not x['ok']:
                    al.add('amber', 'Counters', f'{label}: the runs of the multi-run products do not fill the gaps')
                continue
            text = {'day_link': f"day starts at {x['found']} but the previous day ended at {x['expected']}",
                    'gap': f"counter gap from {x['expected']} to {x['found']}",
                    'overlap': f"overlap: {pname(x.get('product'))} starts at {x['found']}, expected {x['expected']}",
                    'multi_mismatch': f"{pname(x.get('product'))} in {x.get('pieces')} runs but the sum of starts {x['found']} ≠ {x['expected']}"}[t]
            res['checks'].append({'counter': label, 'ok': False, 'text': text})
            al.add('amber', 'Counters', f'{label}: {text}')
    # counter difference vs reported hours
    for k, b in blocks.items():
        c = b['counters'].get('Running h')
        hrs = b['hours'].get(day)
        if c and hrs:
            s, e = c['start'].get(day), c['end'].get(day)
            if s is not None and e is not None and abs((e - s) - hrs) > TOL['Running h']:
                txt = f"{pname(k)}: counter difference {r2(e - s)} h ≠ reported running hours {r2(hrs)} h"
                res['checks'].append({'counter': 'Running hours', 'ok': False, 'text': txt})
                al.add('amber', 'Counters', txt)
    if not any(not c['ok'] for c in res['checks']):
        res['checks'].append({'counter': 'All counters', 'ok': True, 'text': 'counter chain is continuous, no entry error'})
    return res


# ── 3. stoppages and the 24-hour rule (B3) ────────────────────────────────
def _norm_reason(s):
    s = re.sub(r'\d+([.:]\d+)?', ' ', str(s or '').lower())
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s؀-ۿ]', ' ', s)).strip()


def stoppages_section(mp, day, prods, al):
    tab = mp['daily_tabs'].get(day)
    run_h = sum(p['hours'] or 0 for p in prods)
    stops = tab['stops'] if tab else []
    stop_h = sum(s['hours'] or 0 for s in stops)
    pi = {k: (v.get(day) if isinstance(v, dict) else None) for k, v in mp['pi'].items()}
    res = {'stops': stops, 'run_h': r2(run_h), 'stop_h': r2(stop_h), 'pi': {k: r2(v, 3) for k, v in pi.items()},
           'tab_missing': tab is None}
    if run_h == 0:
        res['note'] = 'No production on this day — treated as a planned stop (Friday/holiday)'
    elif abs(run_h + stop_h - 24) > 0.1:
        al.add('amber', 'Stoppages', f'Running hours {r2(run_h)} + recorded stoppages {r2(stop_h)} = {r2(run_h + stop_h)}, not 24 h')
    pi_sum = sum(pi.get(k) or 0 for k in ('planned', 'idle', 'incident', 'net operation'))
    if pi.get('net operation') is not None and run_h > 0:
        if abs(pi_sum - 24) > 0.1 and run_h:
            al.add('amber', 'Stoppages', f'PI tab: planned + idle + incident + net operation = {r2(pi_sum)}, not 24')
        if abs((pi.get('net operation') or 0) - run_h) > 0.1:
            al.add('amber', 'Stoppages', f"PI tab: net operation {r2(pi.get('net operation'))} h ≠ product running hours {r2(run_h)} h")
    # repeated reasons this month
    agg = defaultdict(lambda: {'count': 0, 'hours': 0.0, 'days': [], 'text': '', 'dept': ''})
    for d, t in mp['daily_tabs'].items():
        if d.year != day.year or d.month != day.month or d > day:
            continue
        for s in t['stops']:
            key = _norm_reason(s['reason'])[:60]
            if not key:
                continue
            a = agg[key]
            a['count'] += 1
            a['hours'] += s['hours'] or 0
            a['days'].append(d.day)
            a['text'] = a['text'] or s['reason']
            a['dept'] = a['dept'] or s['dept']
    rep = sorted(agg.values(), key=lambda a: (-a['count'], -a['hours']))
    res['repeated'] = [{'reason': a['text'], 'dept': a['dept'], 'count': a['count'], 'hours': r2(a['hours']),
                        'days': sorted(set(a['days']))} for a in rep if a['count'] >= 2][:10]
    res['month_total_h'] = r2(sum(a['hours'] for a in agg.values()))
    for a in res['repeated'][:3]:
        if a['count'] >= 3:
            al.add('amber', 'Stoppages', f"Stoppage reason repeated {a['count']} times this month: {a['reason']} ({a['hours']} h)")
    return res


# ── 4. flushing losses ────────────────────────────────────────────────────
def flushing_section(mp, day, counters, al):
    m50, fl = mp['blocks'].get('m50'), mp['blocks'].get('flushing')

    def white_used(d):
        tons = defaultdict(float)
        for blk in (m50, fl):
            if not blk:
                continue
            for m in WHITE_CLINKERS:
                v = (blk['dry'].get(m) or {}).get(d)
                if v:
                    tons[m] += v
        return dict(tons)

    events, total_t = [], 0.0
    for d in sorted(mp['dates']):
        if d.year != day.year or d.month != day.month or d > day:
            continue
        w = white_used(d)
        t = sum(w.values())
        if t > 0.05:
            events.append({'day': d.isoformat(), 'tons': r2(t, 1), 'by': {mname(k): r2(v, 1) for k, v in w.items()},
                           'value_jd': r2(t * PRICE_WHITE_CLINKER, 0),
                           'loss_jd': r2(t * (PRICE_WHITE_CLINKER - PRICE_GREY_CLINKER), 0)})
            total_t += t
    today = next((e for e in events if e['day'] == day.isoformat()), None)
    # gray → white switches in today's timeline (including the boundary with yesterday)
    seq = ([counters['prev_last']] if counters.get('prev_last') else []) + [s['key'] for s in counters['timeline']]
    switches = [(a, b) for a, b in zip(seq, seq[1:]) if a in GREY | {'flushing'} and b in WHITE]
    res = {'events': events, 'today': today, 'mtd_tons': r2(total_t, 1),
           'mtd_loss_jd': r2(total_t * (PRICE_WHITE_CLINKER - PRICE_GREY_CLINKER), 0),
           'mtd_value_jd': r2(total_t * PRICE_WHITE_CLINKER, 0),
           'switches': [{'from': pname(a), 'to': pname(b)} for a, b in switches],
           'prices': {'white': PRICE_WHITE_CLINKER, 'grey': PRICE_GREY_CLINKER}}
    if today:
        al.add('info', 'Flushing', f"Grey → white switch: {today['tons']} t of white clinker lost in flushing — loss {today['loss_jd']} JD")
    elif switches:
        al.add('amber', 'Flushing', 'Grey → white switch today, but no white clinker recorded under M50/flushing')
    return res


# ── 5. month-to-date vs annual plan (G1, G2) ──────────────────────────────
def plan_section(mp, day, plan, al):
    ym = (day.year, day.month)
    p = (plan or {}).get('plan', {}).get(ym, {}) if plan else {}
    dim = calendar.monthrange(day.year, day.month)[1]
    days_left = dim - day.day
    holidays = (plan or {}).get('holidays', {}).get(ym, 0) if plan else 0
    month_days = [d for d in mp['dates'] if d.year == day.year and d.month == day.month and d <= day]
    rows, need_days = [], 0.0
    util_vals = [mp['pi'].get('utilization', {}).get(d) for d in month_days]
    util = mean([u for u in util_vals if u is not None]) or 1.0
    run_h_mtd = 0.0
    for k in PRODUCT_ORDER:
        b = mp['blocks'].get(k)
        if not b:
            continue
        mtd = b['mtd'].get(day)
        if mtd is None:
            mtd = sum(b['production'].get(d) or 0 for d in month_days)
        hrs = sum(b['hours'].get(d) or 0 for d in month_days)
        run_h_mtd += hrs
        target = p.get(k)
        if not mtd and not target:
            continue
        tph = TARGET_TPH.get(k) or (mtd / hrs if hrs else None)
        remaining = max(0.0, (target or 0) - (mtd or 0))
        nd = remaining / (tph * 24 * util) if tph and remaining else 0.0
        need_days += nd
        rows.append({'key': k, 'name': pname(k), 'mtd': r2(mtd, 1), 'plan': target,
                     'pct': r2(mtd / target * 100, 1) if target else None, 'remaining': r2(remaining, 1),
                     'tph_used': r2(tph), 'days_needed': r2(nd, 1)})
    total_mtd = sum(r['mtd'] or 0 for r in rows)
    total_plan = sum(v for v in p.values()) if p else None
    spare = days_left - holidays - need_days
    res = {'rows': rows, 'total_mtd': r2(total_mtd, 1), 'total_plan': total_plan,
           'total_pct': r2(total_mtd / total_plan * 100, 1) if total_plan else None,
           'days_left': days_left, 'holidays': holidays, 'need_days': r2(need_days, 1), 'spare_days': r2(spare, 1),
           'utilization': r2(util * 100, 1), 'run_h_mtd': r2(run_h_mtd, 1),
           'required_h': r2((plan or {}).get('required_hours', {}).get(ym)) if plan else None,
           'has_plan': bool(p)}
    if p:
        if spare < 0:
            al.add('amber', 'Plan', f'To meet the monthly plan the mill needs {r2(need_days, 1)} running days and {days_left - holidays} days are left — run on Fridays/holidays ({r2(-spare, 1)} extra days)')
        else:
            al.add('info', 'Plan', f'The mill can stop up to {r2(spare, 1)} days in the rest of the month and still meet the plan')
        for r in rows:
            if r['plan'] and r['pct'] is not None:
                expected = day.day / dim * 100
                if r['pct'] < expected - 25:
                    al.add('info', 'Plan', f"{r['name']}: {r['pct']}% of the monthly plan done, ≈{r2(expected, 0)}% expected by today")
    return res


# ── 6. stock, receipts, weeks left (C1, C2, C4) ───────────────────────────
def stock_section(mp, day, acq, al):
    st = mp['stock']
    final, wet, imp = st.get('final', {}), st.get('wet', {}), st.get('import', {})
    days = sorted(d for d in mp['dates'] if d <= day)
    run_days = [d for d in days if any((b['production'].get(d) or 0) > 0 for b in mp['blocks'].values())][-7:]
    rows = []
    for m in list(final):
        if m in ('Quality enhancer', 'Air entraining agent'):
            continue
        s = final[m].get(day)
        if s is None:
            continue
        cons = [(wet.get(m) or {}).get(d) or 0 for d in run_days]
        avg = mean(cons) if cons else None
        weeks = s / (avg * 7) if avg and avg > 0 and s > 0 else None
        rows.append({'material': m, 'name': mname(m), 'stock': r2(s, 1), 'avg_daily': r2(avg, 1),
                     'weeks': r2(weeks, 1), 'today_use': r2((wet.get(m) or {}).get(day), 1)})
        if s < 0:
            txt = f'{mname(m)} stock is negative ({r2(s, 1)} t)'
            if m in GREY_CLINKERS:
                other = 'Clinker J' if m == 'Clinker M' else 'Clinker M'
                os_ = (final.get(other) or {}).get(day)
                if os_ is not None:
                    txt += f' — deducted from {mname(other)}: corrected {r2(os_ + s, 1)} t'
            al.add('red', 'Stock', txt)
        elif weeks is not None and weeks < STOCK_WEEKS_ALARM and avg and avg > 1:
            al.add('amber', 'Stock', f'{mname(m)}: only {r2(weeks, 1)} weeks left at {r2(avg, 1)} t/day')
    res = {'rows': rows, 'avg_days': [d.isoformat() for d in run_days], 'receipts': [], 'acq_covers': None}
    if acq:
        last = max((d for v in acq['tons'].values() for d in v), default=None)
        res['acq_last'] = last.isoformat() if last else None
        res['acq_covers'] = bool(last and last >= day)
        mats = set(acq['tons']) | {m for m, v in imp.items() if (v.get(day) or 0) > 0}
        for m in sorted(mats):
            a = (acq['tons'].get(m) or {}).get(day) or 0.0
            s = (imp.get(m) or {}).get(day) or 0.0
            mtd = sum(v for d, v in (acq['tons'].get(m) or {}).items()
                      if d.year == day.year and d.month == day.month and d <= day)
            if not a and not s and not mtd:
                continue
            diff = s - a
            bad = abs(diff) > max(5.0, 0.02 * max(a, s)) and res['acq_covers']
            res['receipts'].append({'material': m, 'name': mname(m), 'scale': r2(a, 1), 'stock_tab': r2(s, 1),
                                    'diff': r2(diff, 1), 'trucks': (acq['trucks'].get(m) or {}).get(day, 0),
                                    'mtd_scale': r2(mtd, 1), 'bad': bool(bad), 'covered': bool(res['acq_covers'])})
            if bad:
                al.add('amber', 'Receipts', f'{mname(m)}: truck scale {r2(a, 1)} t ≠ stock tab {r2(s, 1)} t (truck scale is the reference)')
        if not res['acq_covers']:
            al.add('info', 'Receipts', f"Truck-scale file ends on {res.get('acq_last')} and does not cover this day")
        if 'Clinker M' not in acq['tabs']:
            res['note_m'] = 'No "Clinker M" tab in the truck-scale file yet'
    return res


# ── 7. moisture vs lab (C3) ───────────────────────────────────────────────
def moisture_section(mp, day, lab, al):
    used = set()
    moist = {}
    for b in mp['blocks'].values():
        if (b['production'].get(day) or 0) <= 0:
            continue
        for m, v in b['wet'].items():
            if (v.get(day) or 0) > 0 and m in b['moisture']:
                used.add(m)
                moist[m] = b['moisture'][m].get(day)
    rows = []
    for m in sorted(used):
        if m in ('Grinding aids', 'Quality enhancer', 'Air entraining agent'):
            continue
        p = moist.get(m)
        samples = [s for s in lab if s['material'] == m and s['date'] <= day and 'feeder' in s['location'].lower()]
        s = max(samples, key=lambda x: x['date']) if samples else None
        row = {'material': m, 'name': mname(m), 'prod_pct': r2(p * 100 if p is not None else None, 3),
               'lab_pct': r2(s['water_pct'], 3) if s else None, 'lab_date': s['date'].isoformat() if s else None,
               'age_days': (day - s['date']).days if s else None}
        if s and p is not None:
            diff = p * 100 - s['water_pct']
            row['diff'] = r2(diff, 2)
            row['bad'] = abs(diff) > MOIST_TOL_PP
            if row['bad']:
                al.add('amber', 'Moisture', f"{mname(m)}: production report moisture {row['prod_pct']}% vs lab {row['lab_pct']}% (sample {row['lab_date']})")
        rows.append(row)
    return {'rows': rows}


# ── 8. QC hour ↔ production hour (D3 exact) and day QC records ───────────
def qc_day(qc, day):
    return {r['hour']: r for r in qc['hourly'] if r['date'] == day} if qc else {}


def qc_match_section(mp, day, qch, al):
    """Production hourly block row H holds the QC result of hour H-1 (verified on Aug data)."""
    fields = (('blaine', 'blaine', 'Blaine', 0.5), ('r45', 'r45', 'R45', 1e-6), ('wi', 'l_star', 'Whiteness L*', 1e-6))
    mism, compared = [], 0
    for h in range(24):
        qh = (h - 1) % 24
        q = qch.get(qh)
        for pf, qf, label, tol in fields:
            pv = None
            for k, b in mp['blocks'].items():
                v = b['hourly'].get(pf, {}).get(h, {}).get(day)
                if v:
                    pv, pk = v, k
                    break
            qv = q.get(qf) if q else None
            qv = qv if isinstance(qv, (int, float)) and qv else None
            if pv is None or qv is None:
                continue
            compared += 1
            if abs(pv - qv) > tol:
                mism.append({'hour': f'{qh:02d}:00', 'field': label, 'production': r2(pv, 3), 'qc': r2(qv, 3),
                             'product': pname(pk)})
    if mism:
        al.add('amber', 'QC match', f'{len(mism)} values in the production report do not match the QC report (R45/Blaine/whiteness) — they must match exactly')
    return {'compared': compared, 'mismatches': mism}


# ── 9. grinding aid dosage (D1, D2, C5) ───────────────────────────────────
def chem_section(mp, day, qch, qc, proc, prods, al):
    rows, hours = [], []
    badge = latest_badges(qc, day) if qc else {}
    feed_by_hour = {}
    if proc:
        ff = proc['series'].get('fresh feed') or []
        acc = defaultdict(list)
        for t, v in zip(proc['time'], ff):
            if v and v > 5:
                acc[t.hour].append(v)
        feed_by_hour = {h: mean(v) for h, v in acc.items()}
    for p in prods:
        k = p['key']
        b = mp['blocks'][k]
        dens = (b['chem_density'].get('Grinding aids') or {}).get(day)
        if dens and abs(dens - GA_DENSITY) > GA_DENSITY_TOL + 1e-9:
            al.add('amber', 'Chemicals', f'{p["name"]}: grinding-aid density in the report {dens} is outside {GA_DENSITY}±{GA_DENSITY_TOL}')
        qhrs = [r for r in qch.values() if r['product'] == k and r['chemicals']]
        if not qhrs:
            rows.append({'key': k, 'name': p['name'], 'prod_gpt': p['ga_gpt']})
            continue
        ml_min = mean([sum(r['chemicals'].values()) for r in qhrs])
        feeds = [feed_by_hour.get(r['hour']) for r in qhrs]
        feed = mean(feeds) or p['tph']
        qc_gpt = ml_min * 60 * GA_DENSITY / feed if feed else None
        bd = badge.get(k, {}).get('dose')
        row = {'key': k, 'name': p['name'], 'qc_ml_min': r2(ml_min, 1), 'feed_tph': r2(feed),
               'qc_gpt': r2(qc_gpt, 0), 'prod_gpt': p['ga_gpt'], 'badge_ml_min': bd}
        if qc_gpt and p['ga_gpt']:
            diff = (p['ga_gpt'] - qc_gpt) / qc_gpt * 100
            row['diff_pct'] = r2(diff, 1)
            if abs(diff) > GA_TOL_PCT:
                al.add('amber', 'Chemicals', f"{p['name']}: grinding-aid dosage {p['ga_gpt']} g/t in production vs {r2(qc_gpt, 0)} g/t from QC ({r2(diff, 1)}% difference)")
        if bd and abs(ml_min - bd) > 0.5:
            al.add('amber', 'Chemicals', f'{p["name"]}: QC dosage {r2(ml_min, 1)} ml/min vs product batch dosage {bd} ml/min')
        rows.append(row)
    # D2: R45 above target while dosage below target
    for h, r in sorted(qch.items(), key=lambda x: (x[0] - 8) % 24):
        lim = badge.get(r['product'], {}).get('limits', {}).get('r45')
        dose = sum(r['chemicals'].values()) if r['chemicals'] else None
        r45 = r['r45'] if isinstance(r['r45'], (int, float)) else None
        bd = badge.get(r['product'], {}).get('dose')
        hours.append({'hour': f'{h:02d}:00', 'r45': r45, 'dose': dose, 'feed': r2(feed_by_hour.get(h))})
        if lim and lim[1] is not None and r45 is not None and r45 > lim[1] and dose is not None and bd and dose < bd:
            al.add('amber', 'Chemicals', f'{h:02d}:00: R45 {r45} above target while dosage {dose} is below target {bd}')
    return {'rows': rows, 'hours': hours}


def latest_badges(qc, day):
    out = {}
    for b in sorted(qc['badge'], key=lambda x: (x['date'], x['serial'] or 0)):
        if b['date'] <= day and b['product']:
            out[b['product']] = b
    return out


# ── 10. proportions: production (wet) vs QC vs badge vs weigh feeders (D4, C2) ──
QC_ADD_MAP = {'gyps': None, 'l.s': 'Limestone HG', 'pozz': 'Pozzolana', 'basalt': 'Basalt', 'silica': 'Sand'}


def _qc_material(group_label):
    g, _, label = group_label.partition(':')
    g = g.strip()
    if g.startswith('gyps'):
        return material_key(label) or 'Gypsum Az'
    for k, v in QC_ADD_MAP.items():
        if g.startswith(k):
            return v
    return material_key(label)


def qc_clinker_set(src):
    """QC clinker column → production clinker(s); the mixes name more than one source."""
    n = str(src).lower()
    if 'j+m' in n.replace(' ', ''):
        return {'Clinker J', 'Clinker M'}
    if 'newj' in n.replace(' ', '') or 'oldj' in n.replace(' ', ''):
        return {'Clinker J'}
    mk = material_key(src)
    return {mk} if mk else {src}


def proportions_section(mp, day, qch, qc, proc, prods, al):
    badge = latest_badges(qc, day) if qc else {}
    out = []
    for p in prods:
        k = p['key']
        b = mp['blocks'][k]
        wet = {m: v.get(day) for m, v in b['wet'].items()
               if m not in ('Grinding aids', 'Quality enhancer', 'Air entraining agent') and (v.get(day) or 0) > 0}
        tot = sum(wet.values())
        prod_pct = {m: v / tot * 100 for m, v in wet.items()} if tot else {}
        qhrs = [r for r in qch.values() if r['product'] == k]
        qc_pct = defaultdict(float)
        clinker_src = Counter()
        qc_sets = {}
        for r in qhrs:
            for src, v in r['clinker'].items():
                ms = qc_clinker_set(src)
                mk = next(iter(ms)) if len(ms) == 1 else src
                qc_sets[mk] = ms
                qc_pct[mk] += v / len(qhrs)
                clinker_src[mk] += 1
            for g, v in r['additives'].items():
                mk = _qc_material(g) or g
                qc_pct[mk] += v / len(qhrs)
        # clinker is compared as a total; which clinker source is used is the C2 red alarm below
        for src in (prod_pct, qc_pct):
            clk = sum(v for m, v in src.items() if m.startswith('Clinker'))
            if clk:
                src['Clinker (total)'] = clk
        rows = []
        for m in sorted(set(prod_pct) | set(qc_pct), key=lambda x: (not x.startswith('Clinker'), x)):
            pp, qp = prod_pct.get(m), qc_pct.get(m)
            d = (pp or 0) - (qp or 0)
            bad = qhrs and abs(d) > PROP_TOL_PP and not (m.startswith('Clinker') and m != 'Clinker (total)')
            rows.append({'material': m, 'name': mname(m), 'prod': r2(pp, 1), 'qc': r2(qp, 1), 'diff': r2(d, 1),
                         'bad': bool(bad)})
            if bad:
                al.add('amber', 'Proportions', f"{p['name']}: {mname(m)} {r2(pp or 0, 1)}% in production vs {r2(qp or 0, 1)}% in QC instructions")
        # C2: clinker named by QC must be the clinker consumed
        prod_clk = {m for m in wet if m.startswith('Clinker')}
        qc_clk = {m for m in clinker_src}
        for m in sorted(m for m in qc_clk if not (qc_sets.get(m, {m}) & prod_clk)):
            al.add('red', 'Clinker source', f"{p['name']}: QC records {mname(m)} for {clinker_src[m]} h but production consumed {', '.join(mname(x) for x in sorted(prod_clk)) or '—'}")
        bdg = badge.get(k)
        target = None
        if bdg:
            bp = bdg['props']
            target = {'clinker': bp.get('clinker'), 'limestone': bp.get('limestone'), 'pozzolana': bp.get('pozzolana'),
                      'gypsum': bp.get('gypsum'), 'serial': bdg['serial'], 'date': bdg['date'].isoformat()}
        out.append({'key': k, 'name': p['name'], 'rows': rows, 'badge': target,
                    'qc_hours': len(qhrs), 'qc_clinkers': sorted(mname(m) for m in qc_clk)})
    # weigh feeders (DCS) — category level
    wf = None
    if proc:
        names = proc['wf_names']
        rates = {}
        run = [i for i, v in enumerate(proc['series'].get('fresh feed') or []) if v and v > 5]
        for n, label in names.items():
            col = next((h for h in proc['series'] if h.startswith(f'wf{n} feeding') or h.startswith(f'wf {n} feeding')), None)
            vals = [proc['series'][col][i] for i in run] if col else []
            rates[n] = mean([v for v in vals if v is not None]) or 0.0
        tot = sum(rates.values())
        wf = [{'wf': f'WF{n}', 'material': names.get(n) or '—', 'tph': r2(rates[n]),
               'pct': r2(rates[n] / tot * 100 if tot else None, 1)} for n in sorted(rates)]
    return {'products': out, 'wf': wf}


# ── 11. process data: six charts + alarms (Analyses doc, E1–E8, F1, F2) ──
def _bucket(times, series, minutes=5):
    out_t, out = [], {k: [] for k in series}
    if not times:
        return out_t, out
    t0 = times[0]
    cur, acc = None, defaultdict(list)
    for i, t in enumerate(times):
        b = int((t - t0).total_seconds() // (minutes * 60))
        if cur is None:
            cur = b
        if b != cur:
            out_t.append((t0 + dt.timedelta(minutes=cur * minutes)).strftime('%H:%M'))
            for k in series:
                out[k].append(r2(mean(acc[k]), 2))
            acc = defaultdict(list)
            cur = b
        for k, s in series.items():
            acc[k].append(s[i] if i < len(s) else None)
    out_t.append((t0 + dt.timedelta(minutes=cur * minutes)).strftime('%H:%M'))
    for k in series:
        out[k].append(r2(mean(acc[k]), 2))
    return out_t, out


def interp(x, xs, ys):
    """Linear interpolation, extrapolating the end segments."""
    if x is None or len(xs) < 2:
        return None
    for i in range(1, len(xs)):
        if x <= xs[i] or i == len(xs) - 1:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 != x0 else y0
    return None


def fan_check(rpm, kw, table):
    """E3: blocked when actual kW sits >2 table steps below the kW expected at this rpm."""
    if not table or rpm is None or kw is None:
        return None
    rp, kc, vel = [t[0] for t in table], [t[1] for t in table], [t[2] for t in table]
    kw_calc = interp(rpm, rp, kc)
    rpm_eq = interp(kw, kc, rp)
    step = mean([b - a for a, b in zip(rp[-4:], rp[-3:])]) or 50.0
    steps = (rpm - rpm_eq) / step if rpm_eq is not None else None
    v = interp(kw, kc, vel) if all(x is not None for x in vel) else None
    return {'kw_calc': kw_calc, 'rpm_eq': rpm_eq, 'steps': steps, 'velocity': v}


def _minutes(times, mask):
    if len(times) < 2:
        return 0.0
    dtm = (times[-1] - times[0]).total_seconds() / 60 / max(1, len(times) - 1)
    return sum(1 for m in mask if m) * dtm


def process_section(proc, day, qch, qc, prods, sales, fan, al):
    S = proc['series']
    g = lambda *names: next((S[n] for n in names if n in S), [])
    t = proc['time']
    feed, rej, md = g('fresh feed'), g('reject flow'), g('mill drive power')
    be = g('mill bucket elevator drive power')
    run = [bool(v and v > 5) for v in feed]
    badge = latest_badges(qc, day) if qc else {}
    res = {'charts': {}, 'findings': [], 'samples': len(t),
           'window': f"{t[0]:%Y-%m-%d %H:%M} → {t[-1]:%Y-%m-%d %H:%M}" if t else None}
    F = res['findings']

    def finding(sev, area, text):
        F.append({'sev': sev, 'text': text})
        if sev != 'info':
            al.add(sev, area, text)
        else:
            al.add('info', area, text)

    # chart 1: silos (tons — F1: silos are conical, tons are the reliable value)
    silo_t = {n: g(f'tn silo {n} y value') for n in range(1, 6)}
    silo_pct = {n: g(f'silo {n} level %') for n in range(1, 6)}
    if not any(silo_t.values()):
        silo_t = {n: [None if v is None else v * SILOS[n][0] / 100 for v in silo_pct[n]] for n in range(1, 6)}
    tt, b1 = _bucket(t, {f's{n}': silo_t[n] for n in range(1, 6)})
    silo_products = defaultdict(list)
    for h in sorted(qch, key=lambda x: (x - 8) % 24):
        r = qch[h]
        if isinstance(r.get('silo'), (int, float)) and r.get('product'):
            silo_products[int(r['silo'])].append((h, r['product']))
    seq = []
    for h in sorted(qch, key=lambda x: (x - 8) % 24):
        p_ = qch[h].get('product')
        sl = qch[h].get('silo')
        if p_ and (not seq or seq[-1][1] != p_ or seq[-1][2] != sl):
            seq.append((h, p_, sl))
    res['charts']['silos'] = {'t': tt, 'series': b1, 'cap': {n: SILOS[n][0] for n in SILOS}}
    res['product_sequence'] = [{'hour': f'{h:02d}:00', 'product': pname(p_), 'silo': s} for h, p_, s in seq]
    # filling rate: least-squares slope of the receiving silo, per hour with no truck on the loading scale
    scale = g('truck weight at ibau y value')
    fill = []
    for h, r in qch.items():
        s, p_ = r.get('silo'), r.get('product')
        if not isinstance(s, (int, float)) or not p_ or int(s) not in silo_t:
            continue
        idx = [i for i, x in enumerate(t) if x.hour == h]
        ser = silo_t[int(s)]
        if len(idx) < 15 or not all(run[i] for i in idx):
            continue
        if any(scale and i < len(scale) and scale[i] and scale[i] > 2000 for i in idx):
            continue
        xs = [(t[i] - t[idx[0]]).total_seconds() / 3600 for i in idx if ser[i] is not None]
        ys = [ser[i] for i in idx if ser[i] is not None]
        if len(xs) < 10:
            continue
        mx, my = mean(xs), mean(ys)
        den = sum((x - mx) ** 2 for x in xs)
        if den:
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
            fill.append({'silo': int(s), 'hour': f'{h:02d}:00', 'product': pname(p_), 'rate_tph': r2(slope, 1)})
    fill.sort(key=lambda x: (int(x['hour'][:2]) - 8) % 24)
    res['fill_rates'] = fill
    for p in prods:
        rates = [f_['rate_tph'] for f_ in fill if f_['product'] == p['name'] and f_['rate_tph']]
        if rates and p['tph']:
            avg = mean(rates)
            res.setdefault('fill_vs_prod', []).append({'product': p['name'], 'fill_tph': r2(avg, 1), 'prod_tph': p['tph']})
            if abs(avg - p['tph']) / p['tph'] > 0.15:
                finding('amber', 'Silos', f"{p['name']}: silo filling rate {r2(avg, 1)} t/h vs report productivity {p['tph']} t/h")
    # QC silo level vs DCS %
    lvl = []
    for h, r in qch.items():
        s, q = r.get('silo'), r.get('silo_level')
        if not isinstance(s, (int, float)) or not isinstance(q, (int, float)) or int(s) not in silo_pct:
            continue
        idx = [i for i, x in enumerate(t) if x.hour == h]
        if not idx:
            continue
        v = silo_pct[int(s)][idx[0]] if idx[0] < len(silo_pct[int(s)]) else None
        if v is not None:
            lvl.append({'hour': f'{h:02d}:00', 'silo': int(s), 'qc': r2(q * 100, 0), 'dcs': r2(v, 1), 'diff': r2(v - q * 100, 1)})
    res['silo_level_check'] = lvl
    off = [x for x in lvl if abs(x['diff']) > 10]
    if off:
        finding('amber', 'Silos', f'Silo level in the QC report differs from DCS by more than 10% in {len(off)} hours')
    # truck loading (F2): each bulk truck shows on the loading scale (0 → ~45 t); the silo
    # level is compared 10 min before vs 10 min after, because aeration makes it noisy
    res['loading'] = loading_check(t, silo_t, g('truck weight at ibau y value'), sales, qch, prods)
    lo = res['loading']
    if sales is not None and lo['events_count'] != lo['m50_trucks']:
        finding('amber', 'Silos & sales', f"M50 loadings from the silos ({lo['events_count']}) ≠ M50 trucks in sales ({lo['m50_trucks']})")
    off_lvl = [e for e in lo['events'] if e['net_t'] and e['drop_t'] is not None and abs(e['drop_t'] - e['net_t']) > 5]
    if off_lvl:
        finding('amber', 'Silos & sales', f"In {len(off_lvl)} of {len(lo['events'])} loadings the silo level drop differs from the truck net weight by more than 5 t — the level reading during discharge is unreliable")
    for e in lo['events']:
        if e['net_t'] and abs(e['scale_t'] - e['net_t']) > 2:
            finding('amber', 'Silos & sales', f"Truck {e['truck']} ({e['time']}): loading scale {e['scale_t']} t vs weighbridge ticket {e['net_t']} t")

    # chart 2: mill drive power / fresh feed / reject / bucket elevator
    tt, b2 = _bucket(t, {'md': md, 'feed': feed, 'reject': rej, 'be': be})
    res['charts']['mill'] = {'t': tt, 'series': b2}
    over = _minutes(t, [v is not None and v > 13 for v in be])
    under = _minutes(t, [r_ and v is not None and v < 10 for v, r_ in zip(be, run)])
    if over > 0:
        finding('red', 'Mill', f'Mill bucket elevator above 13 A for {r2(over, 0)} min — mill overfilled')
    if under > 60:
        finding('info', 'Mill', f'Mill bucket elevator below 10 A for {r2(under, 0)} min (normal 10–11.5)')
    # empty mill: MD power high with low feed and low reject
    mdv = sorted(v for v in md if v)
    if mdv:
        p90 = mdv[int(len(mdv) * 0.9)]
        fv = sorted(v for v in feed if v)
        rv = sorted(v for v in rej if v)
        f10, r10 = fv[int(len(fv) * 0.1)], rv[int(len(rv) * 0.1)]
        empty = _minutes(t, [m_ and f_ is not None and rr is not None and m_ >= p90 and f_ <= f10 and rr <= r10
                             for m_, f_, rr in zip(md, feed, rej)])
        if empty >= 10:
            finding('info', 'Mill', f'Empty-mill pattern (high drive power with low feed and reject) for {r2(empty, 0)} min')
    # feed cut-offs: WF totalizer flat while running (E8: ≥ 4 minutes)
    cuts = []
    for n in range(1, 5):
        tot = g(f'wf{n} totalizer', f'wf {n} totalizer')
        rate = g(f'wf{n} feeding rate t h', f'wf {n} feeding rate t h')
        name = proc['wf_names'].get(n) or f'WF{n}'
        if not tot or not rate or not any(v and v > 0.3 for v in rate):
            continue
        i = 1
        while i < len(tot):
            if run[i] and tot[i] is not None and tot[i - 1] is not None and abs(tot[i] - tot[i - 1]) < 1e-6:
                j = i
                while j + 1 < len(tot) and run[j + 1] and tot[j + 1] is not None and abs(tot[j + 1] - tot[i]) < 1e-6:
                    j += 1
                mins = (t[j] - t[i - 1]).total_seconds() / 60
                if mins >= 4:
                    cuts.append({'wf': f'WF{n}', 'material': name, 'from': f'{t[i - 1]:%H:%M}', 'to': f'{t[j]:%H:%M}',
                                 'minutes': r2(mins, 0)})
                i = j + 1
            else:
                i += 1
    res['feed_cuts'] = cuts
    for c in cuts[:5]:
        finding('amber', 'Feeding', f"{c['material']} cut off ({c['wf']}) from {c['from']} to {c['to']} ({c['minutes']} min)")
    # separator fan current (E4) and correlation with reject
    aw = [v for v in g('separator fan drive current') if v is not None]
    if aw:
        avg = mean(aw)
        out_ = sum(1 for v in aw if abs(v - avg) > 1) / len(aw) * 100
        res['sep_fan'] = {'avg': r2(avg, 1), 'min': r2(min(aw), 1), 'max': r2(max(aw), 1), 'outside_pct': r2(out_, 0),
                          'r_reject': r2(pearson(g('separator fan drive current'), rej), 2)}
        if not 120 <= avg <= 130:
            finding('amber', 'Separator', f'Separator fan current average {r2(avg, 1)} A is outside 120–130 A')
        if out_ > 10:
            finding('amber', 'Separator', f'Separator fan current outside ±1 A of its average {r2(out_, 0)}% of the time')

    # chart 3: R45 vs separator speed / separator fan speed
    tt, b3 = _bucket(t, {'r45_dcs': g('r45'), 'sep_speed': g('separator speed'), 'sep_fan_speed': g('separator fan speed'),
                         'feed': feed, 'mill_fan_speed': g('mill fan speed')}, minutes=10)
    qc_r45 = [{'hour': f'{h:02d}:00', 'r45': r['r45'] if isinstance(r['r45'], (int, float)) else None,
               'product': pname(r['product'])} for h, r in sorted(qch.items(), key=lambda x: (x[0] - 8) % 24)]
    res['charts']['r45'] = {'t': tt, 'series': b3, 'qc': qc_r45}
    actions = []
    for h, r in qch.items():
        lim = badge.get(r['product'], {}).get('limits', {}).get('r45')
        if not lim or lim[1] is None or not isinstance(r['r45'], (int, float)) or r['r45'] <= lim[1]:
            continue
        nxt = [i for i, x in enumerate(t) if x.hour in (h, (h + 1) % 24)]
        acts = []
        for label, ser, thr in (('fresh feed', feed, 0.5), ('mill fan speed', g('mill fan speed'), 5),
                                ('separator speed', g('separator speed'), 5)):
            vals = [ser[i] for i in nxt if i < len(ser) and ser[i] is not None]
            if vals and max(vals) - min(vals) >= thr:
                acts.append(f'{label} moved between {r2(min(vals), 1)} and {r2(max(vals), 1)}')
        actions.append({'hour': f'{h:02d}:00', 'product': pname(r['product']), 'r45': r['r45'], 'target': lim[1],
                        'actions': acts})
        finding('amber', 'Fineness R45', f"{h:02d}:00 {pname(r['product'])}: R45 = {r['r45']} above target ≤{lim[1]}" +
                (f" — operator action: {', '.join(acts)}" if acts else ' — no operator action visible'))
    res['r45_actions'] = actions

    # chart 4: temperatures
    gas, cem, inlet = g('mill outlet gas temperature'), g('final cement temperature'), g('mill inlet gas temperature')
    tt, b4 = _bucket(t, {'gas_out': gas, 'cement': cem, 'gas_in': inlet})
    lim_c = None
    for p in prods:
        lim_c = (badge.get(p['key'], {}).get('limits', {}) or {}).get('temp_cement') or lim_c
    res['charts']['temps'] = {'t': tt, 'series': b4, 'gas_target': GAS_T, 'cement_target': lim_c}
    off_gas = _minutes(t, [r_ and v is not None and not GAS_T[0] <= v <= GAS_T[1] for v, r_ in zip(gas, run)])
    if off_gas > 30:
        finding('amber', 'Temperature', f'Mill outlet gas temperature outside 90–100 °C for {r2(off_gas, 0)} min')
    if lim_c and lim_c[1]:
        hot = _minutes(t, [r_ and v is not None and v > lim_c[1] for v, r_ in zip(cem, run)])
        if hot > 30:
            finding('amber', 'Temperature', f'Final cement temperature above {lim_c[1]} °C for {r2(hot, 0)} min (target {lim_c[0]}–{lim_c[1]})')
    heater = _minutes(t, [v is not None and v > 100 for v in inlet])
    res['heater_minutes'] = r2(heater, 0)

    # chart 5: ventilation + fan blockage (E3) + RH + pressures (E5)
    fspd, fkw = g('mill fan speed'), g('mill fan drive power')
    ah, ag = g('mill inlet gas pressure'), g('mill outlet gas pressure')
    rh = g('mill inlet gas rh')
    dp = [a - b if a is not None and b is not None else None for a, b in zip(ah, ag)]
    tt, b5 = _bucket(t, {'fan_speed': fspd, 'fan_kw': fkw, 'p_out': ag, 'filter_dp': g('mill filter dp'),
                         'filling': g('mill filling'), 'mill_dp': dp})
    res['charts']['vent'] = {'t': tt, 'series': b5}
    if fan:
        chk = [fan_check(a, b, fan) for a, b in zip(fspd, fkw)]
        steps = [c['steps'] for c in chk if c and c['steps'] is not None]
        blocked = _minutes(t, [r_ and c is not None and c['steps'] is not None and c['steps'] > 2 for c, r_ in zip(chk, run)])
        vel = mean([c['velocity'] for c in chk if c and c['velocity'] is not None])
        res['fan'] = {'avg_rpm': r2(mean(fspd), 0), 'avg_kw': r2(mean(fkw), 1),
                      'kw_calc': r2(mean([c['kw_calc'] for c in chk if c and c['kw_calc'] is not None]), 1),
                      'avg_steps': r2(mean(steps), 1), 'blocked_min': r2(blocked, 0), 'velocity': r2(vel, 3),
                      'beyond_table': bool(fspd and max(v for v in fspd if v is not None) > fan[-1][0])}
        if blocked > 30:
            finding('red', 'Ventilation', f"Mill blocked / ventilation insufficient for {r2(blocked, 0)} min: fan power {res['fan']['avg_kw']} kW vs calculated {res['fan']['kw_calc']} kW at {res['fan']['avg_rpm']} rpm ({res['fan']['avg_steps']} steps lower)")
    rh_hi = _minutes(t, [v is not None and 35 < v <= 100 for v in rh])
    rh_bad = _minutes(t, [v is not None and v > 100 for v in rh])
    if rh_hi > 0:
        finding('amber', 'Ventilation', f'Mill inlet gas RH above 35% for {r2(rh_hi, 0)} min')
    if rh_bad > 0:
        finding('amber', 'Sensors', f'Mill inlet RH sensor read above 100% for {r2(rh_bad, 0)} min — sensor error')
    pos = _minutes(t, [r_ and v is not None and v > 0 for v, r_ in zip(ah, run)])
    if pos > 30:
        finding('amber', 'Ventilation', f'Mill inlet pressure positive for {r2(pos, 0)} min (average {r2(mean(ah), 2)} mbar, target −1 to 0) — reduce the heater fan or check ventilation')
    hi_dp = _minutes(t, [r_ and v is not None and v >= 25 for v, r_ in zip(dp, run)])
    res['mill_dp_avg'] = r2(mean(dp), 1)
    if hi_dp > 30:
        finding('amber', 'Ventilation', f'Mill dP ≥ 25 mbar for {r2(hi_dp, 0)} min (average {r2(mean(dp), 1)})')

    # chart 6: separator air-seal pressure vs separator fan current
    az = g('separator air seal fan pressure')
    tt, b6 = _bucket(t, {'air_seal': az, 'sep_fan_a': g('separator fan drive current')})
    res['charts']['seal'] = {'t': tt, 'series': b6}
    low = _minutes(t, [v is not None and v < 16 for v in az])
    res['air_seal'] = {'avg': r2(mean(az), 2), 'min': r2(min((v for v in az if v is not None), default=None), 2),
                       'low_min': r2(low, 0), 'r_current': r2(pearson(az, g('separator fan drive current')), 2)}
    if low > 0:
        finding('red' if low > 30 else 'amber', 'Separator', f'Separator air-seal pressure below 16 mbar for {r2(low, 0)} min')
    return res


def loading_check(t, silo_t, scale, sales, qch, prods):
    from statistics import median
    events, i = [], 0
    while i < len(scale):
        if scale[i] is not None and scale[i] > 2000:
            j = i
            while j + 1 < len(scale) and scale[j + 1] is not None and scale[j + 1] > 2000:
                j += 1
            mx = max(v for v in scale[i:j + 1] if v is not None)
            if mx >= 20000:
                events.append((i, j, mx / 1000.0))
            i = j + 1
        else:
            i += 1
    tph = mean([p['tph'] for p in prods if p['key'] == 'm50' and p['tph']]) or 0
    trucks = [s for s in (sales or []) if s['product'] == 'm50']
    used, out = set(), []
    for a, b, mx in events:
        ta, tb = t[a], t[b]
        pre = lambda ser: [ser[k] for k in range(len(ser)) if ser[k] is not None and ta - dt.timedelta(minutes=10) <= t[k] < ta]
        post = lambda ser: [ser[k] for k in range(len(ser)) if ser[k] is not None and tb < t[k] <= tb + dt.timedelta(minutes=10)]
        best = None
        for n in (1, 2):
            ser = silo_t.get(n) or []
            p0, p1 = pre(ser), post(ser)
            if not p0 or not p1:
                continue
            drop = median(p0) - median(p1)
            if (qch.get(ta.hour) or {}).get('silo') == n and tph:      # mill was filling this silo meanwhile
                drop += tph * ((tb - ta).total_seconds() / 3600 + 10 / 60)
            if best is None or drop > best[1]:
                best = (n, drop)
        k = None
        for idx, s in enumerate(trucks):
            if idx in used or not s['time_in']:
                continue
            if s['time_in'] - dt.timedelta(minutes=10) <= ta <= (s['time_out'] or s['time_in']) + dt.timedelta(minutes=10):
                k = idx
                break
        if k is not None:
            used.add(k)
        s = trucks[k] if k is not None else None
        out.append({'time': f'{ta:%H:%M}', 'silo': best[0] if best else None,
                    'drop_t': r2(best[1], 1) if best else None, 'scale_t': r2(mx, 2),
                    'truck': s['plate'] if s else None, 'net_t': r2(s['net_t'], 2) if s else None})
    return {'events': out, 'events_count': len(events), 'm50_trucks': len(trucks),
            'unmatched_trucks': [{'plate': s['plate'], 'time': f"{s['time_in']:%H:%M}" if s['time_in'] else None,
                                  'net_t': r2(s['net_t'], 2)} for i, s in enumerate(trucks) if i not in used]}


# ── 12. quality trend: strength, setting, SO3 (D5) ────────────────────────
REF_FIELDS = [('blaine', 'Blaine', 0), ('r45', 'R45', 2), ('l_star', 'L*', 2), ('so3', 'SO3', 2),
              ('setting', 'Initial setting', 0), ('s2', '2D', 1), ('s7', '7D', 1), ('s28', '28D', 1)]


def reference_section(qc, summary, day, al):
    """This month so far (from the hourly QC log) against the monthly QC summary (answer D5)."""
    if not summary:
        return None
    cur = (day.year, day.month)
    mtd = defaultdict(lambda: defaultdict(list))
    for r in (qc or {}).get('hourly', []):
        if (r['date'].year, r['date'].month) != cur or r['date'] > day or not r['product'] or r['product'] == 'transition':
            continue
        for f, src in (('blaine', 'blaine'), ('r45', 'r45'), ('l_star', 'l_star'), ('setting', 'set_initial'),
                       ('s2', 's2'), ('s7', 's7'), ('s28', 's28')):
            v = r.get(src)
            if isinstance(v, (int, float)) and v:
                mtd[r['product']][f].append(v)
        so3 = r.get('so3_chem') if isinstance(r.get('so3_chem'), (int, float)) else r.get('so3_xrf')
        if isinstance(so3, (int, float)) and so3:
            mtd[r['product']]['so3'].append(so3)
    out = {}
    for k in PRODUCT_ORDER:
        hist = sorted((x for x in summary if x['product'] == k and x['month'] < cur), key=lambda x: x['month'])
        now = {f: mean(v) for f, v in mtd.get(k, {}).items()}
        if not hist and not now:
            continue
        rng = {}
        flags = []
        for f, label, nd in REF_FIELDS:
            vals = [x[f] for x in hist if x.get(f) is not None]
            if not vals:
                continue
            lo, hi = min(vals), max(vals)
            rng[f] = (r2(lo, nd), r2(hi, nd), r2(mean(vals), nd))
            v = now.get(f)
            if v is None:
                continue
            if f in ('s2', 's7', 's28') and v < lo:
                flags.append(f)
                al.add('amber', 'Quality', f'{pname(k)}: {label} strength this month {r2(v, 1)} MPa is below the lowest previous monthly average {r2(lo, 1)}')
            elif f not in ('s2', 's7', 's28') and not lo <= v <= hi:
                flags.append(f)
                al.add('info', 'Quality', f'{pname(k)}: {label} this month {r2(v, nd)} is outside the previous months range {r2(lo, nd)}–{r2(hi, nd)}')
        months = [{'month': f"{x['month'][0]}-{x['month'][1]:02d}", **{f: r2(x.get(f), nd) for f, _, nd in REF_FIELDS}}
                  for x in hist]
        out[k] = {'name': pname(k), 'months': months, 'range': rng, 'flags': flags,
                  'mtd': {f: r2(now.get(f), nd) for f, _, nd in REF_FIELDS},
                  'mtd_n': {f: len(mtd.get(k, {}).get(f, [])) for f, _, _ in REF_FIELDS}}
    return {'products': out, 'fields': [[f, label] for f, label, _ in REF_FIELDS], 'month': f'{cur[0]}-{cur[1]:02d}'}


def quality_section(qc, day, al):
    if not qc:
        return None
    rows = [r for r in qc['hourly'] if r['date'].year == day.year and r['date'].month == day.month and r['date'] <= day]
    res = defaultdict(lambda: {'strength': [], 'setting': []})
    so3 = []
    for r in rows:
        k = r['product']
        if not k or k == 'transition':
            continue
        s = {f: r.get(f) for f in ('s2', 's7', 's28') if isinstance(r.get(f), (int, float))}
        if s:
            res[k]['strength'].append({'date': r['date'].isoformat(), **{f: r2(v, 1) for f, v in s.items()}})
        if isinstance(r.get('set_initial'), (int, float)):
            res[k]['setting'].append({'date': r['date'].isoformat(), 'initial': r['set_initial'],
                                      'final': r.get('set_final') if isinstance(r.get('set_final'), (int, float)) else None,
                                      'so3': r.get('so3_chem') if isinstance(r.get('so3_chem'), (int, float)) else r.get('so3_xrf')})
        a, b = r.get('so3_xrf'), r.get('so3_chem')
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            so3.append({'date': r['date'].isoformat(), 'hour': f"{r['hour']:02d}:00", 'product': pname(k),
                        'xrf': a, 'chem': b, 'diff': r2(a - b, 2)})
    out = {}
    for k, v in res.items():
        cls = EN197.get(PRODUCT_CLASS.get(k, ''), {})
        last = v['strength'][-1] if v['strength'] else None
        flags = []
        for s in v['strength']:
            for f in ('s2', 's7', 's28'):
                if f in s and f in cls and s[f] < cls[f]:
                    flags.append(f"{s['date']}: {f[1:]}-day strength {s[f]} below the minimum {cls[f]} MPa")
        for s in v['setting']:
            if 'set' in cls and s['initial'] < cls['set']:
                flags.append(f"{s['date']}: initial setting {s['initial']} min below {cls['set']} min")
        out[k] = {'name': pname(k), 'class': PRODUCT_CLASS.get(k), 'limits': cls, 'strength': v['strength'][-12:],
                  'setting': v['setting'][-12:], 'last': last, 'flags': flags}
        for f_ in flags[-3:]:
            al.add('amber', 'Quality', f'{pname(k)}: {f_}')
    bad = [s for s in so3 if abs(s['diff']) > 0.2]
    if bad:
        al.add('info', 'Quality', f'SO3 by XRF and by chemical analysis differ by more than 0.2 in {len(bad)} samples this month')
    return {'products': out, 'so3': so3[-30:], 'so3_bad': len(bad)}


# ── 13. sales ─────────────────────────────────────────────────────────────
def sales_section(sales, day):
    if sales is None:
        return None
    rows = [s for s in sales if s['date'] == day]
    agg = defaultdict(lambda: {'t': 0.0, 'n': 0})
    for s in rows:
        a = agg[s['product'] or s['type']]
        a['t'] += s['net_t']
        a['n'] += 1
    return {'by_product': [{'name': pname(k), 'tons': r2(v['t'], 1), 'trucks': v['n']} for k, v in agg.items()],
            'total_t': r2(sum(s['net_t'] for s in rows), 1), 'trucks': len(rows)}


# ── report ────────────────────────────────────────────────────────────────
def build_report(day, files, meta=None):
    """files: kind → parsed object (None if not uploaded). meta: kind → {filename, uploaded_at}."""
    al = Alarms()
    meta = meta or {}
    mp, proc, qc = files.get('mill_production'), files.get('process'), files.get('qc')
    rep = {'day': day.isoformat(), 'generated_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
           'files': {}, 'missing': []}
    for k, label in KIND_NAMES.items():
        present = files.get(k) is not None
        info = {'label': label, 'present': present, **(meta.get(k) or {})}
        if k == 'process' and proc is not None:
            from .parsers import process_day
            info['covers'] = process_day(proc) == day
        rep['files'][k] = info
        if not present and k not in ('qc_summary',):
            rep['missing'].append(label)
    if mp is not None:
        rep['files']['mill_production']['covers'] = day in mp['daily_tabs'] or day in mp['days_with_data']
    if qc is not None:
        rep['files']['qc']['covers'] = any(r['date'] == day for r in qc['hourly'])
    if mp is None or not rep['files']['mill_production'].get('covers'):
        al.add('red', 'Files', 'Mill production report for this day not uploaded — the report cannot be built')
        rep['alarms'] = al
        return rep
    prods = products_today(mp, day)
    qch = qc_day(qc, day)
    if qc is not None and not qch:
        al.add('amber', 'Files', 'The QC report has no hours for this day')
    if proc is not None and not rep['files']['process'].get('covers'):
        al.add('amber', 'Files', 'The uploaded process (DCS) file is not for this day')
        proc = None
    rep['products'] = prods
    tot = sum(p['prod'] or 0 for p in prods)
    hrs = sum(p['hours'] or 0 for p in prods)
    kwh = sum(p['total_kwh'] or 0 for p in prods)
    md = sum(p['md_kwh'] or 0 for p in prods)
    for p in prods:
        if p['target_tph'] and p['tph'] and p['tph'] < p['target_tph']:
            al.add('amber', 'Productivity', f"{p['name']}: productivity {p['tph']} t/h below target {p['target_tph']} ({p['tph_gap_pct']}%)")
    pi = {k: (v.get(day) if isinstance(v, dict) else None) for k, v in mp['pi'].items()}
    rep['kpis'] = {'production': r2(tot, 1), 'hours': r2(hrs, 2), 'tph': r2(tot / hrs, 2) if hrs else None,
                   'spc_plant': r2(kwh / tot, 2) if tot else None, 'spc_md': r2(md / tot, 2) if tot else None,
                   'aux_pct': r2((kwh - md) / kwh * 100, 1) if kwh else None,
                   'availability': r2((pi.get('availability') or 0) * 100, 1) if pi.get('availability') is not None else None,
                   'utilization': r2((pi.get('utilization') or 0) * 100, 1) if pi.get('utilization') is not None else None}
    rep['counters'] = counters_section(mp, day, al)
    rep['stoppages'] = stoppages_section(mp, day, prods, al)
    rep['flushing'] = flushing_section(mp, day, rep['counters'], al)
    rep['plan'] = plan_section(mp, day, files.get('plan'), al)
    rep['stock'] = stock_section(mp, day, files.get('acquisition'), al)
    lab = (files.get('clinker') or []) + (files.get('additives') or [])
    rep['moisture'] = moisture_section(mp, day, lab, al) if lab else None
    rep['qc_match'] = qc_match_section(mp, day, qch, al) if qch else None
    rep['chem'] = chem_section(mp, day, qch, qc, proc, prods, al) if qch else None
    rep['proportions'] = proportions_section(mp, day, qch, qc, proc, prods, al) if qch else None
    rep['process'] = process_section(proc, day, qch, qc, prods, files.get('sales'), files.get('fan'), al) if proc else None
    rep['quality'] = quality_section(qc, day, al)
    rep['reference'] = reference_section(qc, files.get('qc_summary'), day, al)
    rep['sales'] = sales_section(files.get('sales'), day)
    if files.get('sales') is not None and rep['sales'] and not rep['sales']['trucks']:
        al.add('info', 'Sales', 'The uploaded sales file has no trucks for this day')
    order = {'red': 0, 'amber': 1, 'info': 2}
    al.sort(key=lambda a: order.get(a['sev'], 3))
    rep['alarms'] = list(al)
    rep['kpis']['red'] = sum(1 for a in al if a['sev'] == 'red')
    rep['kpis']['amber'] = sum(1 for a in al if a['sev'] == 'amber')
    rep['kpis']['mtd'] = rep['plan']['total_mtd']
    rep['kpis']['mtd_pct'] = rep['plan']['total_pct']
    rep['kpis']['flush_loss_mtd'] = rep['flushing']['mtd_loss_jd']
    return rep
