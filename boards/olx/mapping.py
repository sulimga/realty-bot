"""
CRM -> параметри OLX. Поки лише «Продаж будинків» (категорія 1602).

Обов'язкові в 1602 (з визначення категорії posting-services):
  price, total_floors, total_area, number_of_rooms, land_area (цілі числа!), property_type_houses
Решту заповнюємо лише те, що є в CRM: ремонт, тип стін (за замовчуванням цегла).
"""

import re

CATEGORY_HOUSE_SALE = 1602
MAX_PHOTOS = {CATEGORY_HOUSE_SALE: 24}
TITLE_MIN, TITLE_MAX = 16, 70
DESC_MIN, DESC_MAX = 80, 9000

HOUSE_KINDS = {
    "будинок": "house", "дом": "house", "приватний будинок": "house", "частный дом": "house",
    "вілла": "house", "вилла": "house",
    "котедж": "cottage", "коттедж": "cottage",
    "клубний будинок": "club_house",
    "частина будинку": "house_part", "часть дома": "house_part",
    "таунхаус": "townhouse",
    "дача": "country_house",
    "дуплекс": "duplex",
    "модульний будинок": "modular_homes", "модульный дом": "modular_homes",
}
HOUSE_KIND_LABELS = [("house", "Будинок"), ("club_house", "Клубний будинок"), ("cottage", "Котедж"),
                     ("house_part", "Частина будинку"), ("townhouse", "Таунхаус"), ("country_house", "Дача"),
                     ("duplex", "Дуплекс"), ("modular_homes", "Модульні будинки")]

# Ремонт: ті самі правила, що для dom.ria («з ремонтом» -> найвищий рівень і т.д.)
REPAIR = {
    "з ремонтом": "1",          # Авторський проект
    "с ремонтом": "1",
    "дизайнерський ремонт": "1",
    "євроремонт": "2",
    "частковий ремонт": "3",    # Косметичний ремонт
    "житловий стан": "4",
    "без ремонту": "5",         # Після будівельників
    "без ремонта": "5",
    "під чистову": "6",         # Під чистову обробку
    "под чистовую": "6",
}
REPAIR_LABELS = {"1": "Авторський проект", "2": "Євроремонт", "3": "Косметичний ремонт",
                 "4": "Житловий стан", "5": "Після будівельників", "6": "Під чистову обробку",
                 "7": "Аварійний стан"}

WALLS = [  # (ключові слова, код OLX house_type)
    (("цегл", "кирпич"), "0"),
    (("панел",), "1"),
    (("моноліт", "монолит"), "2"),
    (("шлакоблок",), "3"),
    (("дерев", "дерев'", "брус", "зруб", "сруб"), "4"),
    (("газоблок", "газобетон", "піноблок", "пеноблок", "керамоблок", "кераміч"), "5"),
    (("сип", "sip"), "6"),
]
WALLS_LABELS = {"0": "Цегляний", "1": "Панельний", "2": "Монолітний", "3": "Шлакоблочний",
                "4": "Дерев'яний", "5": "Газоблок", "6": "СИП панель", "7": "Інше"}
WALLS_DEFAULT = "0"

CURRENCY = [("$", "USD"), ("usd", "USD"), ("дол", "USD"), ("€", "EUR"), ("eur", "EUR"), ("євро", "EUR"),
            ("грн", "UAH"), ("₴", "UAH"), ("uah", "UAH")]


def get(params, *keys):
    for k in keys:
        if params.get(k) not in (None, ""):
            return params[k]
    return None


def num(value):
    """'75,5 м²' -> 75.5 ; None, якщо числа нема."""
    if value is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(value).replace(" ", "").replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def to_int_str(x):
    """OLX приймає лише цілі (^[1-9]\\d*$): 75.5 -> '76', 0.4 -> '1'."""
    if x is None:
        return None
    return str(max(1, int(round(float(x)))))


def parse_price(raw):
    """'2 500 000 $' -> ('2500000', 'USD')"""
    if not raw:
        return None, None
    s = str(raw).lower()
    digits = re.sub(r"[^\d]", "", s)
    cur = next((c for sign, c in CURRENCY if sign in s), None)
    return (digits or None), cur


def deal_kind(deal_title):
    """'Продаж / Будинок' -> ('продаж', 'будинок')"""
    parts = [p.strip().lower() for p in (deal_title or "").split("/")]
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def is_house_sale(estate):
    deal, kind = deal_kind(estate.get("deal_title"))
    return deal in ("продаж", "продажа") and kind in HOUSE_KINDS


def house_kind(estate):
    p = estate.get("parameters") or {}
    for raw in (get(p, "Тип будинку", "Тип дома"), deal_kind(estate.get("deal_title"))[1]):
        if raw and raw.strip().lower() in HOUSE_KINDS:
            return HOUSE_KINDS[raw.strip().lower()]
    return "house"


def walls_code(estate):
    p = estate.get("parameters") or {}
    raw = get(p, "Тип стін", "Матеріал стін", "Тип стен", "Материал стен", "Стіни", "Стены")
    if raw:
        low = raw.lower()
        for keys, code in WALLS:
            if any(k in low for k in keys):
                return code, raw
        return None, raw          # є значення, але не розпізнали -> спитаємо
    return WALLS_DEFAULT, None


def repair_code(estate):
    p = estate.get("parameters") or {}
    raw = get(p, "Ремонт", "Стан", "Состояние")
    if not raw:
        return None, None
    return REPAIR.get(raw.strip().lower()), raw


def city_and_district(address):
    """
    'Київ, Дарницький район, вул. Пчілки Олени, 7' -> ('Київ', 'Дарницький')
    'Києво-Святошинський район, Лісники, вул. Стежки' -> ('Лісники', None)
    Місто — перша частина, що не є районом/областю.
    """
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    city, district, seen_city = None, None, False
    for p in parts:
        low = p.lower()
        is_area = bool(re.search(r"\b(район|р-н|область|обл\.?)(\b|$)", low))
        if not seen_city:
            if is_area:
                continue
            city, seen_city = re.sub(r"^(м\.|місто|смт\.?|с\.|село)\s*", "", p, flags=re.I).strip(), True
        elif is_area and district is None:
            district = re.sub(r"\s*(район|р-н)\.?\s*", "", p, flags=re.I).strip()
    return city, district


def spaced_article(article):
    """'#A35862' -> 'A 3 5 8 6 2' (OLX ріже/ховає послідовності цифр в описі)."""
    a = (article or "").lstrip("#").strip()
    return " ".join(a) if a else ""


def default_title(estate, params, city):
    kind = dict(HOUSE_KIND_LABELS).get(params.get("property_type_houses"), "Будинок")
    bits = [f"Продаж: {kind.lower()} {params.get('total_area')} м²"]
    if params.get("land_area"):
        bits.append(f"{params['land_area']} сот.")
    if city:
        bits.append(city)
    t = ", ".join(bits)
    if len(t) > TITLE_MAX and city:
        t = ", ".join(bits[:-1])
    return t[:TITLE_MAX]


def title_candidates(estate, params, city):
    out = [default_title(estate, params, city)]
    first = ((estate.get("description") or "").strip().splitlines() or [""])[0].strip(" .!-—")
    if TITLE_MIN <= len(first) <= TITLE_MAX and first not in out:
        out.append(first)
    return out


def description(estate):
    text = (estate.get("description") or "").strip()
    art = spaced_article(estate.get("article"))
    if art:
        text = (text + "\n\n" + art).strip()
    return text[:DESC_MAX]
