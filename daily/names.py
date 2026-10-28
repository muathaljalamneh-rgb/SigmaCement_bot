"""Canonical product / material names and the mappings agreed with Eng. Imad (answers A2, A3, B4)."""
import re

# canonical product keys → Arabic display name
PRODUCT_AR = {
    'ultra': 'ألترا وايت', 'power': 'باور وايت', 'super': 'سوبر وايت', 'eco': 'إيكو وايت',
    'low_alkali': 'منخفض القلويات', 'm10': 'M10', 'm50': 'M50', 'vpa': 'VPA', 'flushing': 'غسيل المطحنة',
    'transition': 'إسمنت انتقالي',
}
PRODUCT_ORDER = ['ultra', 'power', 'super', 'eco', 'low_alkali', 'm10', 'm50', 'vpa', 'flushing']
WHITE = {'ultra', 'power', 'super', 'eco'}
GREY = {'low_alkali', 'm10', 'm50', 'vpa'}

# productivity targets t/h (Analyses doc table 0); others: no target (answer B5)
TARGET_TPH = {'m50': 19.4, 'ultra': 19.9, 'power': 21.0, 'super': 21.5, 'eco': 22.0}

# silo capacity t and dedication (Analyses doc table 1)
SILOS = {1: (600, 'M50'), 2: (600, 'M50'), 3: (600, 'سوبر وايت'), 4: (200, 'باور / ألترا وايت'),
         5: (200, 'إيكو / باور / ألترا وايت')}

# clinker prices JD/t (answer G3)
PRICE_WHITE_CLINKER = 90.0
PRICE_GREY_CLINKER = 34.0

MATERIALS = ['Clinker ROY', 'Clinker SFW', 'Clinker J', 'Clinker RAK', 'Clinker ALB', 'Clinker M',
             'Limestone HG', 'VPA', 'Sand', 'Pozzolana', 'Gypsum Az', 'Gypsum Taf']
WHITE_CLINKERS = ['Clinker ROY', 'Clinker SFW', 'Clinker RAK', 'Clinker ALB']
GREY_CLINKERS = ['Clinker J', 'Clinker M']
MATERIAL_AR = {
    'Clinker ROY': 'كلنكر رويال', 'Clinker SFW': 'كلنكر صفوة', 'Clinker J': 'كلنكر الجوف',
    'Clinker RAK': 'كلنكر رأس الخيمة', 'Clinker ALB': 'كلنكر سيناء', 'Clinker M': 'كلنكر المناصير',
    'Clinker IRAQ': 'كلنكر العراق', 'Limestone HG': 'حجر جيري', 'VPA': 'VPA', 'Sand': 'رمل',
    'Pozzolana': 'بوزولانا', 'Gypsum Az': 'جبس الأزرق', 'Gypsum Taf': 'جبس الطفيلة',
    'Clinker (total)': 'إجمالي الكلنكر', 'Basalt': 'بازلت', 'Limestone LG': 'حجر جيري LG',
    'Grinding aids': 'مساعد طحن', 'Quality enhancer': 'محسّن جودة', 'Air entraining agent': 'عامل هواء',
}


def norm(s):
    return re.sub(r'[\s\-_/]+', ' ', str(s or '')).strip().lower()


def product_key(name):
    """Map any product label from any report to a canonical key (None if unknown)."""
    n = norm(name).replace(',', '.')
    if not n:
        return None
    if 'transition' in n:
        return 'transition'
    if 'flushing' in n or 'pozz crete' in n or n == 'crete' or 'pozzcrete' in n:
        return 'flushing'          # B4: the pozz-crete block is the flushing product
    if 'low al' in n:
        return 'low_alkali'
    if 'vpa' in n:
        return 'vpa'
    if 'm10' in n:
        return 'm10'
    if 'm50' in n:
        return 'm50'
    if 'ultra' in n or 'power white r' in n or 'powerwhiter' in n:
        return 'ultra'
    if 'power' in n:
        return 'power'
    if 'super' in n:
        return 'super'
    if 'eco' in n:
        return 'eco'
    # standardized / production-block names
    if 'wi' in n.split() or n.endswith(' wi'):
        if '52.5 r' in n:
            return 'ultra'
        if 'a ll 52.5' in n:
            return 'power'
        if 'b ll 42.5' in n:
            return 'super'
        if 'b ll 32.5' in n:
            return 'eco'
    if n.startswith('cem i 52.5 r'):
        return 'ultra'
    if n.startswith('cem i 52.5 n'):
        return 'power'           # A2: old label of the Power-white block
    if n.startswith('cem ii 42.5 n'):
        return 'super'
    if n.startswith('cem ii 32.5 n'):
        return 'eco'
    return None


def material_key(name):
    """Map material labels (production, stock, truck-scale tabs, lab sources) to MATERIALS keys."""
    n = norm(name)
    if not n:
        return None
    table = [
        (('roy', 'royal'), 'Clinker ROY'), (('sfw', 'safwa', 'صفوه', 'صفوة'), 'Clinker SFW'),
        (('rak', 'ras al', 'رأس الخيمة'), 'Clinker RAK'), (('alb', 'sina', 'سينا'), 'Clinker ALB'),
        (('iraq',), 'Clinker IRAQ'),
        (('black clinker', 'clinker j', 'jouf', 'الجوف'), 'Clinker J'),
        (('clinker m', 'manaseer', 'مناصير'), 'Clinker M'),
        (('limestone', 'lime', 'l.s'), 'Limestone HG'),
        (('pozz', 'بوزلانا', 'بوزولانا'), 'Pozzolana'),
        (('taf', 'طفيلة'), 'Gypsum Taf'), (('azraq', 'alazraq', 'الازرق', 'gypsum az'), 'Gypsum Az'),
        (('sand', 'silica'), 'Sand'), (('vpa',), 'VPA'),
    ]
    if n in ('clinker r', 'r'):
        return 'Clinker ROY'
    if n in ('m',):
        return 'Clinker M'
    if n in ('j',):
        return 'Clinker J'
    for keys, mk in table:
        if any(k in n for k in keys):
            return mk
    return None
