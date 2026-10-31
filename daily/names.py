"""Canonical product / material names and the mappings agreed with Eng. Imad (answers A2, A3, B4)."""
import re

# canonical product keys → display name
PRODUCT_NAMES = {
    'ultra': 'Ultra-white', 'power': 'Power-white', 'super': 'Super-white', 'eco': 'Eco-white',
    'low_alkali': 'Low-alkali', 'm10': 'M10', 'm50': 'M50', 'vpa': 'VPA', 'flushing': 'Flushing',
    'transition': 'Transition cement',
}
PRODUCT_ORDER = ['ultra', 'power', 'super', 'eco', 'low_alkali', 'm10', 'm50', 'vpa', 'flushing']
WHITE = {'ultra', 'power', 'super', 'eco'}
GREY = {'low_alkali', 'm10', 'm50', 'vpa'}

# productivity targets t/h (Analyses doc table 0); others: no target (answer B5)
TARGET_TPH = {'m50': 19.4, 'ultra': 19.9, 'power': 21.0, 'super': 21.5, 'eco': 22.0}

# silo capacity t and dedication (Analyses doc table 1)
SILOS = {1: (600, 'M50'), 2: (600, 'M50'), 3: (600, 'Super-white'), 4: (200, 'Power / Ultra-white'),
         5: (200, 'Eco / Power / Ultra-white')}

# clinker prices JD/t (answer G3)
PRICE_WHITE_CLINKER = 90.0
PRICE_GREY_CLINKER = 34.0

MATERIALS = ['Clinker ROY', 'Clinker SFW', 'Clinker J', 'Clinker RAK', 'Clinker ALB', 'Clinker M',
             'Limestone HG', 'VPA', 'Sand', 'Pozzolana', 'Gypsum Az', 'Gypsum Taf']
WHITE_CLINKERS = ['Clinker ROY', 'Clinker SFW', 'Clinker RAK', 'Clinker ALB']
GREY_CLINKERS = ['Clinker J', 'Clinker M']
MATERIAL_NAMES = {
    'Clinker ROY': 'Clinker ROY (Royal)', 'Clinker SFW': 'Clinker SFW (Safwa)', 'Clinker J': 'Clinker J (Jouf)',
    'Clinker RAK': 'Clinker RAK', 'Clinker ALB': 'Clinker ALB (Sina)', 'Clinker M': 'Clinker M (Manaseer)',
    'Clinker IRAQ': 'Clinker Iraq', 'Limestone HG': 'Limestone HG', 'VPA': 'VPA', 'Sand': 'Sand',
    'Pozzolana': 'Pozzolana', 'Gypsum Az': 'Gypsum Azraq', 'Gypsum Taf': 'Gypsum Tafila',
    'Clinker (total)': 'Clinker (total)', 'Basalt': 'Basalt', 'Limestone LG': 'Limestone LG',
    'Grinding aids': 'Grinding aid', 'Quality enhancer': 'Quality enhancer', 'Air entraining agent': 'Air entraining agent',
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
