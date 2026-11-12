"""
Маппінг даних з CRM (результат extract_estate.py) у характеристики dom.ria.

ID характеристик і варіанти значень — у characteristics.json (знято з
публічного довідника dom.ria /node/api/getAdditionalSearchCharacteristics).
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "characteristics.json"), encoding="utf-8") as f:
    CHARS = json.load(f)

# Батьківська категорія для кожного типу (з констант форми dom.ria)
PARENT_BY_TYPE = {2: 1, 3: 40, 5: 4, 6: 4, 7: 4, 8: 4, 9: 4,
                  11: 13, 12: 10, 14: 13, 15: 13, 16: 13, 17: 13, 18: 13, 19: 13,
                  20: 13, 21: 13, 22: 13, 25: 24, 26: 24, 27: 24, 28: 24, 29: 24,
                  31: 30, 32: 30, 33: 30, 34: 30, 35: 30}

TYPE_NAMES = {2: "квартира", 3: "кімната", 5: "будинок", 6: "частина будинку", 7: "дача",
              8: "дуплекс", 9: "таунхаус", 11: "офісне приміщення", 12: "офісна будівля",
              14: "торгова площа", 15: "склад", 16: "виробниче", 17: "кафе/ресторан",
              18: "спеціальне приміщення (сфера послуг)", 19: "готель", 21: "комерційне приміщення",
              22: "готовий бізнес", 25: "ділянка під забудову", 26: "комерційна ділянка",
              27: "с/г ділянка", 34: "гараж"}

# Порядок важливий: специфічніше — вище
TYPE_KEYWORDS = [
    (6, ["частина будинку", "часть дома", "пів будинку", "полдома"]),
    (9, ["таунхаус"]), (8, ["дуплекс"]), (7, ["дача", "дачу"]),
    (3, ["кімнат", "комнат"]),
    (2, ["квартир"]),
    (34, ["гараж", "паркомісц", "машиномісц", "машиномест"]),
    (26, ["комерційн ділянк", "земля комерц", "ділянка комерц", "участок коммерч"]),
    (27, ["с/г", "сільськогосп", "сельхоз"]),
    (25, ["ділянк", "участ", "земл"]),
    (12, ["офісна будівля", "офисное здание", "бізнес-центр"]),
    (11, ["офіс", "офис"]),
    (15, ["склад"]), (16, ["виробнич", "производ"]),
    (17, ["кафе", "ресторан", "бар"]), (19, ["готел", "гостиниц"]),
    (14, ["магазин", "торгов", "фасад"]),
    (22, ["готовий бізнес", "готовый бизнес"]),
    (21, ["комерц", "коммерч", "приміщен", "помещен", "нежитл", "нежил"]),
    (5, ["будин", "дом", "котедж", "коттедж", "садиб", "усадьб"]),
]

# ---------------------------------------------------------------- комерція (12.11.2026)
# На dom.ria в категорії «Комерційна» (parent 13) насправді лише ТРИ типи (перевірено пошуком сайту 12.11.2026 —
# інші ID 12, 14–17, 19, 20, 22 дають 0 оголошень):
#   11 — «Офісне приміщення» (офісна нерухомість), 18 — «Спеціальне приміщення» (сфера послуг: кафе, СТО, салон…),
#   21 — «Комерційне приміщення» (комерційна нерухомість: торгові, склади, виробничі, ЦІЛІ БУДІВЛІ).
# Плюс обов'язкова характеристика 169 «тип об'єкта» (однакова для всіх трьох) і для 18 — 199 «сфера».
# Правило власника: ціла будівля → 21 + 169=166 «будівля / комплекс / павільйон» (HAR 12.11.2026, адмінбудівля
# на Стрийській, 4 поверхи, 1368 м²); офіс (частина будівлі) → 11.
COMMERCIAL_RT = {11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22}
OBJ = {"trc": 163, "toc": 164, "in_residential": 165, "building": 166, "market_place": 168, "premises": 1974,
       "site": 1975, "business_center": 1976, "admin_building": 1977, "production": 1982, "warehouse": 1983}
WHOLE_BUILDING_RX = (   # лише називний/знахідний відмінок: «будівля/будівлю» = продається вся; «в будівлі» — частина
    r"окремо\s*-?\s*стояч(а|у)\s+будівл|окремостояч(а|у)\s+будівл|отдельно\s*стоящ(ее|ая)\s+(здание|строение)|"
    r"\bосз\b|(ціл[ау]|вся|всю)\s+будівл[яю]|(цел(ое|ого)|все)\s+здани[ея]|будівля\s+повністю|"
    r"продаж\w*\s+(адміністративн\w*\s+|офісн\w*\s+|комерційн\w*\s+|нежитлов\w*\s+)?будівл[іяю]\b|"
    r"продам\s+(адміністративн\w*\s+|офісн\w*\s+|комерційн\w*\s+|нежитлов\w*\s+)?будівл[юя]\b|"
    r"(\d+\s*-?\s*(х\s*)?|(дво|три|чотири|п.яти|шести)|(одно|двух|трех|четырех|пяти)\s*)поверхов(а|у|ая)\s+"
    r"(\w+\s+){0,2}(будівля|будівлю|здание|корпус)\b|(\d+\s*-?\s*(х\s*)?)этажн(ое|ого)\s+(\w+\s+){0,2}здани|"
    r"\bбудівл[яю]\s+(загальною\s+)?площею|\bздание\s+(общей\s+)?площадью|майнов\w+\s+комплекс|имущественн\w+\s+комплекс|"
    r"(^|/)\s*(будівля|здание|окрема будівля|комплекс)\s*$"
)
# (регулярка, тип dom.ria, тип об'єкта 169, сфера 199 для типу 18)
COMMERCIAL_KINDS = [
    (r"кафе|ресторан|\bбар\b|кав.ярн|кофейн|столов|пекарн", 18, "premises", 186),
    (r"готел|гостиниц|хостел|баз[аиу]\s+відпоч|баз[аиу]\s+отдых", 18, "building", 193),
    (r"\bсто\b|автосерв|автомий|автомой|шиномонт", 18, "premises", 194),
    (r"\bазс\b|заправк", 18, "site", 195),
    (r"медич|медиц|клінік|клиник|стоматолог|аптек", 18, "premises", 190),
    (r"салон\s+крас|перукарн|парикмах|косметолог|барбер", 18, "premises", 189),
    (r"спорт|фітнес|фитнес", 18, "premises", 2115),
    (r"автосалон", 18, "premises", 2116),
    (r"склад|ангар", 21, "warehouse", None),
    (r"виробн|производ|\bцех", 21, "production", None),
    (r"трц|торгово.розважальн|торгово.развлекат", 21, "trc", None),
    (r"торгов\w*\s+місц|торгов\w*\s+мест|кіоск|киоск|\bмаф\b", 21, "market_place", None),
    (r"торгово.офісн|торгово.офисн", 21, "toc", None),
    (r"бізнес.?центр|бизнес.?центр|\bбц\b", 11, "business_center", None),
    (r"офіс|офис", 11, "premises", None),
    (r"торгов|торгів|магазин|фасад|ритейл|рітейл|бутик", 21, "premises", None),
    (r"майданчик|площадк", 21, "site", None),
]


def commercial_kind(estate, rt0):
    """
    Тип dom.ria для комерції з даних CRM -> (rt, тип об'єкта 169, сфера 199|None, пояснення, впевнено?).
    Ціла будівля (за словами в заголовку/параметрах/описі) — завжди 21 + «будівля / комплекс / павільйон».
    """
    title = _norm(estate.get("deal_title"))
    obj_title = title.split("/", 1)[1].strip() if "/" in title else title
    params = estate.get("parameters") or {}
    ptxt = _norm(" ".join(f"{k} {v}" for k, v in params.items()
                          if re.search(r"тип|призначен|назначен|об.єкт|объект|будівл|здани", str(k), re.I)))
    desc = _norm(estate.get("description"))
    if re.search(WHOLE_BUILDING_RX, obj_title) or re.search(WHOLE_BUILDING_RX, ptxt) \
            or re.search(WHOLE_BUILDING_RX, desc):
        return 21, OBJ["building"], None, "ціла будівля", True
    for rx, rt, obj, sphere in COMMERCIAL_KINDS:          # спершу — тип із заголовка/параметрів CRM
        if re.search(rx, obj_title) or re.search(rx, ptxt):
            if rt == 11 and obj == "premises":
                if re.search(r"бізнес.?центр|бизнес.?центр|\bбц\b", desc):
                    return 11, OBJ["business_center"], None, "офіс у бізнес-центрі", True
                if re.search(r"житлов\w* (будинк|комплекс)|\bжк\b|жилом доме", desc):
                    return 11, OBJ["in_residential"], None, "офіс у житловому будинку", True
            return rt, OBJ[obj], sphere, obj_title or ptxt, True
    for rx, rt, obj, sphere in COMMERCIAL_KINDS:          # потім — опис (менш надійно → спитаємо)
        if re.search(rx, desc):
            return rt, OBJ[obj], sphere, f"за описом: «{re.search(rx, desc).group(0)}»", False
    in_res = bool(re.search(r"житлов\w* (будинк|комплекс)|\bжк\b|жилом доме", desc))
    return 21, OBJ["in_residential" if in_res else "premises"], None, "загальна комерція", False


# Варіанти для питання, коли тип комерції визначено не впевнено: (rt, 169, 199, підпис)
COMMERCIAL_CHOICES = [
    (21, 1974, None, "Комерційне приміщення (частина будівлі)"),
    (21, 166, None, "Ціла будівля / комплекс"),
    (11, 1974, None, "Офісне приміщення"),
    (11, 1976, None, "Офіс у бізнес-центрі"),
    (21, 165, None, "Нежитлове в житловому будинку"),
    (21, 1983, None, "Склад"),
    (21, 1982, None, "Виробниче приміщення"),
    (21, 168, None, "Торгове місце / МАФ"),
    (18, 1974, 186, "Кафе / ресторан"),
    (18, 1974, 194, "СТО / автосервіс"),
    (18, 1974, 189, "Салон краси / побутові послуги"),
    (18, 1974, 190, "Медицина / аптека"),
    (18, 166, 193, "Готель / база відпочинку"),
]


# Семантичне поле -> кандидати ID характеристик (беремо той, що є в довіднику типу)
SEM = {
    "rooms": [209, 210, 211], "total_area": [214, 215], "living_area": [216],
    "useful_area": [217], "kitchen_area": [218], "floor": [227], "floors": [228, 229],
    "plot_area": [219, 1917], "plot_unit": [226], "walls": [118, 149, 729],
    "year": [443], "ceiling": [505], "condition": [516, 529, 542], "furniture": [2000],
    "bedrooms": [949], "price": [234, 235], "currency": [242, 246],
    "price_for": [247, 251], "offer_type": [1437], "commission": [2014],
    "object_type": [169], "land_purpose": [2099], "garage_purpose": [175],
    "cadastral": [1663],
}

# Російські варіанти з CRM -> українські слова з довідника dom.ria
RU_UK = [
    ("силикатн", "силікатн"), ("кирпич", "цегла"), ("монолит", "моноліт"),
    ("пеноблок", "піноблок"), ("газоблок", "газоблок"), ("пенобетон", "пінобетон"),
    ("керамзитобетон", "керамзітобетон"), ("ракушняк", "ракушняк"), ("сруб", "зруб"),
    ("каркасн", "каркасн"), ("дерев", "дерев"), ("кераміч", "керамічн"), ("керамич", "керамічн"),
    ("евроремонт", "євроремонт"), ("дизайнерск", "дизайнерськ"), ("косметическ", "косметичн"),
    ("удовлетворительн", "задовільн"), ("отличн", "відмінн"), ("хорош", "хорош"),
    ("требует ремонта", "потребує ремонту"), ("без ремонта", "потребує ремонту"),
    ("черновая", "чорнова"), ("без отделки", "без оздоблювальних робіт"),
    ("после строителей", "без оздоблювальних робіт"), ("після будівельників", "без оздоблювальних робіт"),
    ("жилое состояние", "хороший"), ("житловий стан", "хороший"), ("жилой", "хороший"),
    ("аварийн", "аварійн"), ("строительная отделка", "будівельна обробка"),
]

CONDITION_SYNONYMS = {
    # значення CRM (uk) -> слово з довідника, якщо прямого збігу немає
    "з ремонтом": "хороший", "сучасний ремонт": "євроремонт", "авторський": "дизайнерськ",
    "без ремонту": "потребує ремонту", "під ремонт": "потребує ремонту",
    "під чистове": "без оздоблювальних", "дизайнерський": "дизайнерськ",
}


def _norm(s):
    s = str(s or "").lower().replace("ё", "е").replace("’", "'").replace("`", "'")
    for ru, uk in RU_UK:
        s = s.replace(ru, uk)
    return re.sub(r"\s+", " ", s).strip()


def match_option(value, options: dict):
    """Шукає варіант довідника, що найкраще відповідає тексту з CRM."""
    if value is None or not options:
        return None
    v = _norm(value)
    for k, syn in CONDITION_SYNONYMS.items():
        if k in v:
            v = v + " " + syn
    best, best_len = None, 0
    for oid, label in options.items():
        lab = _norm(label)
        if not lab:
            continue
        if lab == v:
            return int(oid)
        # слово-ключ довідника входить у значення CRM або навпаки
        stem = lab.split("/")[0].strip()
        words_match = stem and set(stem.split()) <= set(v.split())   # «силікатна цегла» = «цегла силікатна»
        if (stem and stem in v) or words_match or (len(v) > 3 and v in lab):
            if len(stem) > best_len:
                best, best_len = int(oid), len(stem)
    if best is None:
        # у довідниках різних типів ті самі матеріали названі по-різному
        for a, b in WALL_ALIASES:
            if a in v:
                alt = match_option(v.replace(a, b), options) if b not in v else None
                if alt:
                    return alt
    return best


WALL_ALIASES = [("газоблок", "газобетон"), ("газобетон", "газоблок"),
                ("піноблок", "пінобетон"), ("пінобетон", "піноблок"),
                ("силікатна цегла", "цегла силікатна"), ("цегла силікатна", "силікатна цегла"),
                ("шлакоблок", "шлакобетон"), ("керамоблок", "керамічний блок")]


def parse_number(s):
    if s is None:
        return None
    m = re.search(r"\d[\d\s ]*(?:[.,]\d+)?", str(s))
    if not m:
        return None
    raw = m.group(0).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        f = float(raw)
        return int(f) if f.is_integer() else round(f, 2)
    except ValueError:
        return None


def parse_price(s):
    """'$ 365 000' / '365 000 $' / '12 000 грн/міс' -> (365000, '$')"""
    if not s:
        return None, None
    txt = str(s)
    digits = re.sub(r"[^\d]", "", txt.split("/")[0])
    amount = int(digits) if digits else None
    cur = "$"
    low = txt.lower()
    if "€" in txt or "eur" in low:
        cur = "€"
    elif "грн" in low or "₴" in txt or "uah" in low:
        cur = "грн"
    return amount, cur


def detect_deal(deal_title):
    """'Продаж / Квартира' -> (advert_type_id, realty_type_id)"""
    t = _norm(deal_title)
    op = 1
    if "подоб" in t or "посуточ" in t:
        op = 4
    elif "оренд" in t or "аренд" in t:
        op = 3
    obj = t.split("/", 1)[1] if "/" in t else t
    for rt, kws in TYPE_KEYWORDS:
        if any(k in obj for k in kws):
            return op, rt
    return op, None


def dict_for(rt, op):
    """Довідник для типу; якщо саме такого немає — найближчий схожий."""
    op_key = 3 if op in (3, 4) else 1
    candidates = [f"{rt}_{op_key}", f"{rt}_1"]
    if rt in (6, 7, 8, 9):
        candidates += [f"5_{op_key}", "5_1"]
    if rt in (12, 14, 15, 16, 17, 19, 20, 22):
        candidates += [f"21_{op_key}", "21_1"]
    if rt in (26, 27, 28, 29):
        candidates += ["25_1"]
    if rt in (31, 32, 33, 35):
        candidates += ["34_1"]
    for c in candidates:
        if c in CHARS and CHARS[c]:
            return c, CHARS[c]
    return None, {}


# Стан ремонту: значення CRM -> ID варіанта dom.ria (за правилами агенції).
# 516 — квартири/кімнати, 529 — будинки, 542 — комерція.
CRM_CONDITION = {
    "з ремонтом":  {516: 506,  529: 517, 542: 530},   # дизайнерський ремонт / дизайнерське оформлення
    "під чистову": {516: 512,  529: 523, 542: 536},   # чорнова штукатурка / будівельна обробка
    "без ремонту": {516: 513,  529: 524, 542: 537},   # без оздоблювальних робіт
}
CRM_CONDITION_ALIASES = {
    "с ремонтом": "з ремонтом", "под чистовую": "під чистову", "під чистове": "під чистову",
    "без ремонта": "без ремонту",
}


def crm_condition(text, char_id):
    if not text or not char_id:
        return None
    t = str(text).lower().replace("’", "'").strip()
    t = CRM_CONDITION_ALIASES.get(t, t)
    return CRM_CONDITION.get(t, {}).get(int(char_id))


def year_option(year, options):
    if not year or not options:
        return None
    y = int(year)
    for oid, label in options.items():
        lab = str(label)
        if lab.strip() == str(y):
            return int(oid)
        if lab.startswith("Здача в") and str(y) in lab:
            return int(oid)
        m = re.match(r"(\d{4})-(\d{4})", lab)
        if m and int(m.group(1)) <= y <= int(m.group(2)):
            return int(oid)
        if lab.startswith("до ") and y < parse_number(lab):
            return int(oid)
    return None


class Params:
    """Плаский пошук по quick_facts + parameters з CRM за ключовими словами."""

    def __init__(self, estate):
        self.items = []
        for src in (estate.get("parameters") or {}, estate.get("quick_facts") or {}):
            for k, v in src.items():
                self.items.append((_norm(k), k, v))
        self.used = set()

    def get(self, keywords, exclude=()):
        return self.get_item(keywords, exclude)[1]

    def get_item(self, keywords, exclude=()):
        """(назва поля в CRM, значення) першого параметра, що підходить."""
        for nk, k, v in self.items:
            if any(kw in nk for kw in keywords) and not any(ex in nk for ex in exclude):
                self.used.add(k)
                return k, v
        return None, None

    def unused(self):
        seen = set()
        out = []
        for _, k, v in self.items:
            if k not in self.used and k not in seen:
                seen.add(k)
                out.append((k, v))
        return out


def build_characteristics(estate, rt, op, config):
    """Повертає (characteristics_values, warnings, dict_key, невикористані параметри CRM,
    unmatched — [(id характеристики, текст з CRM)] для списків, де не знайшовся варіант)."""
    key, d = dict_for(rt, op)
    warnings = []
    unmatched = []
    if not d:
        return {}, [f"Немає довідника характеристик для типу {rt}"], None, [], []
    p = Params(estate)
    cv = {}

    def cid(sem):
        for c in SEM[sem]:
            if str(c) in d:
                return c
        return None

    def put_num(sem, value):
        c = cid(sem)
        if c and value is not None:
            cv[str(c)] = value

    def put_opt(sem, text):
        c = cid(sem)
        if not c or text is None:
            return
        oid = match_option(text, d[str(c)].get("o"))
        if oid:
            cv[str(c)] = oid
        else:
            warnings.append(f"«{d[str(c)]['l']}»: не знайшов варіант для «{text}»")
            unmatched.append((str(c), str(text)))

    def opt_by_label(sem, label):
        c = cid(sem)
        if not c:
            return None, None
        for oid, lab in (d[str(c)].get("o") or {}).items():
            if lab == label:
                return c, int(oid)
        return c, None

    # --- площі, кімнати, поверхи
    put_num("total_area", parse_number(p.get(["загальна площа", "общая площадь", "площа заг", "площадь общ"])
                                       or p.get(["площа", "площадь"], exclude=["житл", "жил", "кухн", "ділянк", "участ", "корисн", "полезн"])))
    put_num("living_area", parse_number(p.get(["житлова", "жилая"])))
    put_num("kitchen_area", parse_number(p.get(["кухн"], exclude=["тип"])))
    put_num("useful_area", parse_number(p.get(["корисна", "полезная"])))
    rooms = parse_number(p.get(["кімнат", "комнат", "кількість приміщень", "количество помещений"],
                               exclude=["спал"]))
    put_num("rooms", rooms)
    put_num("bedrooms", parse_number(p.get(["спал"])))

    floor_raw = p.get(["поверх", "этаж"], exclude=["поверховість", "этажность", "поверхів", "этажей"])
    floors_raw = p.get(["поверховість", "этажность", "поверхів", "этажей", "кількість поверхів"])
    if floor_raw and re.search(r"\d+\s*/\s*\d+", str(floor_raw)):
        a, b = re.search(r"(\d+)\s*/\s*(\d+)", str(floor_raw)).groups()
        floor_raw, floors_raw = a, floors_raw or b
    if cid("floor"):
        put_num("floor", parse_number(floor_raw))
    put_num("floors", parse_number(floors_raw))

    # --- ділянка
    plot_key, plot_raw = p.get_item(["ділянк", "участ", "земл"], exclude=["призначен", "назначен", "кадастр"])
    if plot_raw is None and cid("plot_area") == 1917:
        # земля: у CRM площа ділянки — це «Площа загальна, соток»
        plot_key, plot_raw = p.get_item(["площа загальна", "общая площадь", "площа"],
                                        exclude=["житл", "жил", "кухн", "корисн", "полезн"])
    if plot_raw is not None and cid("plot_area"):
        val = parse_number(plot_raw)
        unit_label = "сотка"
        low = _norm(f"{plot_key} {plot_raw}")          # одиниця — з назви поля або значення
        if re.search(r"(?<![а-яіїє])га(?![а-яіїє])", low) or "гект" in low:
            unit_label = "гектар"
        elif "м2" in low or "м²" in low or "кв.м" in low or "кв. м" in low:
            if "сот" not in low:
                unit_label = "кв. м"
        put_num("plot_area", val)
        c, oid = opt_by_label("plot_unit", unit_label)
        if c and oid:
            cv[str(c)] = oid
    elif cid("plot_unit"):
        c, oid = opt_by_label("plot_unit", "сотка")
        cv[str(c)] = oid  # обов'язкове поле

    # --- будинок / стан
    put_opt("walls", p.get(["стін", "стен", "матеріал", "материал"], exclude=["покрівл", "кровл", "дах"]))
    c_w = cid("walls")
    if c_w and str(c_w) not in cv and config.get("default_walls"):
        oid = match_option(config["default_walls"], d[str(c_w)].get("o"))
        if oid:
            cv[str(c_w)] = oid
            warnings[:] = [w for w in warnings if "«" + d[str(c_w)]["l"] + "»" not in w]
            unmatched[:] = [u for u in unmatched if u[0] != str(c_w)]
    year = parse_number(p.get(["рік", "год постр", "год сдачи", "рік здачі"]))
    if year and cid("year"):
        oid = year_option(year, d[str(cid("year"))].get("o"))
        if oid:
            cv[str(cid("year"))] = oid
    ceil = parse_number(p.get(["стел", "потол"]))
    if ceil:
        put_num("ceiling", ceil / 100 if ceil > 20 else ceil)
    cond_txt = p.get(["ремонт", "стан", "состояние"])
    c_cond = cid("condition")
    fixed = crm_condition(cond_txt, c_cond)
    if fixed:
        cv[str(c_cond)] = fixed
    else:
        put_opt("condition", cond_txt)
    furn = p.get(["мебл", "мебел"])
    if furn is not None and cid("furniture"):
        no = any(w in _norm(furn) for w in ["без", "ні", "нет", "немає"])
        c, oid = opt_by_label("furniture", "немає" if no else "є")
        if oid:
            cv[str(c)] = oid
    cad = p.get(["кадастр"])
    if cad and cid("cadastral"):
        cv[str(cid("cadastral"))] = str(cad).strip()

    # --- обов'язкові для комерції/ділянок/гаражів значення за замовчуванням
    if cid("object_type"):
        txt = p.get(["тип об", "тип обь", "тип приміщ", "тип помещ"])
        oid = match_option(txt, d[str(cid("object_type"))].get("o")) if txt else None
        cv[str(cid("object_type"))] = oid or config.get("default_commercial_object_type", 1974)
    if cid("land_purpose"):
        txt = p.get(["призначен", "назначен"])
        oid = match_option(txt, d[str(cid("land_purpose"))].get("o")) if txt else None
        cv[str(cid("land_purpose"))] = oid or (2084 if rt == 26 else 2083)
    if cid("garage_purpose"):
        cv[str(cid("garage_purpose"))] = 173
        if "211" in d and "211" not in cv:
            cv["211"] = 1

    # --- ціна
    amount, cur = parse_price(estate.get("price"))
    if amount:
        put_num("price", amount)
        c, oid = opt_by_label("currency", cur)
        if c and oid:
            cv[str(c)] = oid
        pf = cid("price_for")
        if pf:
            want = "за ділянку" if pf == 251 else "за об'єкт"
            c, oid = opt_by_label("price_for", want)
            if oid:
                cv[str(c)] = oid
    else:
        warnings.append("Не знайшов ціну в CRM")

    # --- тип пропозиції та комісія
    c, oid = opt_by_label("offer_type", config.get("offer_type_label", "від посередника"))
    if c and oid:
        cv[str(c)] = oid
    if config.get("commission") is not None and cid("commission"):
        cv[str(cid("commission"))] = config["commission"]

    for t in config.get("default_tags", []):
        cv[str(t)] = int(t)

    # --- перевірка обов'язкових
    for c, meta in d.items():
        if meta.get("r") and c not in cv:
            warnings.append(f"Обов'язкове поле не заповнено: {c} «{meta['l']}»")

    leftovers = [f"{k}: {v}" for k, v in p.unused()]
    return cv, warnings, key, leftovers, unmatched


def describe(cv, dict_key):
    """Людський вигляд characteristics_values для перевірки."""
    d = CHARS.get(dict_key, {})
    out = []
    for c, v in cv.items():
        meta = d.get(c, {})
        label = meta.get("l", c)
        if meta.get("o"):
            v = f"{meta['o'].get(str(v), v)} ({v})"
        out.append(f"  {c:>5} {label}: {v}")
    return "\n".join(out)
