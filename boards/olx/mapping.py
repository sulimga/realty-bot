"""
CRM -> параметри OLX.

Категорії й поля беремо з categories.json — знімок дерева «Нерухомість» з
posting-services /v2/categories (03.11.2026). Підтримуємо:
  будинки       — продаж 1602, оренда 330
  квартири      — продаж 1758, оренда 1760
  комерція      — продаж 3679–3689, оренда 3690–3701 (тип — за назвою з CRM, інакше питаємо)
Земля, кімнати, гаражі, нерухомість за кордоном — поки ні.

Заповнюємо лише обов'язкові поля категорії + те, що вже налаштовано (ремонт, тип стін,
«вторинний ринок»). Числа OLX приймає лише цілі (^[1-9]\\d*$).
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "categories.json"), encoding="utf-8") as _f:
    CATS = json.load(_f)

# Заголовок: без категорії сайт пускає до 70 символів, з обраною категорією — до 150
# (схема ads/validation/<id>.ad.olxua.uk.schema.json). Ми завжди шлемо category_id.
TITLE_MIN, TITLE_MAX = 16, 150
DESC_MIN, DESC_MAX = 80, 9000

SALE, RENT = "sale", "rent"
HOUSE, FLAT, COMMERCIAL = "house", "flat", "commercial"
FAMILY_CATS = {HOUSE: {SALE: 1602, RENT: 330}, FLAT: {SALE: 1758, RENT: 1760}}

# Тип комерції: (регулярка по назві з CRM, продаж, оренда, «чого» — для заголовка).
# Порядок важливий: конкретніше — вище, загальне («приміщення», «нежитлове») — в кінці.
COMMERCIAL_TYPES = [
    (r"\bмаф|кіоск|киоск|павіл|павил", 3680, 3695, "МАФу"),
    (r"\bсто\b|автомий|автомой|\bазс\b|шиномонт", 3685, 3696, "приміщення під СТО"),
    (r"готел|гостиниц|хостел|баз[аиу] відпоч|баз[аиу] отдых", 3684, None, "готелю"),
    (r"кафе|ресторан|кав.ярн|кофейн|\bбар\b", 3683, 3694, "приміщення під кафе/ресторан"),
    (r"салон|перукарн|парикмах|косметолог|краси\b", 3686, 3697, "приміщення під салон краси"),
    (r"медич|медиц|клінік|клиник|стоматолог", 3686, 3699, "медичного приміщення"),
    (r"спорт|фітнес|фитнес|\bзал\b", None, 3698, "залу"),
    (r"склад|ангар", 3682, 3693, "складу"),
    (r"виробн|производ|\bцех|ферм", 3687, 3691, "виробничого приміщення"),
    (r"офіс|офис|бізнес.?центр|бизнес.?центр", 3681, 3690, "офісу"),
    (r"торгов|торгів|магазин|фасад|ритейл|рітейл", 3679, 3692, "торгового приміщення"),
    (r"вільного призн|свободного назн|комерц|коммерч|нежитл|нежил|приміщен|помещен|будівл|здани",
     3688, 3700, "приміщення вільного призначення"),
]
# «чого» для категорій, які можна обрати лише вручну
COMMERCIAL_NOUNS = {3689: "комерційного приміщення", 3701: "комерційного приміщення"}
for _rx, _s, _r, _noun in COMMERCIAL_TYPES:
    for _cid in (_s, _r):
        if _cid:
            COMMERCIAL_NOUNS.setdefault(_cid, _noun)
COMMERCIAL_CATS = {SALE: list(range(3679, 3690)), RENT: list(range(3690, 3702))}

NOT_SUPPORTED = [  # (регулярка, що це) — для таких OLX-програма поки не вміє
    (r"земл|ділянк|участ", "земля"),
    (r"гараж|паркомісц|паркомест|парковоч|машиномісц|машиномест", "гаражі/паркомісця"),
    (r"^кімнат|^комнат", "кімнати"),
]
FLAT_RX = r"квартир|апартамент|студі|студио|пентхаус|смарт"
HOUSE_RX = r"будинок|будинку|\bдом\b|котедж|коттедж|вілл|вилл|таунхаус|дуплекс|\bдач"

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
HOUSE_KIND_GEN = {"house": "будинку", "club_house": "клубного будинку", "cottage": "котеджу",
                  "house_part": "частини будинку", "townhouse": "таунхауса", "country_house": "дачі",
                  "duplex": "дуплекса", "modular_homes": "модульного будинку"}

ROOMS_KEYS = {1: "odnokomnatnye", 2: "dvuhkomnatnye", 3: "trehkomnatnye", 4: "chetyrehkomnatnye",
              5: "pyatikomnatnye"}
ROOM_WORDS = {"одно": 1, "дво": 2, "двох": 2, "три": 3, "трьох": 3, "чотири": 4, "чотирьох": 4,
              "п'яти": 5, "пʼяти": 5, "пяти": 5, "шести": 6, "шістьох": 6}

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
# комерція: «Ремонт» лише так/ні
IS_REPAIRED = {"1": "yes", "2": "yes", "3": "yes", "4": "yes", "5": "no", "6": "no", "7": "no"}

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

STREET_RX = (r"^(вул\.?|вулиця|ул\.?|улица|просп\.?|проспект|пр-т|бульв\.?|бульвар|бул\.?|пров\.?|провулок|"
             r"пер\.?|переулок|пл\.?|площа|площадь|наб\.?|набережна|набережная|шосе|шоссе|узвіз|спуск|"
             r"тупик|алея|аллея|в'їзд|проїзд|проезд|майдан|дорога)(\s|$)")
FLAGS = ("from_developer", "commission", "is_exchange", "cooperate", "eoselia")


# ------------------------------------------------------------------ категорії
def cat(cid):
    return CATS[str(cid)]


def cat_label(cid):
    return cat(cid)["label"]


def param_def(cid, code):
    return next((p for p in cat(cid)["params"] if p["code"] == code), None)


def has_param(cid, code):
    return param_def(cid, code) is not None


def required_params(cid):
    return [p for p in cat(cid)["params"] if p["req"]]


def param_values(cid, code):
    """[(key, label), …] для enum-поля."""
    pd = param_def(cid, code) or {}
    return [tuple(v.split("=", 1)) for v in (pd.get("values") or [])]


def value_label(cid, code, key):
    return dict(param_values(cid, code)).get(str(key), str(key))


def max_photos(cid):
    return cat(cid).get("max_photos") or 24


# ------------------------------------------------------------------ дрібні парсери
def get(params, *keys):
    for k in keys:
        if params.get(k) not in (None, ""):
            return params[k]
    return None


def num(value):
    """'75,5 м²' -> 75.5 ; None, якщо числа нема."""
    if value is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(value).replace(" ", "").replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def to_int_str(x):
    """OLX приймає лише цілі (^[1-9]\\d*$): 75.5 -> '76', 0.4 -> '1'."""
    if x is None:
        return None
    return str(max(1, int(round(float(x)))))


def split_pair(val):
    """'14 / 24' -> (14, 24); '14' -> (14, None)."""
    m = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", str(val or ""))
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else None) if m else (None, None)


def parse_price(raw):
    """'2 500 000 $' -> ('2500000', 'USD')"""
    if not raw:
        return None, None
    s = str(raw).lower()
    digits = re.sub(r"[^\d]", "", s)
    cur = next((c for sign, c in CURRENCY if sign in s), None)
    return (digits or None), cur


def find_rooms_in_text(text):
    """'3 кімнати', '2-кімнатна', 'двокімнатна', 'студія' -> число кімнат або None."""
    t = (text or "").lower().replace("’", "'")
    m = re.search(r"(\d+)\s*[-–]?\s*(?:кімн|комн|к\.?\s*кв)", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(" + "|".join(ROOM_WORDS) + r")\s*[-–]?\s*(?:кімн|комн)", t)
    if m:
        return ROOM_WORDS[m.group(1)]
    return 1 if re.search(r"студі|студио", t) else None


def find_floor_in_text(text):
    """(поверх, поверховість) з тексту: '24 поверх', '5 з 9 поверх', '14/24 пов.'."""
    t = (text or "").lower()
    m = re.search(r"(\d{1,2})\s*(?:-?й|-?го|-?му|-?ому)?\s*(?:повер[хс]\w*|эт\w*)\s*(?:з|із|из)\s*(\d{1,2})", t) \
        or re.search(r"(\d{1,2})\s*/\s*(\d{1,2})\s*(?:поверх|пов\b|эт)", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    floor = None
    m = re.search(r"(\d{1,2})\s*(?:-?й|-?го|-?му|-?ому|-?м)?\s*(?:повер[хс]\w*|эт\w*)", t)
    if m:
        floor = int(m.group(1))
    floors = None
    m = re.search(r"(\d{1,2})\s*-?\s*(?:поверхов\w*|этажн\w*)", t) or \
        re.search(r"(?:поверховість|этажность)\D{0,5}(\d{1,2})", t)
    if m:
        floors = int(m.group(1))
    return floor, floors


def rooms_key(n):
    return ROOMS_KEYS[min(max(int(n), 1), 5)]


# ------------------------------------------------------------------ що це за оголошення
def deal_kind(deal_title):
    """'Продаж / Будинок' -> ('продаж', 'будинок'). Ріжемо лише по першому «/»."""
    parts = [p.strip().lower() for p in (deal_title or "").split("/", 1)]
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def deal_oper(deal):
    d = (deal or "").lower()
    if re.search(r"подоб|посуточ|погодин|почасов", d):
        return None
    if "прод" in d:
        return SALE
    if "оренд" in d or "аренд" in d:
        return RENT
    return None


def commercial_category(text, oper):
    """Категорія комерції за текстом типу з CRM; None — не впізнали або на OLX такої нема."""
    t = (text or "").lower().replace("’", "'")
    for rx, sale, rent, _ in COMMERCIAL_TYPES:
        if re.search(rx, t):
            return sale if oper == SALE else rent
    return None


def detect(estate):
    """
    -> (oper, family, category_id | None, пояснення).
    family None + пояснення — OLX-програма такого не вміє. category_id None при family=commercial —
    тип не впізнали, треба спитати.
    """
    deal, kind = deal_kind(estate.get("deal_title"))
    oper = deal_oper(deal)
    if not oper:
        return None, None, None, f"угода «{deal or '?'}» — вмію лише продаж і довгострокову оренду"
    for rx, what in NOT_SUPPORTED:
        if re.search(rx, kind):
            return oper, None, None, f"{what} на OLX поки не підключено"
    if re.search(FLAT_RX, kind):
        return oper, FLAT, FAMILY_CATS[FLAT][oper], ""
    if kind in HOUSE_KINDS or re.search(HOUSE_RX, kind):
        return oper, HOUSE, FAMILY_CATS[HOUSE][oper], ""
    p = estate.get("parameters") or {}
    extra = get(p, "Тип об'єкта", "Тип объекта", "Тип приміщення", "Тип помещения", "Призначення", "Назначение") or ""
    cid = commercial_category(kind, oper) or commercial_category(extra, oper)
    return oper, COMMERCIAL, cid, ""


def category_choices(oper):
    """Усі категорії, які можна обрати вручну для цієї угоди: [(id, назва)]."""
    ids = [FAMILY_CATS[FLAT][oper], FAMILY_CATS[HOUSE][oper]] + COMMERCIAL_CATS[oper]
    return [(cid, cat_label(cid)) for cid in ids]


def family_of(cid):
    for fam, d in FAMILY_CATS.items():
        if cid in d.values():
            return fam
    return COMMERCIAL


# ------------------------------------------------------------------ поля з CRM
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


def complex_name(estate):
    p = estate.get("parameters") or {}
    zk = (get(p, "Комплекс", "ЖК") or "").strip()
    if not zk or "не жк" in zk.lower() or zk.lower() in ("-", "—", "нет", "немає", "ні"):
        return None
    return re.sub(r"^(жк|ж/к)\s+", "", zk, flags=re.I).strip() or None


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


def street(address):
    """Вулиця (без номера будинку) з адреси CRM: 'Київ, …, вул. Пчілки Олени, 7' -> 'вул. Пчілки Олени'."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    for p in parts[1:]:
        if re.search(STREET_RX, p.lower()):
            return re.sub(r"\s+\d+[\w/-]*$", "", p).strip()   # «вул. Стежки 7» -> «вул. Стежки»
    return None


def spaced_article(article):
    """'#A35862' -> 'A 3 5 8 6 2' (OLX ріже/ховає послідовності цифр в описі)."""
    a = (article or "").lstrip("#").strip()
    return " ".join(a) if a else ""


# ------------------------------------------------------------------ заголовок і опис
def _fit(must, optional=()):
    """Склеює заголовок: спершу відкидаємо необов'язкові частини (з кінця), обов'язкові лишаються."""
    must = [b for b in must if b]
    opt = [b for b in optional if b]
    while opt and len(", ".join(must + opt)) > TITLE_MAX:
        opt.pop()
    return ", ".join(must + opt)[:TITLE_MAX].rstrip(" ,")


def default_title(cid, params, place):
    """
    place: {"city", "street", "complex"}.
    Будинок — обов'язково населений пункт; квартира — вулиця і ЖК; комерція — вулиця і місто.
    """
    fam = family_of(cid)
    verb = "Продаж" if cid in (FAMILY_CATS[HOUSE][SALE], FAMILY_CATS[FLAT][SALE]) or cid in COMMERCIAL_CATS[SALE] \
        else "Оренда"
    city, st, zk = place.get("city"), place.get("street"), place.get("complex")
    area = params.get("total_area")
    if fam == HOUSE:
        what = HOUSE_KIND_GEN.get(params.get("property_type_houses"), "будинку")
        head = f"{verb} {what} {area} м²"
        land = f"{params['land_area']} сот." if params.get("land_area") else None
        return _fit([head, city], [land])             # населений пункт — обов'язково
    if fam == FLAT:
        rooms = {v: k for k, v in ROOMS_KEYS.items()}.get(params.get("number_of_rooms_string"))
        r = ("багатокімн. " if rooms == 5 else f"{rooms}-кімн. ") if rooms else ""
        head = f"{verb} {r}квартири {area} м²"
        return _fit([head, f"ЖК {zk}" if zk else None, st], [city])   # вулиця і ЖК — обов'язково
    noun = COMMERCIAL_NOUNS.get(cid, "комерційного приміщення")
    return _fit([f"{verb} {noun} {area} м²", st], [city])


def title_ok(t):
    """Правила OLX зі схеми: довжина, без 4+ розділових знаків підряд, без смайликів/посилань/email."""
    if not (TITLE_MIN <= len(t) <= TITLE_MAX):
        return False
    if re.search(r"[!?.,\-=+#%&@*_><:()|~]{4,}", t) or re.search(r"https?://|@", t):
        return False
    return not any(0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF for ch in t)


def title_candidates(estate, cid, params, place):
    out = [default_title(cid, params, place)]
    first = ((estate.get("description") or "").strip().splitlines() or [""])[0].strip(" .!-—")
    if title_ok(first) and first not in out:
        out.append(first)
    return out


def description(estate):
    """Опис CRM + артикул. OLX не приймає в описі посилань і символу «@» — прибираємо їх."""
    text = (estate.get("description") or "").strip()
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"\S*@\S*", "", text)
    art = spaced_article(estate.get("article"))
    if art:
        text = (text + "\n\n" + art).strip()
    return text[:DESC_MAX]
