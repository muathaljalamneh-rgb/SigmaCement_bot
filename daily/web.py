"""English daily dashboard (server-rendered tables + Chart.js panels) and the aiohttp app."""
import hmac
import os
import hashlib
import html
import json
import datetime as dt

from aiohttp import web

E = lambda x: html.escape('' if x is None else str(x))


def fmt(v, nd=1):
    if v is None or v == '':
        return '—'
    if isinstance(v, (int, float)):
        if abs(v) >= 1000:
            return f'{v:,.0f}'
        return f'{v:,.{nd}f}'.rstrip('0').rstrip('.') if nd else f'{v:,.0f}'
    return E(v)


SEV = {'red': ('Critical', '⛔'), 'amber': ('Warning', '⚠️'), 'info': ('Info', 'ℹ️')}


def table(headers, rows, cls=''):
    h = ''.join(f'<th>{E(x)}</th>' for x in headers)
    body = ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>' for r in rows)
    return f'<div class="tw"><table class="{cls}"><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>'


def badge(ok, yes='OK', no='Mismatch'):
    return f'<span class="st {"good" if ok else "bad"}">{"✓ " + yes if ok else "✗ " + no}</span>'


def section(sid, title, inner, missing=None):
    if missing:
        inner = f'<p class="missing">⛔ {E(missing)}</p>'
    return f'<section id="{sid}"><h2>{E(title)}</h2>{inner}</section>'


QC_MISSING = 'QC report not uploaded or has no data for this day'


def render_dashboard(rep, days, base='/'):
    d = rep['day']
    k = rep.get('kpis') or {}
    alarms = rep.get('alarms') or []
    nav = [('alarms', 'Alarms'), ('prod', 'Production'), ('counters', 'Counters'), ('stops', 'Stoppages'),
           ('plan', 'Plan'), ('flush', 'Flushing'), ('stock', 'Stock'), ('moist', 'Moisture'),
           ('props', 'Proportions'), ('chem', 'Chemicals'), ('qcm', 'QC match'), ('proc', 'Process'),
           ('silos', 'Silos & sales'), ('quality', 'Quality')]
    prev_d = max((x for x in days if x < d), default=None)       # nearest available day either side
    next_d = min((x for x in days if x > d), default=None)
    shown = days if d in days else sorted(days + [d], reverse=True)
    opts = ''.join(f'<option value="{E(x)}"{" selected" if x == d else ""}>{E(x)}</option>' for x in shown)

    files = ''.join(
        f'<span class="chip {"ok" if f.get("present") and f.get("covers", True) else "no"}" title="{E(f.get("filename"))}">'
        f'{"✓" if f.get("present") and f.get("covers", True) else "✗"} {E(f["label"])}</span>'
        for kk, f in (rep.get('files') or {}).items() if kk != 'qc_summary' or f.get('present'))

    tiles = [
        ('Production', fmt(k.get('production')), 't'), ('Running hours', fmt(k.get('hours'), 2), 'h'),
        ('Productivity', fmt(k.get('tph'), 2), 't/h'), ('Plant SPC', fmt(k.get('spc_plant'), 2), 'kWh/t'),
        ('Mill drive SPC', fmt(k.get('spc_md'), 2), 'kWh/t'), ('Auxiliaries share', fmt(k.get('aux_pct')), '%'),
        ('Utilization', fmt(k.get('utilization')), '%'), ('Month-to-date vs plan', fmt(k.get('mtd_pct')), '%'),
        ('Flushing loss this month', fmt(k.get('flush_loss_mtd'), 0), 'JD'),
    ]
    tiles_html = ''.join(f'<div class="tile"><div class="tl">{E(a)}</div><div class="tv">{b}</div><div class="tu">{E(c)}</div></div>'
                         for a, b, c in tiles)
    alarm_counts = (f'<span class="st bad">⛔ {k.get("red", 0)} critical</span> '
                    f'<span class="st warn">⚠️ {k.get("amber", 0)} warnings</span>')

    parts = []
    # alarms
    al_rows = ''.join(f'<li class="al {a["sev"]}"><span class="st {"bad" if a["sev"] == "red" else "warn" if a["sev"] == "amber" else "info"}">'
                      f'{SEV[a["sev"]][1]} {SEV[a["sev"]][0]}</span> <b>{E(a["area"])}</b> — {E(a["text"])}</li>' for a in alarms)
    parts.append(section('alarms', 'Alarms and notes', f'<ul class="alarms">{al_rows or "<li>None</li>"}</ul>'))
    if not rep.get('products') and rep.get('products') is None:
        body = '\n'.join(parts)
        return PAGE.format(day=E(d), opts=opts, prev=_navlink(base, prev_d, '← Previous day'),
                           next=_navlink(base, next_d, 'Next day →'), files=files, tiles=tiles_html,
                           counts=alarm_counts, nav='', body=body, gen=E(rep.get('generated_at')), data='{}')

    # production
    pr = rep['products']
    rows = [[E(p['name']), fmt(p['prod']), fmt(p['hours'], 2),
             f"{fmt(p['tph'], 2)}" + (f" <small class='{'neg' if (p['tph_gap_pct'] or 0) < 0 else 'pos'}'>({fmt(p['tph_gap_pct'])}%)</small>" if p['tph_gap_pct'] is not None else ''),
             fmt(p['target_tph'], 1), fmt(p['spc_md'], 2), fmt(p['spc_plant'], 2), fmt(p['aux_pct']),
             fmt(p['ck'], 3), fmt(p['clinker_pct']), fmt(p['ga_gpt'], 0), fmt(p['blaine'], 0), fmt(p['r45'], 2),
             fmt(p['whiteness'], 2), fmt(p['mtd'])] for p in pr]
    parts.append(section('prod', 'Production by product', table(
        ['Product', 'Production t', 'Hours', 'Productivity t/h', 'Target t/h', 'Mill SPC', 'Plant SPC', 'Auxiliaries %',
         'C/K', 'Clinker %', 'Grinding aid g/t', 'Blaine', 'R45', 'Whiteness', 'Month to date'], rows) if pr else '<p>No production on this day.</p>'))

    # counters + timeline
    c = rep.get('counters') or {}
    tl = c.get('timeline') or []
    bar = ''.join(f'<div class="seg p-{E(x["key"])}" style="flex:{x["hours"] or 0}" title="{E(x["name"])}: {fmt(x["hours"], 2)} h">'
                  f'<span>{E(x["name"])}<br>{fmt(x["hours"], 2)} h</span></div>' for x in tl)
    chk = ''.join(f'<li>{badge(x["ok"], "OK", "Error")} <b>{E(x["counter"])}</b>: {E(x["text"])}</li>' for x in c.get('checks') or [])
    parts.append(section('counters', 'Product sequence and counters', (
        f'<p class="muted">Product order is taken from the running-hours counter. Previous product (end of the previous day): '
        f'<b>{E(_pname(c.get("prev_last")))}</b></p><div class="timeline">{bar}</div><ul class="checks">{chk}</ul>')))

    # stoppages
    s = rep.get('stoppages') or {}
    st_rows = [[fmt(x['hours'], 2), E(x['dept']), E(x['reason'])] for x in s.get('stops') or []]
    rep_rows = [[E(x['reason']), E(x['dept']), fmt(x['count'], 0), fmt(x['hours'], 1), E(', '.join(map(str, x['days'])))]
                for x in s.get('repeated') or []]
    pi = s.get('pi') or {}
    parts.append(section('stops', 'Stoppages and the 24-hour rule', (
        f'<p>Running hours <b>{fmt(s.get("run_h"), 2)}</b> + recorded stoppages <b>{fmt(s.get("stop_h"), 2)}</b> = '
        f'<b>{fmt((s.get("run_h") or 0) + (s.get("stop_h") or 0), 2)}</b> h. '
        f'PI tab: planned {fmt(pi.get("planned"), 2)} · idle {fmt(pi.get("idle"), 2)} · incident {fmt(pi.get("incident"), 2)} · net operation {fmt(pi.get("net operation"), 2)}</p>'
        + (f'<p class="muted">{E(s.get("note"))}</p>' if s.get('note') else '')
        + (table(['Duration h', 'Department', 'Reason'], st_rows) if st_rows else '<p>No stoppages recorded today.</p>')
        + f'<h3>Repeated reasons this month (total stoppages {fmt(s.get("month_total_h"), 1)} h)</h3>'
        + (table(['Reason', 'Department', 'Times', 'Hours', 'Days'], rep_rows) if rep_rows else '<p>No repeated reason.</p>'))))

    # plan
    p = rep.get('plan') or {}
    p_rows = [[E(x['name']), fmt(x['mtd']), fmt(x['plan'], 0), fmt(x['pct']), fmt(x['remaining']), fmt(x['tph_used'], 2),
               fmt(x['days_needed'])] for x in p.get('rows') or []]
    spare = p.get('spare_days')
    verdict = ('<span class="st warn">⚠️ The mill must run on Fridays/holidays</span>' if spare is not None and spare < 0
               else f'<span class="st good">✓ The mill can stop up to {fmt(spare)} days</span>') if p.get('has_plan') else '<span class="muted">Annual plan not uploaded</span>'
    parts.append(section('plan', 'Month to date vs annual plan', (
        f'<p>Produced so far <b>{fmt(p.get("total_mtd"))}</b> t of a <b>{fmt(p.get("total_plan"), 0)}</b> t plan '
        f'(<b>{fmt(p.get("total_pct"))}%</b>). Days left {fmt(p.get("days_left"), 0)} · holidays {fmt(p.get("holidays"), 0)} · '
        f'days needed {fmt(p.get("need_days"))} (at {fmt(p.get("utilization"))}% utilization) → {verdict}</p>'
        + table(['Product', 'Month to date', 'Plan', '%', 'Remaining', 't/h used', 'Days needed'], p_rows))))

    # flushing
    f = rep.get('flushing') or {}
    f_rows = [[E(x['day']), fmt(x['tons']), E(', '.join(f'{a}: {fmt(b)}' for a, b in x['by'].items())), fmt(x['value_jd'], 0),
               fmt(x['loss_jd'], 0)] for x in f.get('events') or []]
    sw = ', '.join(f"{x['from']} → {x['to']}" for x in f.get('switches') or []) or 'none'
    parts.append(section('flush', 'Mill flushing losses (grey → white)', (
        f'<p>Switches today: <b>{E(sw)}</b>. This month: <b>{fmt(f.get("mtd_tons"))}</b> t of white clinker used in flushing, '
        f'worth {fmt(f.get("mtd_value_jd"), 0)} JD; loss against grey clinker <b>{fmt(f.get("mtd_loss_jd"), 0)}</b> JD '
        f'(white {fmt((f.get("prices") or {}).get("white"), 0)} · grey {fmt((f.get("prices") or {}).get("grey"), 0)} JD/t).</p>'
        + (table(['Day', 't', 'By source', 'Value JD', 'Loss JD'], f_rows) if f_rows else ''))))

    # stock
    sk = rep.get('stock') or {}
    s_rows = [[E(x['name']), f"<span class='{'neg' if (x['stock'] or 0) < 0 else ''}'>{fmt(x['stock'])}</span>", fmt(x['today_use']),
               fmt(x['avg_daily']), (f"<span class='st warn'>⚠️ {fmt(x['weeks'])}</span>" if x['weeks'] is not None and x['weeks'] < 2 else fmt(x['weeks']))]
              for x in sk.get('rows') or []]
    r_rows = [[E(x['name']), fmt(x['scale']), fmt(x['trucks'], 0), fmt(x['stock_tab']), fmt(x['diff']),
               badge(not x['bad']) if x.get('covered') else '<span class="muted">truck scale does not cover this day</span>',
               fmt(x['mtd_scale'])] for x in sk.get('receipts') or []]
    rec = (table(['Material', 'Truck scale t', 'Trucks', 'Stock tab t', 'Difference', 'Check', 'Truck scale month to date'], r_rows)
           if r_rows else '<p>No receipts today.</p>') if rep['files'].get('acquisition', {}).get('present') else '<p class="missing">Truck-scale file not uploaded</p>'
    parts.append(section('stock', 'Stock and receipts', (
        table(['Material', 'Stock t', 'Used today', 'Average of last 7 running days', 'Enough for (weeks)'], s_rows)
        + '<h3>Receipts: truck scale (reference) vs stock tab</h3>'
        + (f'<p class="muted">Last date in the truck-scale file: {E(sk.get("acq_last"))}</p>' if sk.get('acq_last') else '')
        + (f'<p class="muted">{E(sk.get("note_m"))}</p>' if sk.get('note_m') else '') + rec)))

    # moisture
    mo = rep.get('moisture')
    m_rows = [[E(x['name']), fmt(x['prod_pct'], 3), fmt(x['lab_pct'], 3), E(x['lab_date']), fmt(x.get('age_days'), 0),
               fmt(x.get('diff'), 2), badge(not x.get('bad')) if x.get('lab_pct') is not None else '—'] for x in (mo or {}).get('rows') or []]
    parts.append(section('moist', 'Material moisture: production report vs lab', table(
        ['Material', 'Production %', 'Lab %', 'Sample date', 'Sample age (days)', 'Difference', 'Status'], m_rows) if mo else '',
        missing=None if mo else 'Clinker / additive lab files not uploaded'))

    # proportions
    pp = rep.get('proportions')
    inner = ''
    for x in (pp or {}).get('products') or []:
        b = x.get('badge') or {}
        inner += (f'<h3>{E(x["name"])} <small class="muted">({fmt(x["qc_hours"], 0)} QC hours · QC clinker: {E(", ".join(x["qc_clinkers"]) or "—")}'
                  + (f' · batch {E(b.get("serial"))}: clinker {fmt(b.get("clinker"))}% limestone {fmt(b.get("limestone"))}% pozzolana {fmt(b.get("pozzolana"))}% gypsum {fmt(b.get("gypsum"))}%' if b else '')
                  + ')</small></h3>')
        inner += table(['Material', 'Production (wet) %', 'QC %', 'Difference', 'Status'],
                       [[E(r['name']), fmt(r['prod']), fmt(r['qc']), fmt(r['diff']),
                         badge(not r['bad']) if r['qc'] is not None and r['prod'] is not None else '—'] for r in x['rows']])
    if (pp or {}).get('wf'):
        inner += '<h3>Weigh feeders (DCS) — average while running</h3>' + table(
            ['Feeder', 'Material', 't/h', '%'], [[E(w['wf']), E(w['material']), fmt(w['tph'], 2), fmt(w['pct'])] for w in pp['wf']])
    parts.append(section('props', 'Material proportions (wet basis): production vs QC instructions', inner,
                         missing=None if pp else QC_MISSING))

    # chemicals
    ch = rep.get('chem')
    c_rows = [[E(x['name']), fmt(x.get('qc_ml_min')), fmt(x.get('badge_ml_min')), fmt(x.get('feed_tph'), 2), fmt(x.get('qc_gpt'), 0),
               fmt(x.get('prod_gpt'), 0), fmt(x.get('diff_pct'))] for x in (ch or {}).get('rows') or []]
    parts.append(section('chem', 'Grinding-aid dosage', (
        '<p class="muted">The QC dosage in ml/min is converted to g/t with a density of 1.06 g/ml and the mill fresh feed.</p>'
        + table(['Product', 'QC ml/min', 'Batch target ml/min', 'Feed t/h', 'QC g/t', 'Production g/t', 'Difference %'], c_rows)
        + '<div class="chart"><canvas id="c_chem"></canvas></div>') if ch else '',
        missing=None if ch else QC_MISSING))

    # QC match
    qm = rep.get('qc_match')
    q_rows = [[E(x['hour']), E(x['product']), E(x['field']), fmt(x['production'], 3), fmt(x['qc'], 3)] for x in (qm or {}).get('mismatches') or []]
    parts.append(section('qcm', 'QC values in the production report (no tolerance)', (
        f'<p><b>{fmt(qm.get("compared"), 0)}</b> hourly values compared (Blaine · R45 · whiteness L*). '
        + (f'Mismatches: <b>{len(qm["mismatches"])}</b></p>' + table(['QC hour', 'Product', 'Value', 'Production report', 'QC report'], q_rows)
           if qm['mismatches'] else f'{badge(True, "All values match")}</p>')) if qm else '',
        missing=None if qm else QC_MISSING))

    # process
    pc = rep.get('process')
    if pc:
        fnd = ''.join(f'<li class="al {x["sev"]}">{SEV[x["sev"]][1]} {E(x["text"])}</li>' for x in pc['findings'])
        seq = ' → '.join(f"{x['hour']} {x['product']} (silo {fmt(x['silo'], 0)})" for x in pc.get('product_sequence') or [])
        fan = pc.get('fan') or {}
        sep = pc.get('sep_fan') or {}
        seal = pc.get('air_seal') or {}
        fv = (pc.get('fill_vs_prod') or [])
        fills = (', '.join(f"{x['product']}: {fmt(x['fill_tph'])} t/h from the silo slope vs {fmt(x['prod_tph'], 2)} t/h in the production report" for x in fv)
                 + f" ({len(pc.get('fill_rates') or [])} hours without loading)") if fv else 'no filling hour without loading'
        inner = (f'<p class="muted">Window {E(pc.get("window"))} · {fmt(pc.get("samples"), 0)} readings</p><ul class="alarms">{fnd}</ul>'
                 '<h3>1. Silo levels (t)</h3><div class="chart tall"><canvas id="c_silos"></canvas></div>'
                 f'<p class="note">Production sequence from QC: {E(seq) or "—"}.<br>Filling rate from the curve slope (no loading): {E(fills)}.</p>'
                 '<h3>2. Mill: drive power, fresh feed and reject, bucket elevator</h3><div class="chart"><canvas id="c_md"></canvas></div>'
                 '<div class="chart"><canvas id="c_feed"></canvas></div><div class="chart sm"><canvas id="c_be"></canvas></div>'
                 + (f'<p class="note">Separator fan current: average {fmt(sep.get("avg"))} A (from {fmt(sep.get("min"))} to {fmt(sep.get("max"))}), '
                    f'outside ±1 A of the average {fmt(sep.get("outside_pct"), 0)}% of the time, correlation with reject {fmt(sep.get("r_reject"), 2)}.</p>' if sep else '')
                 + ('<h3>Feed cut-offs</h3>' + table(['Feeder', 'Material', 'From', 'To', 'Minutes'],
                    [[E(x['wf']), E(x['material']), E(x['from']), E(x['to']), fmt(x['minutes'], 0)] for x in pc['feed_cuts']]) if pc.get('feed_cuts') else '')
                 + '<h3>3. Fineness R45 vs separator speed and separator fan</h3><div class="chart sm"><canvas id="c_r45"></canvas></div>'
                   '<div class="chart sm"><canvas id="c_sep"></canvas></div><div class="chart sm"><canvas id="c_sepfan"></canvas></div>'
                 + ('<h3>Hours above the R45 target and operator actions</h3>' + table(['Hour', 'Product', 'R45', 'Target', 'Action'],
                    [[E(x['hour']), E(x['product']), fmt(x['r45'], 2), '≤ ' + fmt(x['target'], 2), E(', '.join(x['actions']) or 'none')] for x in pc['r45_actions']]) if pc.get('r45_actions') else '')
                 + '<h3>4. Temperatures: outlet gas, final cement, inlet gas</h3><div class="chart"><canvas id="c_temp"></canvas></div>'
                 + f'<p class="note">Electric heater running (inlet gas above 100 °C) for {fmt(pc.get("heater_minutes"), 0)} min.</p>'
                 + '<h3>5. Mill ventilation</h3><div class="chart sm"><canvas id="c_fanspd"></canvas></div>'
                   '<div class="chart sm"><canvas id="c_fankw"></canvas></div><div class="chart sm"><canvas id="c_press"></canvas></div>'
                   '<div class="chart sm"><canvas id="c_fill"></canvas></div>'
                 + (f'<p class="note">Mill fan: {fmt(fan.get("avg_rpm"), 0)} rpm and {fmt(fan.get("avg_kw"))} kW; calculated power from the table '
                    f'{fmt(fan.get("kw_calc"))} kW → {fmt(fan.get("avg_steps"))} steps lower; achieved air velocity ≈ {fmt(fan.get("velocity"), 3)} m/s. '
                    f'Average mill dP {fmt(pc.get("mill_dp_avg"))} mbar.'
                    + (' Fan speed is above the last row of the fan table — the calculated value is extrapolated.' if fan.get('beyond_table') else '') + '</p>' if fan else '')
                 + '<h3>6. Separator ventilation: air-seal pressure and separator fan current</h3><div class="chart sm"><canvas id="c_seal"></canvas></div>'
                   '<div class="chart sm"><canvas id="c_sepa"></canvas></div>'
                 + f'<p class="note">Air-seal pressure: average {fmt(seal.get("avg"), 2)} mbar, minimum {fmt(seal.get("min"), 2)}, below 16 for {fmt(seal.get("low_min"), 0)} min; '
                   f'correlation with separator fan current {fmt(seal.get("r_current"), 2)} (near zero = the pressure does not follow the current).</p>')
    else:
        inner = ''
    parts.append(section('proc', 'Mill operation process (DCS)', inner, missing=None if pc else 'Process (DCS) file for this day not uploaded'))

    # silos & sales
    lo = (pc or {}).get('loading')
    sa = rep.get('sales')
    inner = ''
    if sa:
        inner += f'<p>Sales today: <b>{fmt(sa["total_t"])}</b> t in <b>{sa["trucks"]}</b> trucks.</p>' + table(
            ['Product', 't', 'Trucks'], [[E(x['name']), fmt(x['tons']), fmt(x['trucks'], 0)] for x in sa['by_product']])
    if lo:
        inner += (f'<h3>M50 loading from silos 1 and 2 vs sales</h3><p>Loadings on the loading scale: <b>{lo["events_count"]}</b> · '
                  f'M50 trucks in sales: <b>{lo["m50_trucks"]}</b></p>'
                  + table(['Time', 'Silo', 'Level drop t', 'Loading scale t', 'Truck', 'Weighbridge net t'],
                          [[E(x['time']), fmt(x['silo'], 0), fmt(x['drop_t']), fmt(x['scale_t'], 2), E(x['truck']), fmt(x['net_t'], 2)] for x in lo['events']]))
        if lo.get('unmatched_trucks'):
            inner += '<p>Trucks without a matching loading: ' + E(', '.join(f"{x['plate']} ({x['time']})" for x in lo['unmatched_trucks'])) + '</p>'
    lv = (pc or {}).get('silo_level_check') or []
    if lv:
        bad = [x for x in lv if abs(x['diff']) > 10]
        inner += f'<p>Silo level in the QC report vs DCS: {len(lv)} hours, difference above 10%: <b>{len(bad)}</b>.</p>'
    parts.append(section('silos', 'Silos and sales', inner, missing=None if (sa or lo) else 'Sales and process (DCS) files not uploaded'))

    # quality
    qu = rep.get('quality')
    inner = ''
    for kk, x in ((qu or {}).get('products') or {}).items():
        lim = x.get('limits') or {}
        inner += (f'<h3>{E(x["name"])} <small class="muted">(class {E(x.get("class"))} — minimums: '
                  f'2D {fmt(lim.get("s2"))} · 7D {fmt(lim.get("s7"))} · 28D {fmt(lim.get("s28"))} MPa · initial setting ≥ {fmt(lim.get("set"), 0)} min)</small></h3>')
        if x['strength']:
            inner += table(['Date', '2D', '7D', '28D'], [[E(s['date']), fmt(s.get('s2')), fmt(s.get('s7')), fmt(s.get('s28'))] for s in x['strength']])
        if x['setting']:
            inner += table(['Date', 'Initial setting', 'Final setting', 'SO3'], [[E(s['date']), fmt(s['initial'], 0), fmt(s['final'], 0), fmt(s['so3'], 2)] for s in x['setting']])
        for fl in x['flags']:
            inner += f'<p class="st warn">⚠️ {E(fl)}</p>'
    if (qu or {}).get('so3'):
        inner += f'<h3>SO3 by XRF vs chemical analysis (difference above 0.2 in {qu["so3_bad"]} samples)</h3>' + table(
            ['Date', 'Hour', 'Product', 'XRF', 'Chemical', 'Difference'],
            [[E(s['date']), E(s['hour']), E(s['product']), fmt(s['xrf'], 2), fmt(s['chem'], 2), fmt(s['diff'], 2)] for s in qu['so3'][-12:]])
    parts.append(section('quality', 'Quality this month: strength, setting time, SO3', inner or '<p>No strength results recorded this month.</p>',
                         missing=None if qu else 'QC report not uploaded'))

    # QC summary reference (monthly averages) vs this month so far
    rf = rep.get('reference')
    inner = ''
    ref_charts = {}
    if rf:
        fields = rf['fields']
        for kk, x in rf['products'].items():
            rows = [[E(m['month'])] + [fmt(m.get(f), 2) for f, _ in fields] for m in x['months']]
            cells = []
            for f, _ in fields:
                v = x['mtd'].get(f)
                cls = 'neg' if f in x['flags'] else ''
                cells.append(f"<b class='{cls}'>{fmt(v, 2)}</b>" + (f" <small class='muted'>({x['mtd_n'][f]})</small>" if v is not None else ''))
            rows.append([f"<b>{E(rf['month'])} to date</b>"] + cells)
            if x['range']:
                rows.append(['<span class="muted">Previous months range</span>'] + [
                    f"<span class='muted'>{fmt(x['range'][f][0], 2)} – {fmt(x['range'][f][1], 2)}</span>" if f in x['range'] else '—'
                    for f, _ in fields])
            inner += f'<h3>{E(x["name"])}</h3>' + table(['Month'] + [lbl for _, lbl in fields], rows)
            if any(m.get('s28') or m.get('s2') for m in x['months']):
                cid = f'c_ref_{kk}'
                inner += f'<div class="chart sm"><canvas id="{cid}"></canvas></div>'
                ref_charts[cid] = {'t': [m['month'] for m in x['months']] + [rf['month'] + ' to date'],
                                   's2': [m.get('s2') for m in x['months']] + [x['mtd'].get('s2')],
                                   's7': [m.get('s7') for m in x['months']] + [x['mtd'].get('s7')],
                                   's28': [m.get('s28') for m in x['months']] + [x['mtd'].get('s28')]}
        inner = ('<p class="muted">Previous monthly averages from the QC summary file, against this month to date from the hourly QC report '
                 '(number of readings in brackets). Values in red are outside the previous months range.</p>' + inner)
    parts.append(section('ref', 'Comparison with the yearly QC summary', inner,
                         missing=None if rf else 'QC summary file not uploaded'))
    nav.append(('ref', 'QC summary'))

    navh = ''.join(f'<a href="#{a}">{E(b)}</a>' for a, b in nav)
    data = {'chem': (ch or {}).get('hours'), 'charts': (pc or {}).get('charts'), 'ref': ref_charts,
            'sequence': (pc or {}).get('product_sequence')}
    return PAGE.format(day=E(d), opts=opts, prev=_navlink(base, prev_d, '← Previous day'),
                       next=_navlink(base, next_d, 'Next day →'), files=files, tiles=tiles_html, counts=alarm_counts,
                       nav=navh, body='\n'.join(parts), gen=E(rep.get('generated_at')),
                       data=json.dumps(data, ensure_ascii=False).replace('</', '<\\/'))


def render_no_files(d, days):
    """Same page and date picker, with a 'no files for this day' message."""
    prev_d = max((x for x in days if x < d), default=None)
    next_d = min((x for x in days if x > d), default=None)
    opts = '<option value="" selected>— days with files —</option>' + ''.join(
        f'<option value="{E(x)}">{E(x)}</option>' for x in days)
    body = (f'<section><h2>No files for this day</h2><p>No file has been uploaded for production day <b>{E(d)}</b>. '
            'Pick another date from the calendar or from the list of days with files.</p>'
            + (f'<p class="muted">Nearest earlier day with files: {_navlink("/", prev_d, prev_d)}</p>' if prev_d else '')
            + (f'<p class="muted">Nearest later day with files: {_navlink("/", next_d, next_d)}</p>' if next_d else '')
            + '</section>')
    return PAGE.format(day=E(d), opts=opts, prev=_navlink('/', prev_d, '← Previous day'),
                       next=_navlink('/', next_d, 'Next day →'), files='', tiles='', counts='', nav='',
                       body=body, gen='—', data='{}')


def _pname(k):
    from .names import PRODUCT_NAMES
    return PRODUCT_NAMES.get(k, k or '—')


def _navlink(base, d, label):
    return f'<a class="btn" href="{base}day/{E(d)}">{E(label)}</a>' if d else f'<span class="btn off">{E(label)}</span>'


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sigma — Daily report {day}</title>
<style>
:root{{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;
--axis:#c3c2b7;--ring:rgba(11,11,11,.10);--accent:#2a78d6;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--s8:#e34948;
--good:#0ca30c;--goodtxt:#006300;--warn:#fab219;--serious:#ec835a;--crit:#d03b3b;}}
@media (prefers-color-scheme:dark){{:root:where(:not([data-theme="light"])){{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--ring:rgba(255,255,255,.10);--accent:#3987e5;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767;--goodtxt:#0ca30c;}}}}
:root[data-theme="dark"]{{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;
--ring:rgba(255,255,255,.10);--accent:#3987e5;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767;--goodtxt:#0ca30c;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--page);color:var(--ink);font:14px/1.6 system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif}}
header{{background:var(--surface);border-bottom:1px solid var(--ring);padding:14px 16px}}
h1{{margin:0 0 6px;font-size:20px}} h2{{font-size:17px;margin:0 0 10px;padding-bottom:6px;border-bottom:1px solid var(--grid)}}
h3{{font-size:14.5px;margin:16px 0 6px}}
.bar{{display:flex;flex-wrap:wrap;gap:8px;align-items:center}}
.btn{{padding:4px 10px;border:1px solid var(--ring);border-radius:6px;color:var(--ink);text-decoration:none;background:var(--page)}}
.btn.off{{opacity:.4}} input[type=date],select{{padding:4px 8px;border-radius:6px;border:1px solid var(--ring);background:var(--page);color:var(--ink)}}
.chip{{font-size:12px;padding:2px 8px;border-radius:12px;border:1px solid var(--ring)}} .chip.no{{color:var(--crit)}} .chip.ok{{color:var(--goodtxt)}}
nav{{position:sticky;top:0;z-index:5;background:var(--surface);border-bottom:1px solid var(--ring);padding:6px 16px;overflow-x:auto;white-space:nowrap}}
nav a{{color:var(--ink2);text-decoration:none;margin-right:14px;font-size:13px}} nav a:hover{{color:var(--accent)}}
main{{max-width:1280px;margin:0 auto;padding:12px 16px 40px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:12px 0}}
.tile{{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:10px 12px}}
.tl{{color:var(--ink2);font-size:12.5px}} .tv{{font-size:24px;font-weight:600}} .tu{{color:var(--muted);font-size:12px}}
section{{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:14px 16px;margin:12px 0}}
.tw{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}}
th{{text-align:left;color:var(--ink2);font-weight:600;border-bottom:1px solid var(--axis);padding:6px 8px;white-space:nowrap}}
td{{border-bottom:1px solid var(--grid);padding:5px 8px;white-space:nowrap}}
.st{{display:inline-block;font-size:12px;padding:1px 8px;border-radius:10px;border:1px solid currentColor}}
.st.good{{color:var(--goodtxt)}} .st.bad{{color:var(--crit)}} .st.warn{{color:#9a6a00}} .st.info{{color:var(--ink2)}}
:root[data-theme="dark"] .st.warn{{color:var(--warn)}} @media (prefers-color-scheme:dark){{.st.warn{{color:var(--warn)}}}}
ul.alarms{{list-style:none;padding:0;margin:0}} ul.alarms li{{padding:6px 0;border-bottom:1px solid var(--grid)}}
li.al.red{{border-left:3px solid var(--crit);padding-left:8px}} li.al.amber{{border-left:3px solid var(--warn);padding-left:8px}}
li.al.info{{border-left:3px solid var(--grid);padding-left:8px;color:var(--ink2)}}
ul.checks{{list-style:none;padding:0}} ul.checks li{{padding:3px 0}}
.timeline{{display:flex;gap:2px;height:46px;margin:8px 0}} .seg{{border-radius:4px;color:#fff;font-size:11.5px;display:flex;align-items:center;justify-content:center;overflow:hidden;text-align:center;line-height:1.25}}
.p-m50,.p-m10,.p-low_alkali,.p-vpa{{background:#6b6a66}} .p-power{{background:var(--s1)}} .p-super{{background:var(--s3)}}
.p-eco{{background:var(--s2)}} .p-ultra{{background:var(--s7)}} .p-flushing{{background:var(--s8)}}
.chart{{position:relative;height:240px;margin:6px 0}} .chart.tall{{height:300px}} .chart.sm{{height:170px}}
.muted{{color:var(--ink2)}} .note{{color:var(--ink2);font-size:13px}} .missing{{color:var(--crit)}}
.neg{{color:var(--crit)}} .pos{{color:var(--goodtxt)}} small{{font-weight:400}}
footer{{color:var(--muted);text-align:center;font-size:12px;padding:16px}}
</style></head><body>
<header><h1>Sigma Cement — Daily mill report <a class="btn" style="font-size:13px;font-weight:400" href="/upload">⬆️ Upload files</a></h1>
<div class="bar"><b>Production day {day}</b> <span class="muted">(08:00 → 08:00 next day)</span>
<label>📅 Date: <input type="date" value="{day}" onchange="if(this.value)location.href='/day/'+this.value"></label>
{prev}<select aria-label="Days with files" onchange="location.href='/day/'+this.value">{opts}</select>{next} {counts}</div>
<div class="bar" style="margin-top:8px">{files}</div></header>
<nav>{nav}</nav>
<main><div class="tiles">{tiles}</div>{body}</main>
<footer>Last updated {gen} · refreshed every morning and whenever a late file is uploaded</footer>
<script src="/static/chart-4.4.1.umd.js"></script>
<script>
const DATA={data};
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function panel(id,labels,sets,yTitle,opts){{
  const el=document.getElementById(id); if(!el||!labels) return;
  Chart.defaults.font.family='system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif';
  Chart.defaults.color=css('--ink2');
  new Chart(el,{{type:'line',data:{{labels,datasets:sets.map(s=>({{label:s.label,data:s.data,borderColor:css(s.color),backgroundColor:css(s.color),
    borderWidth:2,pointRadius:labels.length<=15?4:0,pointHitRadius:8,tension:.15,spanGaps:true,borderDash:s.dash||[]}}))}},
   options:{{maintainAspectRatio:false,animation:false,interaction:{{mode:'index',intersect:false}},
    plugins:{{legend:{{display:sets.length>1,position:'top',labels:{{boxWidth:12,boxHeight:2}}}},
      title:{{display:true,text:yTitle,align:'start',color:css('--ink'),font:{{size:12,weight:'600'}}}}}},
    scales:{{x:{{grid:{{display:false}},ticks:{{maxTicksLimit:13,color:css('--muted')}},border:{{color:css('--axis')}}}},
      y:{{position:'left',grid:{{color:css('--grid')}},ticks:{{color:css('--muted'),maxTicksLimit:5}},border:{{display:false}},...(opts||{{}})}}}}}}}});
}}
const C=DATA.charts||{{}};
const g=(c,k)=>c&&c.series?c.series[k]:null;
if(C.silos){{panel('c_silos',C.silos.t,[1,2,3,4,5].map(n=>({{label:'Silo '+n,data:g(C.silos,'s'+n),color:'--s'+n}})),'Silo levels — t');}}
if(C.mill){{panel('c_md',C.mill.t,[{{label:'Mill drive power',data:g(C.mill,'md'),color:'--s1'}}],'Mill drive power — kW');
  panel('c_feed',C.mill.t,[{{label:'Fresh feed',data:g(C.mill,'feed'),color:'--s1'}},{{label:'Reject',data:g(C.mill,'reject'),color:'--s2'}}],'Fresh feed and reject — t/h');
  panel('c_be',C.mill.t,[{{label:'Mill bucket elevator',data:g(C.mill,'be'),color:'--s1'}}],'Mill bucket elevator current — A (normal 10–11.5, above 13 = overfilled)');}}
if(C.r45){{panel('c_r45',C.r45.t,[{{label:'R45 (DCS)',data:g(C.r45,'r45_dcs'),color:'--s1'}}],'R45 — %');
  panel('c_sep',C.r45.t,[{{label:'Separator speed',data:g(C.r45,'sep_speed'),color:'--s1'}}],'Separator speed — rpm');
  panel('c_sepfan',C.r45.t,[{{label:'Separator fan speed',data:g(C.r45,'sep_fan_speed'),color:'--s1'}}],'Separator fan speed — rpm');}}
if(C.temps){{panel('c_temp',C.temps.t,[{{label:'Outlet gas',data:g(C.temps,'gas_out'),color:'--s1'}},{{label:'Final cement',data:g(C.temps,'cement'),color:'--s2'}},
  {{label:'Inlet gas',data:g(C.temps,'gas_in'),color:'--s3'}}],'Temperatures — °C (outlet gas target 90–100)');}}
if(C.vent){{panel('c_fanspd',C.vent.t,[{{label:'Mill fan speed',data:g(C.vent,'fan_speed'),color:'--s1'}}],'Mill fan speed — rpm');
  panel('c_fankw',C.vent.t,[{{label:'Mill fan power',data:g(C.vent,'fan_kw'),color:'--s1'}}],'Mill fan power — kW');
  panel('c_press',C.vent.t,[{{label:'Outlet pressure',data:g(C.vent,'p_out'),color:'--s1'}},{{label:'Mill dP',data:g(C.vent,'mill_dp'),color:'--s2'}},
    {{label:'Filter dP',data:g(C.vent,'filter_dp'),color:'--s3'}}],'Pressures — mbar');
  panel('c_fill',C.vent.t,[{{label:'Mill filling',data:g(C.vent,'filling'),color:'--s1'}}],'Mill filling');}}
if(C.seal){{panel('c_seal',C.seal.t,[{{label:'Air-seal pressure',data:g(C.seal,'air_seal'),color:'--s1'}}],'Separator air-seal pressure — mbar (alarm below 16)');
  panel('c_sepa',C.seal.t,[{{label:'Separator fan current',data:g(C.seal,'sep_fan_a'),color:'--s1'}}],'Separator fan current — A (120–130 ±1)');}}
for(const [id,c] of Object.entries(DATA.ref||{{}})){{panel(id,c.t,[{{label:'2D',data:c.s2,color:'--s1'}},{{label:'7D',data:c.s7,color:'--s2'}},
  {{label:'28D',data:c.s28,color:'--s3'}}],'Strength by month — MPa');}}
if(DATA.chem){{const h=DATA.chem;panel('c_chem',h.map(x=>x.hour),[{{label:'R45',data:h.map(x=>x.r45),color:'--s1'}}],'Hourly R45 from QC — %');}}
</script></body></html>"""


def _file_table(idx):
    from .parsers import KIND_NAMES
    rows = ''.join(f'<tr><td>{E(KIND_NAMES.get(r["kind"], r["kind"]))}</td><td>{E(r["slot"] if r["slot"] != "*" else "latest copy")}</td>'
                   f'<td>{E(r["filename"])}</td><td>{E(r["uploaded_at"].strftime("%Y-%m-%d %H:%M") if r["uploaded_at"] else "")}</td></tr>'
                   for r in idx)
    return (f'<table><thead><tr><th>Type</th><th>Day / month</th><th>File</th><th>Uploaded</th></tr></thead><tbody>{rows}</tbody></table>'
            if rows else '<p>No files yet.</p>')


UPLOAD = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sigma — Upload files</title><style>
body{{margin:0;font:14px/1.7 system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;background:#f9f9f7;color:#0b0b0b}}
@media (prefers-color-scheme:dark){{body{{background:#0d0d0d;color:#f0efec}} section{{background:#1a1a19!important}} .btn{{color:#f0efec!important}}}}
main{{max-width:900px;margin:0 auto;padding:16px}} section{{background:#fcfcfb;border:1px solid rgba(128,128,128,.25);border-radius:10px;padding:14px 16px;margin:12px 0}}
h1{{font-size:20px}} h2{{font-size:16px;margin:0 0 8px}} .btn{{display:inline-block;padding:6px 12px;border:1px solid rgba(128,128,128,.35);border-radius:6px;color:#0b0b0b;text-decoration:none;margin:4px 0}}
button{{font:inherit;padding:8px 18px;border-radius:6px;border:0;background:#2a78d6;color:#fff;margin-top:10px}} input[type=file]{{font:inherit;max-width:100%}}
.bad{{color:#d03b3b}} table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{text-align:left;padding:5px 6px;border-bottom:1px solid rgba(128,128,128,.25)}}
.tw{{overflow-x:auto}} .muted{{opacity:.75}} #wait{{display:none}}
</style></head><body><main>
<h1>⬆️ Upload daily report files <a class="btn" style="font-size:13px" href="/">Dashboard →</a></h1>
<section><form method="post" action="/upload" enctype="multipart/form-data" onsubmit="document.getElementById('wait').style.display='block'">
<p>Choose one or more Excel files. Each file is recognised from its content; file names do not matter:</p>
<p class="muted">Mill production · Mill operation process (DCS) · Mill product QC · Raw material acquisition · Clinker material · Additive materials · Daily sales · Annual plan · Mill fan table · QC summary</p>
<input type="file" name="files" multiple accept=".xlsx,.xlsm"><br><button>Upload and update the report</button>
<p id="wait">⏳ Reading the files and building the report… this can take a minute.</p></form></section>
{result}
<section><h2>Stored files</h2><div class="tw">{files}</div></section>
</main></body></html>"""


LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sigma — Sign in</title><style>body{{font:15px system-ui,"Segoe UI",Tahoma,sans-serif;background:#f9f9f7;color:#0b0b0b;display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0}}@media (prefers-color-scheme:dark){{body{{background:#0d0d0d;color:#f0efec}}}}
form{{padding:24px;border:1px solid rgba(128,128,128,.3);border-radius:10px;min-width:280px}}input,button{{width:100%;padding:8px;margin-top:10px;font:inherit}}
.e{{color:#d03b3b}}</style></head><body><form method="post" action="/login"><h2>Sigma — Daily report</h2>
<label>Password<input type="password" name="p" autofocus></label>{err}<button>Sign in</button></form></body></html>"""


def link_key(secret):
    """Key embedded in the links the bot sends; opening one signs the browser in."""
    return hmac.new(secret.encode(), b'sigma-dashboard-link', hashlib.sha256).hexdigest()[:32]


def make_app(store, password, secret):
    """Dashboard app. Access: the bot's private link (?k=...) or, if set, the password."""
    key = link_key(secret)
    token = hmac.new(secret.encode(), (password or key).encode(), hashlib.sha256).hexdigest()

    def authed(req):
        return hmac.compare_digest(req.cookies.get('sg', ''), token)

    def grant(req, to):
        r = web.HTTPFound(to)
        r.set_cookie('sg', token, max_age=60 * 60 * 24 * 365, httponly=True, samesite='Lax',
                     secure=req.headers.get('X-Forwarded-Proto', req.scheme) == 'https')
        return r

    @web.middleware
    async def link_login(req, handler):
        k = req.query.get('k')
        if k and hmac.compare_digest(k, key):
            raise grant(req, req.path)
        return await handler(req)

    async def login_get(req):
        if not password:
            return web.Response(text='<p style="font:16px system-ui;padding:24px">Open the dashboard from the link the Telegram bot sends (or send /daily).</p>',
                                content_type='text/html')
        return web.Response(text=LOGIN.format(err=''), content_type='text/html')

    async def login_post(req):
        form = await req.post()
        if password and hmac.compare_digest(form.get('p', ''), password):
            raise grant(req, '/')
        return web.Response(text=LOGIN.format(err='<p class="e">Wrong password</p>'), content_type='text/html')

    async def index(req):
        if not authed(req):
            raise web.HTTPFound('/login')
        import asyncio
        from .service import available_days
        days = await asyncio.to_thread(available_days, store)
        pinned = await asyncio.to_thread(store.pinned_day)
        if pinned and pinned.isoformat() in days:
            raise web.HTTPFound(f'/day/{pinned.isoformat()}')
        from .service import report_day
        raise web.HTTPFound(f'/day/{days[0] if days else report_day().isoformat()}')

    async def day_page(req):
        if not authed(req):
            raise web.HTTPFound('/login')
        try:
            d = dt.date.fromisoformat(req.match_info['d'])
        except ValueError:
            raise web.HTTPNotFound()
        import asyncio
        from .service import report_for_view, available_days
        days = await asyncio.to_thread(available_days, store)
        if d.isoformat() not in days:
            return web.Response(text=render_no_files(d.isoformat(), days), content_type='text/html',
                                headers={'Cache-Control': 'no-store'})
        rep = await asyncio.to_thread(report_for_view, store, d)
        page = await asyncio.to_thread(render_dashboard, rep, days)
        return web.Response(text=page, content_type='text/html', headers={'Cache-Control': 'no-store'})

    async def upload_get(req):
        if not authed(req):
            raise web.HTTPFound('/login')
        import asyncio
        idx = await asyncio.to_thread(store.file_index)
        return web.Response(text=UPLOAD.format(result='', files=_file_table(idx)), content_type='text/html',
                            headers={'Cache-Control': 'no-store'})

    async def upload_post(req):
        if not authed(req):
            raise web.HTTPFound('/login')
        import asyncio
        from . import service as SV
        from .parsers import KIND_NAMES
        reader = await req.multipart()
        pinned = await asyncio.to_thread(store.pinned_day)
        rows, days = [], set()
        while True:
            part = await reader.next()
            if part is None:
                break
            if not part.filename:
                continue
            data = await part.read(decode=False)
            name = part.filename
            if not name.lower().endswith(('.xlsx', '.xlsm')):
                rows.append(f'<li class="bad">✗ {E(name)}: not an Excel file (.xlsx)</li>')
                continue
            try:
                kind, day, _ = await asyncio.to_thread(SV.ingest, store, bytes(data), name, None, None, pinned)
            except Exception as e:
                rows.append(f'<li class="bad">✗ {E(name)}: {E(e)}</li>')
                continue
            if kind in ('mill_production', 'process', 'sales'):
                days.add(day)
            rows.append(f'<li>✓ {E(name)} → <b>{E(KIND_NAMES.get(kind, kind))}</b>'
                        + (f' · day {E(day)}' if kind in ('mill_production', 'process', 'sales') else '') + '</li>')
        if pinned:
            days.add(pinned)
        built = []
        for d in sorted(days):
            rep = await asyncio.to_thread(SV.rebuild, store, d)
            if rep.get('products') is not None:
                built.append(f'<a class="btn" href="/day/{d}">📊 Report {d}</a>')
        result = (f'<section><h2>Upload result</h2><ul>{"".join(rows) or "<li>No files selected</li>"}</ul>'
                  f'<div class="bar">{" ".join(built)}</div></section>')
        idx = await asyncio.to_thread(store.file_index)
        return web.Response(text=UPLOAD.format(result=result, files=_file_table(idx)), content_type='text/html',
                            headers={'Cache-Control': 'no-store'})

    async def health(req):
        return web.Response(text='ok')

    app = web.Application(middlewares=[link_login], client_max_size=80 * 1024 * 1024)
    app.router.add_static('/static/', os.path.join(os.path.dirname(__file__), 'static'))
    app.add_routes([web.get('/', index), web.get('/login', login_get), web.post('/login', login_post),
                    web.get('/day/{d}', day_page), web.get('/upload', upload_get), web.post('/upload', upload_post),
                    web.get('/healthz', health)])
    return app


async def _days(store):
    import asyncio
    return [x.isoformat() for x in await asyncio.to_thread(store.report_days)]
