"""Arabic daily dashboard (server-rendered tables + Chart.js panels) and the aiohttp app."""
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


SEV = {'red': ('حرج', '⛔'), 'amber': ('تنبيه', '⚠️'), 'info': ('معلومة', 'ℹ️')}


def table(headers, rows, cls=''):
    h = ''.join(f'<th>{E(x)}</th>' for x in headers)
    body = ''.join('<tr>' + ''.join(f'<td>{c}</td>' for c in r) + '</tr>' for r in rows)
    return f'<div class="tw"><table class="{cls}"><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>'


def badge(ok, yes='مطابق', no='غير مطابق'):
    return f'<span class="st {"good" if ok else "bad"}">{"✓ " + yes if ok else "✗ " + no}</span>'


def section(sid, title, inner, missing=None):
    if missing:
        inner = f'<p class="missing">⛔ {E(missing)}</p>'
    return f'<section id="{sid}"><h2>{E(title)}</h2>{inner}</section>'


def render_dashboard(rep, days, base='/'):
    d = rep['day']
    k = rep.get('kpis') or {}
    alarms = rep.get('alarms') or []
    nav = [('alarms', 'الإنذارات'), ('prod', 'الإنتاج'), ('counters', 'العدادات'), ('stops', 'التوقفات'),
           ('plan', 'الخطة'), ('flush', 'الغسيل'), ('stock', 'المخزون'), ('moist', 'الرطوبة'),
           ('props', 'النسب'), ('chem', 'الكيماويات'), ('qcm', 'مطابقة المختبر'), ('proc', 'التشغيل'),
           ('silos', 'الصوامع والمبيعات'), ('quality', 'الجودة')]
    prev_d = max((x for x in days if x < d), default=None)       # nearest available day either side
    next_d = min((x for x in days if x > d), default=None)
    shown = days if d in days else sorted(days + [d], reverse=True)
    opts = ''.join(f'<option value="{E(x)}"{" selected" if x == d else ""}>{E(x)}</option>' for x in shown)

    files = ''.join(
        f'<span class="chip {"ok" if f.get("present") and f.get("covers", True) else "no"}" title="{E(f.get("filename"))}">'
        f'{"✓" if f.get("present") and f.get("covers", True) else "✗"} {E(f["label"])}</span>'
        for kk, f in (rep.get('files') or {}).items() if kk != 'qc_summary' or f.get('present'))

    tiles = [
        ('الإنتاج', fmt(k.get('production')), 'طن'), ('ساعات التشغيل', fmt(k.get('hours'), 2), 'ساعة'),
        ('الإنتاجية', fmt(k.get('tph'), 2), 'طن/ساعة'), ('استهلاك المصنع', fmt(k.get('spc_plant'), 2), 'kWh/طن'),
        ('استهلاك محرك المطحنة', fmt(k.get('spc_md'), 2), 'kWh/طن'), ('حصة المعدات المساعدة', fmt(k.get('aux_pct')), '%'),
        ('الاستغلال', fmt(k.get('utilization')), '%'), ('المنجز من خطة الشهر', fmt(k.get('mtd_pct')), '%'),
        ('خسارة الغسيل هذا الشهر', fmt(k.get('flush_loss_mtd'), 0), 'دينار'),
    ]
    tiles_html = ''.join(f'<div class="tile"><div class="tl">{E(a)}</div><div class="tv">{b}</div><div class="tu">{E(c)}</div></div>'
                         for a, b, c in tiles)
    alarm_counts = (f'<span class="st bad">⛔ {k.get("red", 0)} حرج</span> '
                    f'<span class="st warn">⚠️ {k.get("amber", 0)} تنبيه</span>')

    parts = []
    # alarms
    al_rows = ''.join(f'<li class="al {a["sev"]}"><span class="st {"bad" if a["sev"] == "red" else "warn" if a["sev"] == "amber" else "info"}">'
                      f'{SEV[a["sev"]][1]} {SEV[a["sev"]][0]}</span> <b>{E(a["area"])}</b> — {E(a["text"])}</li>' for a in alarms)
    parts.append(section('alarms', 'الإنذارات والملاحظات', f'<ul class="alarms">{al_rows or "<li>لا يوجد</li>"}</ul>'))
    if not rep.get('products') and rep.get('products') is None:
        body = '\n'.join(parts)
        return PAGE.format(day=E(d), opts=opts, prev=_navlink(base, prev_d, '→ اليوم السابق'),
                           next=_navlink(base, next_d, 'اليوم التالي ←'), files=files, tiles=tiles_html,
                           counts=alarm_counts, nav='', body=body, gen=E(rep.get('generated_at')), data='{}')

    # production
    pr = rep['products']
    rows = [[E(p['name']), fmt(p['prod']), fmt(p['hours'], 2),
             f"{fmt(p['tph'], 2)}" + (f" <small class='{'neg' if (p['tph_gap_pct'] or 0) < 0 else 'pos'}'>({fmt(p['tph_gap_pct'])}%)</small>" if p['tph_gap_pct'] is not None else ''),
             fmt(p['target_tph'], 1), fmt(p['spc_md'], 2), fmt(p['spc_plant'], 2), fmt(p['aux_pct']),
             fmt(p['ck'], 3), fmt(p['clinker_pct']), fmt(p['ga_gpt'], 0), fmt(p['blaine'], 0), fmt(p['r45'], 2),
             fmt(p['whiteness'], 2), fmt(p['mtd'])] for p in pr]
    parts.append(section('prod', 'الإنتاج حسب المنتج', table(
        ['المنتج', 'الإنتاج طن', 'ساعات', 'الإنتاجية ط/س', 'الهدف ط/س', 'SPC المطحنة', 'SPC المصنع', 'المساعدات %',
         'C/K', 'كلنكر %', 'مساعد طحن g/t', 'Blaine', 'R45', 'البياض', 'تراكمي الشهر'], rows) if pr else '<p>لا إنتاج في هذا اليوم.</p>'))

    # counters + timeline
    c = rep.get('counters') or {}
    tl = c.get('timeline') or []
    tot = sum(x['hours'] or 0 for x in tl) or 1
    bar = ''.join(f'<div class="seg p-{E(x["key"])}" style="flex:{x["hours"] or 0}" title="{E(x["name"])}: {fmt(x["hours"], 2)} س">'
                  f'<span>{E(x["name"])}<br>{fmt(x["hours"], 2)} س</span></div>' for x in tl)
    chk = ''.join(f'<li>{badge(x["ok"], "سليم", "خطأ")} <b>{E(x["counter"])}</b>: {E(x["text"])}</li>' for x in c.get('checks') or [])
    parts.append(section('counters', 'تسلسل المنتجات والعدادات', (
        f'<p class="muted">ترتيب المنتجات مأخوذ من عدّاد ساعات التشغيل. المنتج السابق (آخر اليوم الماضي): '
        f'<b>{E(_pname(c.get("prev_last")))}</b></p><div class="timeline">{bar}</div><ul class="checks">{chk}</ul>')))

    # stoppages
    s = rep.get('stoppages') or {}
    st_rows = [[fmt(x['hours'], 2), E(x['dept']), E(x['reason'])] for x in s.get('stops') or []]
    rep_rows = [[E(x['reason']), E(x['dept']), fmt(x['count'], 0), fmt(x['hours'], 1), E('، '.join(map(str, x['days'])))]
                for x in s.get('repeated') or []]
    pi = s.get('pi') or {}
    parts.append(section('stops', 'التوقفات وقاعدة 24 ساعة', (
        f'<p>ساعات التشغيل <b>{fmt(s.get("run_h"), 2)}</b> + التوقفات المسجلة <b>{fmt(s.get("stop_h"), 2)}</b> = '
        f'<b>{fmt((s.get("run_h") or 0) + (s.get("stop_h") or 0), 2)}</b> ساعة. '
        f'تبويب PI: مخطط {fmt(pi.get("planned"), 2)} · خامل {fmt(pi.get("idle"), 2)} · أعطال {fmt(pi.get("incident"), 2)} · تشغيل {fmt(pi.get("net operation"), 2)}</p>'
        + (f'<p class="muted">{E(s.get("note"))}</p>' if s.get('note') else '')
        + (table(['المدة س', 'القسم', 'السبب'], st_rows) if st_rows else '<p>لا توقفات مسجلة اليوم.</p>')
        + f'<h3>الأسباب المتكررة هذا الشهر (مجموع التوقفات {fmt(s.get("month_total_h"), 1)} س)</h3>'
        + (table(['السبب', 'القسم', 'عدد المرات', 'الساعات', 'الأيام'], rep_rows) if rep_rows else '<p>لا يوجد سبب تكرر.</p>'))))

    # plan
    p = rep.get('plan') or {}
    p_rows = [[E(x['name']), fmt(x['mtd']), fmt(x['plan'], 0), fmt(x['pct']), fmt(x['remaining']), fmt(x['tph_used'], 2),
               fmt(x['days_needed'])] for x in p.get('rows') or []]
    spare = p.get('spare_days')
    verdict = ('<span class="st warn">⚠️ يجب تشغيل المطحنة أيام الجمع/العطل</span>' if spare is not None and spare < 0
               else f'<span class="st good">✓ يمكن إيقاف المطحنة حتى {fmt(spare)} يوم</span>') if p.get('has_plan') else '<span class="muted">الخطة السنوية غير مرفوعة</span>'
    parts.append(section('plan', 'المنجز مقابل الخطة السنوية', (
        f'<p>المنجز حتى اليوم <b>{fmt(p.get("total_mtd"))}</b> طن من خطة <b>{fmt(p.get("total_plan"), 0)}</b> طن '
        f'(<b>{fmt(p.get("total_pct"))}%</b>). الأيام المتبقية {fmt(p.get("days_left"), 0)} · العطل {fmt(p.get("holidays"), 0)} · '
        f'الأيام اللازمة {fmt(p.get("need_days"))} (باستغلال {fmt(p.get("utilization"))}%) → {verdict}</p>'
        + table(['المنتج', 'المنجز', 'الخطة', '%', 'المتبقي', 'ط/س المعتمدة', 'أيام لازمة'], p_rows))))

    # flushing
    f = rep.get('flushing') or {}
    f_rows = [[E(x['day']), fmt(x['tons']), E('، '.join(f'{a}: {fmt(b)}' for a, b in x['by'].items())), fmt(x['value_jd'], 0),
               fmt(x['loss_jd'], 0)] for x in f.get('events') or []]
    sw = '، '.join(f"{x['from']} ← {x['to']}" for x in f.get('switches') or []) or 'لا يوجد'
    parts.append(section('flush', 'خسائر غسيل المطحنة (رمادي ← أبيض)', (
        f'<p>تحويلات اليوم: <b>{E(sw)}</b>. هذا الشهر: <b>{fmt(f.get("mtd_tons"))}</b> طن كلنكر أبيض في الغسيل، '
        f'قيمتها {fmt(f.get("mtd_value_jd"), 0)} دينار، والخسارة مقابل الرمادي <b>{fmt(f.get("mtd_loss_jd"), 0)}</b> دينار '
        f'(أبيض {fmt((f.get("prices") or {}).get("white"), 0)} · رمادي {fmt((f.get("prices") or {}).get("grey"), 0)} دينار/طن).</p>'
        + (table(['اليوم', 'طن', 'حسب المصدر', 'القيمة دينار', 'الخسارة دينار'], f_rows) if f_rows else ''))))

    # stock
    sk = rep.get('stock') or {}
    s_rows = [[E(x['name']), f"<span class='{'neg' if (x['stock'] or 0) < 0 else ''}'>{fmt(x['stock'])}</span>", fmt(x['today_use']),
               fmt(x['avg_daily']), (f"<span class='st warn'>⚠️ {fmt(x['weeks'])}</span>" if x['weeks'] is not None and x['weeks'] < 2 else fmt(x['weeks']))]
              for x in sk.get('rows') or []]
    r_rows = [[E(x['name']), fmt(x['scale']), fmt(x['trucks'], 0), fmt(x['stock_tab']), fmt(x['diff']),
               badge(not x['bad']) if x.get('covered') else '<span class="muted">القبان لا يغطي اليوم</span>',
               fmt(x['mtd_scale'])] for x in sk.get('receipts') or []]
    rec = (table(['المادة', 'القبان طن', 'شاحنات', 'تبويب المخزون', 'الفرق', 'المطابقة', 'القبان تراكمي الشهر'], r_rows)
           if r_rows else '<p>لا استلام اليوم.</p>') if rep['files'].get('acquisition', {}).get('present') else '<p class="missing">ملف القبان غير مرفوع</p>'
    parts.append(section('stock', 'المخزون والاستلام', (
        table(['المادة', 'المخزون طن', 'استهلاك اليوم', 'متوسط آخر 7 أيام تشغيل', 'يكفي (أسابيع)'], s_rows)
        + f'<h3>الاستلام حسب القبان (المرجع) مقابل تبويب المخزون</h3>'
        + (f'<p class="muted">آخر تاريخ في ملف القبان: {E(sk.get("acq_last"))}</p>' if sk.get('acq_last') else '')
        + (f'<p class="muted">{E(sk.get("note_m"))}</p>' if sk.get('note_m') else '') + rec)))

    # moisture
    mo = rep.get('moisture')
    m_rows = [[E(x['name']), fmt(x['prod_pct'], 3), fmt(x['lab_pct'], 3), E(x['lab_date']), fmt(x.get('age_days'), 0),
               fmt(x.get('diff'), 2), badge(not x.get('bad')) if x.get('lab_pct') is not None else '—'] for x in (mo or {}).get('rows') or []]
    parts.append(section('moist', 'رطوبة المواد: تقرير الإنتاج مقابل المختبر', table(
        ['المادة', 'الإنتاج %', 'المختبر %', 'تاريخ العينة', 'عمر العينة (يوم)', 'الفرق', 'الحالة'], m_rows) if mo else '',
        missing=None if mo else 'تحاليل الكلنكر/المواد المضافة غير مرفوعة'))

    # proportions
    pp = rep.get('proportions')
    inner = ''
    for x in (pp or {}).get('products') or []:
        b = x.get('badge') or {}
        inner += (f'<h3>{E(x["name"])} <small class="muted">({fmt(x["qc_hours"], 0)} ساعة في المختبر · كلنكر المختبر: {E("، ".join(x["qc_clinkers"]) or "—")}'
                  + (f' · دفعة {E(b.get("serial"))}: كلنكر {fmt(b.get("clinker"))}% حجر {fmt(b.get("limestone"))}% بوزولانا {fmt(b.get("pozzolana"))}% جبس {fmt(b.get("gypsum"))}%' if b else '')
                  + ')</small></h3>')
        inner += table(['المادة', 'الإنتاج (رطب) %', 'المختبر %', 'الفرق', 'الحالة'],
                       [[E(r['name']), fmt(r['prod']), fmt(r['qc']), fmt(r['diff']),
                         badge(not r['bad']) if r['qc'] is not None and r['prod'] is not None else '—'] for r in x['rows']])
    if (pp or {}).get('wf'):
        inner += '<h3>موازين التغذية (DCS) — متوسط أثناء التشغيل</h3>' + table(
            ['الميزان', 'المادة', 'ط/س', '%'], [[E(w['wf']), E(w['material']), fmt(w['tph'], 2), fmt(w['pct'])] for w in pp['wf']])
    parts.append(section('props', 'نسب المواد (أساس رطب): الإنتاج مقابل تعليمات المختبر', inner,
                         missing=None if pp else 'تقرير المختبر غير مرفوع أو لا يحتوي هذا اليوم'))

    # chemicals
    ch = rep.get('chem')
    c_rows = [[E(x['name']), fmt(x.get('qc_ml_min')), fmt(x.get('badge_ml_min')), fmt(x.get('feed_tph'), 2), fmt(x.get('qc_gpt'), 0),
               fmt(x.get('prod_gpt'), 0), fmt(x.get('diff_pct'))] for x in (ch or {}).get('rows') or []]
    parts.append(section('chem', 'جرعة مساعد الطحن', (
        '<p class="muted">جرعة المختبر بالـ ml/دقيقة تُحوَّل إلى g/t بكثافة 1.06 g/ml وتغذية المطحنة الطازجة.</p>'
        + table(['المنتج', 'المختبر ml/min', 'هدف الدفعة ml/min', 'التغذية ط/س', 'المختبر g/t', 'الإنتاج g/t', 'الفرق %'], c_rows)
        + '<div class="chart"><canvas id="c_chem"></canvas></div>') if ch else '',
        missing=None if ch else 'تقرير المختبر غير مرفوع أو لا يحتوي هذا اليوم'))

    # QC match
    qm = rep.get('qc_match')
    q_rows = [[E(x['hour']), E(x['product']), E(x['field']), fmt(x['production'], 3), fmt(x['qc'], 3)] for x in (qm or {}).get('mismatches') or []]
    parts.append(section('qcm', 'مطابقة قيم المختبر في تقرير الإنتاج (بدون تسامح)', (
        f'<p>تمت مقارنة <b>{fmt(qm.get("compared"), 0)}</b> قيمة ساعية (Blaine · R45 · البياض L*). '
        + (f'غير المطابق: <b>{len(qm["mismatches"])}</b></p>' + table(['ساعة المختبر', 'المنتج', 'القيمة', 'تقرير الإنتاج', 'تقرير المختبر'], q_rows)
           if qm['mismatches'] else f'{badge(True, "كل القيم مطابقة")}</p>')) if qm else '',
        missing=None if qm else 'تقرير المختبر غير مرفوع أو لا يحتوي هذا اليوم'))

    # process
    pc = rep.get('process')
    if pc:
        fnd = ''.join(f'<li class="al {x["sev"]}">{SEV[x["sev"]][1]} {E(x["text"])}</li>' for x in pc['findings'])
        seq = ' ← '.join(f"{x['hour']} {x['product']} (صومعة {fmt(x['silo'], 0)})" for x in pc.get('product_sequence') or [])
        fan = pc.get('fan') or {}
        sep = pc.get('sep_fan') or {}
        seal = pc.get('air_seal') or {}
        fv = (pc.get('fill_vs_prod') or [])
        fills = ('، '.join(f"{x['product']}: {fmt(x['fill_tph'])} ط/س من ميل الصومعة مقابل {fmt(x['prod_tph'], 2)} ط/س في تقرير الإنتاج" for x in fv)
                 + f" ({len(pc.get('fill_rates') or [])} ساعة بدون تحميل)") if fv else 'لا توجد ساعة امتلاء بدون تحميل'
        inner = (f'<p class="muted">الفترة {E(pc.get("window"))} · {fmt(pc.get("samples"), 0)} قراءة</p><ul class="alarms">{fnd}</ul>'
                 '<h3>1. مستوى الصوامع (طن)</h3><div class="chart tall"><canvas id="c_silos"></canvas></div>'
                 f'<p class="note">تسلسل الإنتاج حسب المختبر: {E(seq) or "—"}.<br>معدل الامتلاء من ميل المنحنى (بدون تحميل): {E(fills)}.</p>'
                 '<h3>2. المطحنة: القدرة، التغذية والرجيع، الرافع</h3><div class="chart"><canvas id="c_md"></canvas></div>'
                 '<div class="chart"><canvas id="c_feed"></canvas></div><div class="chart sm"><canvas id="c_be"></canvas></div>'
                 + (f'<p class="note">تيار مروحة الفاصل: متوسط {fmt(sep.get("avg"))} A (من {fmt(sep.get("min"))} إلى {fmt(sep.get("max"))})، '
                    f'خارج ±1 A من المتوسط {fmt(sep.get("outside_pct"), 0)}% من الوقت، الارتباط مع الرجيع {fmt(sep.get("r_reject"), 2)}.</p>' if sep else '')
                 + ('<h3>انقطاعات التغذية</h3>' + table(['الميزان', 'المادة', 'من', 'إلى', 'دقائق'],
                    [[E(x['wf']), E(x['material']), E(x['from']), E(x['to']), fmt(x['minutes'], 0)] for x in pc['feed_cuts']]) if pc.get('feed_cuts') else '')
                 + '<h3>3. النعومة R45 مقابل سرعة الفاصل ومروحته</h3><div class="chart sm"><canvas id="c_r45"></canvas></div>'
                   '<div class="chart sm"><canvas id="c_sep"></canvas></div><div class="chart sm"><canvas id="c_sepfan"></canvas></div>'
                 + ('<h3>ساعات تجاوز R45 وإجراءات المشغل</h3>' + table(['الساعة', 'المنتج', 'R45', 'الهدف', 'الإجراء'],
                    [[E(x['hour']), E(x['product']), fmt(x['r45'], 2), '≤ ' + fmt(x['target'], 2), E('، '.join(x['actions']) or 'لا يوجد')] for x in pc['r45_actions']]) if pc.get('r45_actions') else '')
                 + '<h3>4. الحرارة: غاز المخرج، الإسمنت النهائي، غاز المدخل</h3><div class="chart"><canvas id="c_temp"></canvas></div>'
                 + f'<p class="note">السخان الكهربائي يعمل (حرارة المدخل فوق 100 °C) لمدة {fmt(pc.get("heater_minutes"), 0)} دقيقة.</p>'
                 + '<h3>5. تهوية المطحنة</h3><div class="chart sm"><canvas id="c_fanspd"></canvas></div>'
                   '<div class="chart sm"><canvas id="c_fankw"></canvas></div><div class="chart sm"><canvas id="c_press"></canvas></div>'
                   '<div class="chart sm"><canvas id="c_fill"></canvas></div>'
                 + (f'<p class="note">مروحة المطحنة: {fmt(fan.get("avg_rpm"), 0)} rpm و{fmt(fan.get("avg_kw"))} kW؛ القدرة المحسوبة من الجدول '
                    f'{fmt(fan.get("kw_calc"))} kW → أقل بـ {fmt(fan.get("avg_steps"))} خطوة؛ سرعة الهواء المتحققة ≈ {fmt(fan.get("velocity"), 3)} م/ث. '
                    f'متوسط فرق ضغط المطحنة {fmt(pc.get("mill_dp_avg"))} mbar.'
                    + (' السرعة فوق آخر سطر في جدول المروحة — القيمة المحسوبة بالاستقراء.' if fan.get('beyond_table') else '') + '</p>' if fan else '')
                 + '<h3>6. تهوية الفاصل: ضغط مروحة عزل الهواء وتيار مروحة الفاصل</h3><div class="chart sm"><canvas id="c_seal"></canvas></div>'
                   '<div class="chart sm"><canvas id="c_sepa"></canvas></div>'
                 + f'<p class="note">ضغط عزل الهواء: متوسط {fmt(seal.get("avg"), 2)} mbar، أدنى {fmt(seal.get("min"), 2)}، تحت 16 لمدة {fmt(seal.get("low_min"), 0)} دقيقة؛ '
                   f'الارتباط مع تيار مروحة الفاصل {fmt(seal.get("r_current"), 2)} (قريب من الصفر = الضغط لا يتبع التيار).</p>')
    else:
        inner = ''
    parts.append(section('proc', 'بيانات تشغيل المطحنة (DCS)', inner, missing=None if pc else 'ملف بيانات التشغيل لهذا اليوم غير مرفوع'))

    # silos & sales
    lo = (pc or {}).get('loading')
    sa = rep.get('sales')
    inner = ''
    if sa:
        inner += f'<p>المبيعات اليوم: <b>{fmt(sa["total_t"])}</b> طن في <b>{sa["trucks"]}</b> شاحنة.</p>' + table(
            ['المنتج', 'طن', 'شاحنات'], [[E(x['name']), fmt(x['tons']), fmt(x['trucks'], 0)] for x in sa['by_product']])
    if lo:
        inner += (f'<h3>تحميل M50 من الصوامع 1 و2 مقابل المبيعات</h3><p>مرات التحميل على ميزان التحميل: <b>{lo["events_count"]}</b> · '
                  f'شاحنات M50 في المبيعات: <b>{lo["m50_trucks"]}</b></p>'
                  + table(['الوقت', 'الصومعة', 'انخفاض المستوى طن', 'ميزان التحميل طن', 'الشاحنة', 'الصافي بالقبان طن'],
                          [[E(x['time']), fmt(x['silo'], 0), fmt(x['drop_t']), fmt(x['scale_t'], 2), E(x['truck']), fmt(x['net_t'], 2)] for x in lo['events']]))
        if lo.get('unmatched_trucks'):
            inner += '<p>شاحنات بدون تحميل مطابق: ' + E('، '.join(f"{x['plate']} ({x['time']})" for x in lo['unmatched_trucks'])) + '</p>'
    lv = (pc or {}).get('silo_level_check') or []
    if lv:
        bad = [x for x in lv if abs(x['diff']) > 10]
        inner += f'<p>مستوى الصومعة في تقرير المختبر مقابل DCS: {len(lv)} ساعة، الفرق فوق 10%: <b>{len(bad)}</b>.</p>'
    parts.append(section('silos', 'الصوامع والمبيعات', inner, missing=None if (sa or lo) else 'ملف المبيعات وبيانات التشغيل غير مرفوعين'))

    # quality
    qu = rep.get('quality')
    inner = ''
    for kk, x in ((qu or {}).get('products') or {}).items():
        lim = x.get('limits') or {}
        inner += (f'<h3>{E(x["name"])} <small class="muted">(الصنف {E(x.get("class"))} — الحدود الدنيا: '
                  f'يومان {fmt(lim.get("s2"))} · 7 أيام {fmt(lim.get("s7"))} · 28 يوم {fmt(lim.get("s28"))} MPa · الشك الابتدائي ≥ {fmt(lim.get("set"), 0)} د)</small></h3>')
        if x['strength']:
            inner += table(['التاريخ', '2 يوم', '7 أيام', '28 يوم'], [[E(s['date']), fmt(s.get('s2')), fmt(s.get('s7')), fmt(s.get('s28'))] for s in x['strength']])
        if x['setting']:
            inner += table(['التاريخ', 'الشك الابتدائي', 'النهائي', 'SO3'], [[E(s['date']), fmt(s['initial'], 0), fmt(s['final'], 0), fmt(s['so3'], 2)] for s in x['setting']])
        for fl in x['flags']:
            inner += f'<p class="st warn">⚠️ {E(fl)}</p>'
    if (qu or {}).get('so3'):
        inner += f'<h3>SO3 بالأشعة مقابل الكيميائي (الفرق فوق 0.2 في {qu["so3_bad"]} عينة)</h3>' + table(
            ['التاريخ', 'الساعة', 'المنتج', 'XRF', 'كيميائي', 'الفرق'],
            [[E(s['date']), E(s['hour']), E(s['product']), fmt(s['xrf'], 2), fmt(s['chem'], 2), fmt(s['diff'], 2)] for s in qu['so3'][-12:]])
    parts.append(section('quality', 'الجودة هذا الشهر: المقاومة، زمن الشك، SO3', inner or '<p>لا نتائج مقاومة مسجلة هذا الشهر.</p>',
                         missing=None if qu else 'تقرير المختبر غير مرفوع'))

    navh = ''.join(f'<a href="#{a}">{E(b)}</a>' for a, b in nav)
    data = {'chem': (ch or {}).get('hours'), 'charts': (pc or {}).get('charts'),
            'sequence': (pc or {}).get('product_sequence')}
    return PAGE.format(day=E(d), opts=opts, prev=_navlink(base, prev_d, '→ اليوم السابق'),
                       next=_navlink(base, next_d, 'اليوم التالي ←'), files=files, tiles=tiles_html, counts=alarm_counts,
                       nav=navh, body='\n'.join(parts), gen=E(rep.get('generated_at')),
                       data=json.dumps(data, ensure_ascii=False).replace('</', '<\\/'))


def _pname(k):
    from .names import PRODUCT_AR
    return PRODUCT_AR.get(k, k or '—')


def _navlink(base, d, label):
    return f'<a class="btn" href="{base}day/{E(d)}">{E(label)}</a>' if d else f'<span class="btn off">{E(label)}</span>'


PAGE = """<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>سيجما — التقرير اليومي {day}</title>
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
.btn.off{{opacity:.4}} select{{padding:4px 8px;border-radius:6px;border:1px solid var(--ring);background:var(--page);color:var(--ink)}}
.chip{{font-size:12px;padding:2px 8px;border-radius:12px;border:1px solid var(--ring)}} .chip.no{{color:var(--crit)}} .chip.ok{{color:var(--goodtxt)}}
nav{{position:sticky;top:0;z-index:5;background:var(--surface);border-bottom:1px solid var(--ring);padding:6px 16px;overflow-x:auto;white-space:nowrap}}
nav a{{color:var(--ink2);text-decoration:none;margin-left:14px;font-size:13px}} nav a:hover{{color:var(--accent)}}
main{{max-width:1280px;margin:0 auto;padding:12px 16px 40px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:12px 0}}
.tile{{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:10px 12px}}
.tl{{color:var(--ink2);font-size:12.5px}} .tv{{font-size:24px;font-weight:600}} .tu{{color:var(--muted);font-size:12px}}
section{{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:14px 16px;margin:12px 0}}
.tw{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}}
th{{text-align:right;color:var(--ink2);font-weight:600;border-bottom:1px solid var(--axis);padding:6px 8px;white-space:nowrap}}
td{{border-bottom:1px solid var(--grid);padding:5px 8px;white-space:nowrap}}
.st{{display:inline-block;font-size:12px;padding:1px 8px;border-radius:10px;border:1px solid currentColor}}
.st.good{{color:var(--goodtxt)}} .st.bad{{color:var(--crit)}} .st.warn{{color:#9a6a00}} .st.info{{color:var(--ink2)}}
:root[data-theme="dark"] .st.warn{{color:var(--warn)}} @media (prefers-color-scheme:dark){{.st.warn{{color:var(--warn)}}}}
ul.alarms{{list-style:none;padding:0;margin:0}} ul.alarms li{{padding:6px 0;border-bottom:1px solid var(--grid)}}
li.al.red{{border-right:3px solid var(--crit);padding-right:8px}} li.al.amber{{border-right:3px solid var(--warn);padding-right:8px}}
li.al.info{{border-right:3px solid var(--grid);padding-right:8px;color:var(--ink2)}}
ul.checks{{list-style:none;padding:0}} ul.checks li{{padding:3px 0}}
.timeline{{display:flex;gap:2px;height:46px;margin:8px 0}} .seg{{border-radius:4px;color:#fff;font-size:11.5px;display:flex;align-items:center;justify-content:center;overflow:hidden;text-align:center;line-height:1.25}}
.p-m50,.p-m10,.p-low_alkali,.p-vpa{{background:#6b6a66}} .p-power{{background:var(--s1)}} .p-super{{background:var(--s3)}}
.p-eco{{background:var(--s2)}} .p-ultra{{background:var(--s7)}} .p-flushing{{background:var(--s8)}}
.chart{{position:relative;height:240px;margin:6px 0}} .chart.tall{{height:300px}} .chart.sm{{height:170px}}
.muted{{color:var(--ink2)}} .note{{color:var(--ink2);font-size:13px}} .missing{{color:var(--crit)}}
.neg{{color:var(--crit)}} .pos{{color:var(--goodtxt)}} small{{font-weight:400}}
footer{{color:var(--muted);text-align:center;font-size:12px;padding:16px}}
</style></head><body>
<header><h1>سيجما للإسمنت — التقرير اليومي للمطحنة</h1>
<div class="bar"><b>يوم الإنتاج {day}</b> <span class="muted">(08:00 ← 08:00 اليوم التالي)</span>
{prev}<select aria-label="اختر اليوم" onchange="location.href='/day/'+this.value">{opts}</select>{next} {counts}</div>
<div class="bar" style="margin-top:8px">{files}</div></header>
<nav>{nav}</nav>
<main><div class="tiles">{tiles}</div>{body}</main>
<footer>آخر تحديث {gen} · يُحدَّث تلقائيًا كل صباح وعند رفع أي ملف متأخر</footer>
<script src="/static/chart-4.4.1.umd.js"></script>
<script>
const DATA={data};
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function panel(id,labels,sets,yTitle,opts){{
  const el=document.getElementById(id); if(!el||!labels) return;
  Chart.defaults.font.family='system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif';
  Chart.defaults.color=css('--ink2');
  new Chart(el,{{type:'line',data:{{labels,datasets:sets.map(s=>({{label:s.label,data:s.data,borderColor:css(s.color),backgroundColor:css(s.color),
    borderWidth:2,pointRadius:0,pointHitRadius:8,tension:.15,spanGaps:true,borderDash:s.dash||[]}}))}},
   options:{{maintainAspectRatio:false,animation:false,interaction:{{mode:'index',intersect:false}},
    plugins:{{legend:{{display:sets.length>1,position:'top',labels:{{boxWidth:12,boxHeight:2}}}},
      title:{{display:true,text:yTitle,align:'start',color:css('--ink'),font:{{size:12,weight:'600'}}}},
      tooltip:{{rtl:true}}}},
    scales:{{x:{{reverse:false,grid:{{display:false}},ticks:{{maxTicksLimit:13,color:css('--muted')}},border:{{color:css('--axis')}}}},
      y:{{position:'right',grid:{{color:css('--grid')}},ticks:{{color:css('--muted'),maxTicksLimit:5}},border:{{display:false}},...(opts||{{}})}}}}}}}});
}}
const C=DATA.charts||{{}};
const g=(c,k)=>c&&c.series?c.series[k]:null;
if(C.silos){{panel('c_silos',C.silos.t,[1,2,3,4,5].map(n=>({{label:'صومعة '+n,data:g(C.silos,'s'+n),color:'--s'+n}})),'مستوى الصوامع — طن');}}
if(C.mill){{panel('c_md',C.mill.t,[{{label:'قدرة محرك المطحنة',data:g(C.mill,'md'),color:'--s1'}}],'قدرة محرك المطحنة — kW');
  panel('c_feed',C.mill.t,[{{label:'التغذية الطازجة',data:g(C.mill,'feed'),color:'--s1'}},{{label:'الرجيع',data:g(C.mill,'reject'),color:'--s2'}}],'التغذية والرجيع — طن/ساعة');
  panel('c_be',C.mill.t,[{{label:'رافع المطحنة',data:g(C.mill,'be'),color:'--s1'}}],'تيار رافع المطحنة — A (الطبيعي 10–11.5، فوق 13 امتلاء)');}}
if(C.r45){{panel('c_r45',C.r45.t,[{{label:'R45 (DCS)',data:g(C.r45,'r45_dcs'),color:'--s1'}}],'R45 — %');
  panel('c_sep',C.r45.t,[{{label:'سرعة الفاصل',data:g(C.r45,'sep_speed'),color:'--s1'}}],'سرعة الفاصل — rpm');
  panel('c_sepfan',C.r45.t,[{{label:'سرعة مروحة الفاصل',data:g(C.r45,'sep_fan_speed'),color:'--s1'}}],'سرعة مروحة الفاصل — rpm');}}
if(C.temps){{panel('c_temp',C.temps.t,[{{label:'غاز المخرج',data:g(C.temps,'gas_out'),color:'--s1'}},{{label:'الإسمنت النهائي',data:g(C.temps,'cement'),color:'--s2'}},
  {{label:'غاز المدخل',data:g(C.temps,'gas_in'),color:'--s3'}}],'الحرارة — °C (هدف غاز المخرج 90–100)');}}
if(C.vent){{panel('c_fanspd',C.vent.t,[{{label:'سرعة مروحة المطحنة',data:g(C.vent,'fan_speed'),color:'--s1'}}],'سرعة مروحة المطحنة — rpm');
  panel('c_fankw',C.vent.t,[{{label:'قدرة مروحة المطحنة',data:g(C.vent,'fan_kw'),color:'--s1'}}],'قدرة مروحة المطحنة — kW');
  panel('c_press',C.vent.t,[{{label:'ضغط المخرج',data:g(C.vent,'p_out'),color:'--s1'}},{{label:'فرق ضغط المطحنة',data:g(C.vent,'mill_dp'),color:'--s2'}},
    {{label:'فرق ضغط الفلتر',data:g(C.vent,'filter_dp'),color:'--s3'}}],'الضغوط — mbar');
  panel('c_fill',C.vent.t,[{{label:'امتلاء المطحنة',data:g(C.vent,'filling'),color:'--s1'}}],'امتلاء المطحنة');}}
if(C.seal){{panel('c_seal',C.seal.t,[{{label:'ضغط عزل الهواء',data:g(C.seal,'air_seal'),color:'--s1'}}],'ضغط مروحة عزل الهواء — mbar (إنذار تحت 16)');
  panel('c_sepa',C.seal.t,[{{label:'تيار مروحة الفاصل',data:g(C.seal,'sep_fan_a'),color:'--s1'}}],'تيار مروحة الفاصل — A (120–130 ±1)');}}
if(DATA.chem){{const h=DATA.chem;panel('c_chem',h.map(x=>x.hour),[{{label:'R45',data:h.map(x=>x.r45),color:'--s1'}}],'R45 الساعي من المختبر — %');}}
</script></body></html>"""


NO_DAY = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>سيجما — {day}</title><style>body{{font:15px/1.7 system-ui,"Segoe UI",Tahoma,sans-serif;background:#f9f9f7;color:#0b0b0b;padding:16px}}
@media (prefers-color-scheme:dark){{body{{background:#0d0d0d;color:#f0efec}}a{{color:#3987e5}}}}</style></head><body>
<h2>لا توجد ملفات مرفوعة ليوم {day}</h2><p>الأيام المرفوعة:</p><ul>{links}</ul></body></html>"""


LOGIN = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>سيجما — دخول</title><style>body{{font:15px system-ui,"Segoe UI",Tahoma,sans-serif;background:#f9f9f7;color:#0b0b0b;display:flex;
min-height:100vh;align-items:center;justify-content:center;margin:0}}@media (prefers-color-scheme:dark){{body{{background:#0d0d0d;color:#f0efec}}}}
form{{padding:24px;border:1px solid rgba(128,128,128,.3);border-radius:10px;min-width:280px}}input,button{{width:100%;padding:8px;margin-top:10px;font:inherit}}
.e{{color:#d03b3b}}</style></head><body><form method="post" action="/login"><h2>التقرير اليومي — سيجما</h2>
<label>كلمة السر<input type="password" name="p" autofocus></label>{err}<button>دخول</button></form></body></html>"""


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
            return web.Response(text='<p dir="rtl" style="font:16px system-ui;padding:24px">افتح الداشبورد من الرابط الذي يرسله البوت في تلجرام (أو أرسل /daily).</p>',
                                content_type='text/html')
        return web.Response(text=LOGIN.format(err=''), content_type='text/html')

    async def login_post(req):
        form = await req.post()
        if password and hmac.compare_digest(form.get('p', ''), password):
            raise grant(req, '/')
        return web.Response(text=LOGIN.format(err='<p class="e">كلمة السر غير صحيحة</p>'), content_type='text/html')

    async def index(req):
        if not authed(req):
            raise web.HTTPFound('/login')
        import asyncio
        from .service import available_days
        days = await asyncio.to_thread(available_days, store)
        pinned = await asyncio.to_thread(store.pinned_day)
        if pinned:
            raise web.HTTPFound(f'/day/{pinned.isoformat()}')
        if not days:
            return web.Response(text='<p dir="rtl">لا توجد تقارير بعد — ارفع ملفات اليوم على تلجرام.</p>', content_type='text/html')
        raise web.HTTPFound(f'/day/{days[0]}')

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
            links = ''.join(f'<li><a href="/day/{E(x)}">{E(x)}</a></li>' for x in days[:31])
            return web.Response(text=NO_DAY.format(day=E(d), links=links or '<li>لا توجد أيام مرفوعة بعد</li>'),
                                content_type='text/html', status=404)
        rep = await asyncio.to_thread(report_for_view, store, d)
        page = await asyncio.to_thread(render_dashboard, rep, days)
        return web.Response(text=page, content_type='text/html', headers={'Cache-Control': 'no-store'})

    async def health(req):
        return web.Response(text='ok')

    app = web.Application(middlewares=[link_login])
    app.router.add_static('/static/', os.path.join(os.path.dirname(__file__), 'static'))
    app.add_routes([web.get('/', index), web.get('/login', login_get), web.post('/login', login_post),
                    web.get('/day/{d}', day_page), web.get('/healthz', health)])
    return app


async def _days(store):
    import asyncio
    return [x.isoformat() for x in await asyncio.to_thread(store.report_days)]
