"""
insight_engine.py — AI analytical layer for the Sigma Cement monthly report.
============================================================================
Division of labour (non-negotiable):
  * PYTHON computes every NUMBER (report_engine).  Zero AI arithmetic.
  * CLAUDE only WRITES the reasoning: cross-linking, root-cause narrative,
    retrospective explanations, and prioritized recommendations.

The evidence pack below is assembled from ALL stored months, so the model can
spot multi-month drifts (fineness erosion, recipe creep, stock reclassification,
weekday stoppage patterns) that a single-month view cannot reveal.

Fails safe: if the API errors or returns junk, the caller falls back to the
deterministic rule-based insights already in report_engine.
"""
import json, logging, calendar
from datetime import datetime

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

SYSTEM = """You are the lead process engineer and controller of a white/grey cement grinding plant \
(Om Al Rassas / Sigma Cement, Jordan). You write the analytical section of the monthly management report \
that goes to the plant owner.

YOUR ROLE: you receive a PRE-COMPUTED evidence pack. Every number in it was calculated deterministically \
from the plant's Excel workbooks. You do NOT recalculate anything. You reason ACROSS the numbers.

WHAT MAKES A GOOD INSIGHT (this is the whole point of the report):
- It LINKS at least two different data domains: stoppages <-> silo levels <-> stock movements <-> \
recipe ratios <-> power/SPC <-> packing/dispatch <-> quality (Blaine/whiteness).
- It EXPLAINS a mechanism ("X happened BECAUSE Y"), not just restating a number.
- It uses MULTI-MONTH history to show a trend, a reversal, or to retrospectively explain an older anomaly.
- It quantifies the consequence in tons, hours, or JD wherever the evidence pack supports it.
- It is ACTIONABLE or it changes how management understands the plant.

EXAMPLES OF THE DEPTH EXPECTED (from a previous month, do not copy — match the calibre):
- "The bottleneck moved from reliability to dispatch: April lost 90.2 h to incidents and almost nothing \
to full silos; June lost 34.7 h to incidents but 51.6 h to full silos. The mill is no longer the constraint."
- "610 t of Clinker M vanished between the May closing and the June opening with zero recorded consumption, \
yet TOTAL clinker is conserved to within 39 t (0.07%) because SFW and J opened above their May closings by a \
matching amount. This is a pile reclassification, not a physical loss — but it is undocumented."
- "The SPC savings were partly bought with fineness, and that margin is now gone: Super white Blaine fell \
4693 -> 4616 -> 4448 across three months against a 4400 minimum. Further energy gains must come from process \
optimization, not coarser grinding."
- "All sub-minimum t/h days were short runs immediately after stoppages, while uninterrupted 24 h days ran \
19.9-21.3 t/h. The plant does not have a productivity problem, it has a restart-frequency problem."

STRICT RULES:
- Use ONLY numbers present in the evidence pack. NEVER invent, estimate, or extrapolate a figure.
- If evidence for a suspected link is missing, say what data would confirm it instead of guessing.
- Write in ENGLISH, plain professional prose. No emoji. No markdown headers inside the fields.
- Quantities: match the pack's units and rounding.
- Be specific: name days, products, materials, months.
- Never repeat the same finding twice across insights.

OUTPUT: respond with ONE valid JSON object and NOTHING else — no preamble, no code fences.
Schema:
{
  "headline": "<one sentence, max 30 words: the single most important thing management must know this month>",
  "insights": [
    {"title": "<short bold claim, max 12 words>",
     "body": "<60-110 words: the mechanism, the linked evidence with numbers, the consequence>"}
  ],
  "recommendations": [
    {"priority": "URGENT|HIGH|MEDIUM|MONITOR|POSITIVE",
     "text": "<one actionable sentence with the quantified reason>"}
  ]
}
Produce between 12 and 15 insights, ordered most-important first.
Produce between 8 and 14 recommendations, ordered URGENT first, and include at least one POSITIVE if the \
evidence supports it."""


def _round(o, nd=2):
    """Recursively round floats so the prompt stays compact and unambiguous."""
    if isinstance(o, float):
        return round(o, nd)
    if isinstance(o, dict):
        return {k: _round(v, nd) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_round(v, nd) for v in o]
    return o


def build_evidence(cur_metrics, cur_extra, history):
    """
    cur_metrics : metrics_json() of the report month
    cur_extra   : dict with day-level detail for the report month (stoppage log,
                  per-product daily rows, silo/stock movements)
    history     : list of metrics_json() dicts for ALL other stored months
    """
    hist = sorted([h for h in history if h], key=lambda m: (m['year'], m['month']))
    pack = {
        'report_month': f"{calendar.month_name[cur_metrics['month']]} {cur_metrics['year']}",
        'house_rules': {
            'summary_sheet_is_authoritative': True,
            'all_clinker_variants_consolidated': True,
            'M50_ships_100pct_bulk_no_packing': True,
            'grey_pool_J_plus_M_for_M50_min_months': 6,
            'white_pool_ALB_SFW_RAK_for_whites_min_months': 4,
            'naming_maps': {'Power white-R': 'CEM I 52.5R', 'Super white Special': 'M10'},
            'clinker_prices_jd_per_t': {'grey_J_M': 36, 'white_ALB_SFW_RAK_ROY': 100},
            'white_clinker_in_M50_is_a_cost_leak': 'grey product must not consume 100 JD/t white clinker',
        },
        'current_month': _round(cur_metrics),
        'current_month_detail': _round(cur_extra),
        'previous_months': [_round(h) for h in hist],
        'notes_for_analyst': [
            "Zero-production days that fall on Fridays indicate the weekly planned-stop regime.",
            "Compare each month's clinker pile closing balance with the next month's opening balance: "
            "a mismatch with total clinker conserved indicates a pile reclassification, not a loss.",
            "Compare production vs packing vs silo level change per product to find unaccounted tonnage; "
            "M50 is bulk so it has no packing figure.",
            "Short runs after stoppages tend to show inflated plant SPC (fixed loads spread over few tons) "
            "and clinker-rich recipes (feeders not yet stabilized).",
            "Check material_daily_consumption_t for M50: any ALB/SFW/RAK/ROY tonnage there is premium white "
            "clinker (100 JD/t) consumed in a grey product (36 JD/t) — cross-reference those days with the "
            "recipe deviation days and the stoppage/changeover log to explain WHY it happened.",
            "High material moisture (see material_daily_moisture_pct) consumes drying energy — correlate "
            "high-moisture materials with the SPC of the products consuming them.",
        ],
    }
    return pack


def generate_insights(client, cur_metrics, cur_extra, history, max_tokens=8000):
    """Returns dict with headline/insights/recommendations, or None on failure."""
    pack = build_evidence(cur_metrics, cur_extra, history)
    payload = json.dumps(pack, ensure_ascii=False, default=str)
    if len(payload) > 300000:  # keep well inside context limits
        pack['current_month_detail'].pop('daily_products', None)
        payload = json.dumps(pack, ensure_ascii=False, default=str)
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=max_tokens, system=SYSTEM,
            messages=[{"role": "user", "content":
                       "EVIDENCE PACK (all figures pre-computed, do not recalculate):\n\n"
                       + payload +
                       "\n\nWrite the analytical section now. Respond with the JSON object only."}])
        txt = "".join(b.text for b in resp.content if getattr(b, 'type', '') == 'text').strip()
        if txt.startswith('```'):
            txt = txt.split('```')[1]
            if txt.startswith('json'):
                txt = txt[4:]
        start, end = txt.find('{'), txt.rfind('}')
        if start < 0 or end < 0:
            raise ValueError('no JSON object in model response')
        data = json.loads(txt[start:end + 1])
        if not data.get('insights'):
            raise ValueError('empty insights')
        # hard sanity: drop malformed entries rather than failing the whole report
        data['insights'] = [i for i in data['insights']
                            if isinstance(i, dict) and i.get('title') and i.get('body')]
        data['recommendations'] = [r for r in data.get('recommendations', [])
                                   if isinstance(r, dict) and r.get('text')]
        if not data['insights']:
            raise ValueError('all insights malformed')
        logger.info(f"AI insights: {len(data['insights'])} insights, "
                    f"{len(data['recommendations'])} recommendations")
        return data
    except Exception as e:
        logger.warning(f"AI insight generation failed, falling back to rules: {e}")
        return None
