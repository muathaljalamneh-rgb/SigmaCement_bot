import datetime as dt

from daily.analysis import tile_day, interp, fan_check, qc_clinker_set
from daily.parsers import parse_range
from daily.names import product_key, material_key
from daily.service import report_day


def test_tile_day_contiguous_products():
    d1, f, segs = tile_day([('m50', 7855.0, 7859.0), ('super', 7859.0, 7870.0), ('power', 7870.0, 7879.0)], 7855.0, 0.05)
    assert d1 == 7879.0
    assert not [x for x in f if x['type'] != 'multi_ok']
    assert [s[0] for s in segs] == ['m50', 'super', 'power']


def test_tile_day_flags_gap_and_typo():
    _, f, _ = tile_day([('m50', 7855.0, 7859.0), ('power', 7860.0, 7879.0)], 7855.0, 0.05)
    assert any(x['type'] == 'gap' for x in f)


def test_tile_day_product_made_twice_is_sum_of_starts():
    # 14 Aug: Super-white before and after Eco-white; the counter holds the SUM of both starts (answer B1)
    iv = [('super', 7879.0 + 7892.191263, 7881.606095 + 7903.0), ('eco', 7881.606095, 7892.191263)]
    _, f, segs = tile_day(iv, 7879.0, 0.05)
    assert any(x['type'] == 'multi_ok' and x['product'] == 'super' for x in f)
    assert [s[0] for s in segs] == ['super', 'eco', 'super']


def test_parse_range():
    assert parse_range('≤1.2') == (None, 1.2)
    assert parse_range('≥ 91') == (91.0, None)
    assert parse_range('4000 ±100') == (3900.0, 4100.0)
    assert parse_range('95-105') == (95.0, 105.0)


def test_fan_check_interpolates_and_counts_steps():
    table = [(1300, 15.37, 0.575), (1350, 17.22, 0.597), (1400, 19.2, 0.619), (1450, 21.33, 0.642)]
    c = fan_check(1450, 15.37, table)
    assert abs(c['kw_calc'] - 21.33) < 1e-6
    assert abs(c['steps'] - 3.0) < 1e-6
    assert interp(1325, [1300, 1350], [15.37, 17.22]) == (15.37 + 17.22) / 2


def test_names():
    assert product_key('CEM II/A-LL 52,5 N - Wi') == 'power'
    assert product_key('CEM I 52,5 R - Wi') == 'ultra'
    assert product_key('Transition cement') == 'transition'
    assert product_key('Wateen M50') == 'm50'
    assert product_key('pozz-crete') == 'flushing'
    assert material_key('شركة سينا / مصر') == 'Clinker ALB'
    assert material_key('Black clinker') == 'Clinker J'
    assert qc_clinker_set('Mixing J+M') == {'Clinker J', 'Clinker M'}


def test_report_day_window():
    assert report_day(dt.datetime(2026, 8, 26, 9, 30)) == dt.date(2026, 8, 25)
    assert report_day(dt.datetime(2026, 8, 26, 7, 0)) == dt.date(2026, 8, 24)
