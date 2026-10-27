"""
Повний конвеєр: посилання-презентація CRM -> готовий (майже) payload
для POST https://rieltor.ua/api/offers/

Поки НЕ включає завантаження фото (rentaImages1 залишається порожнім) —
це наступний окремий крок, бо фото вимагають окремих POST-запитів.

Використання:
    python3 crm_to_rieltor_payload.py "<посилання CRM>" "<COOKIE rieltor.ua>"
"""

import sys
import json
import re
import time
import requests

from extract_estate import fetch_estate_data
from rieltor_geo import RieltorGeoClient, resolve_address
from rieltor_photos import upload_photos, RateLimitExhausted

# --- довідники, зняті з get-common-data (див. rieltor_reference.json) ---

ITEM_TYPE_BY_DEAL = {
    "будинок": 3, "дом": 3,
    "приватний будинок": 3, "частный дом": 3,
    "дуплекс": 3,
    "таунхаус": 3,
    "котедж": 3, "коттедж": 3,
    "вілла": 3, "вилла": 3,
    "дача": 3,
    "частина будинку": 3, "часть дома": 3,
    "модульний будинок": 3, "модульный дом": 3,
    "квартира": 1,
    "кімната": 2, "комната": 2,
    "офіс": 4, "офис": 4,
    "торгівельна площа": 4, "торговая площадь": 4,
    "складське приміщення": 4, "складское помещение": 4, "склад": 4,
    "земля": 5, "земельна ділянка": 5, "земельный участок": 5,
    "паркомісце": 6, "парковочное место": 6, "паркоместо": 6,
}
OPER_TYPE_BY_DEAL = {"продаж": 1, "продажа": 1, "оренда": 2, "аренда": 2}

# Запасний варіант, якщо точного збігу в ITEM_TYPE_BY_DEAL нема (CRM формулює тип
# по-різному: «Земля під інд. будівництво», «С/г ділянка», «Присадибна ділянка» тощо) —
# розпізнаємо за ключовим словом. Порядок важливий: земля/ділянка — перші, щоб не
# сплутати з будинком («ділянка під забудову будинку» тощо).
ITEM_TYPE_KEYWORDS = [
    (5, ["земл", "ділянк", "участ"]),
    (3, ["будин", "дом", "дуплекс", "таунхаус", "котедж", "коттедж", "вілл", "вилл", "дач"]),
    (4, ["офіс", "офис", "торгов", "торгів", "склад"]),
    (1, ["квартир"]),
    (2, ["кімнат", "комнат"]),
    (6, ["паркоміс", "парковоч", "паркомест"]),
]


def guess_item_type(label: str):
    for item_type, kws in ITEM_TYPE_KEYWORDS:
        if any(kw in label for kw in kws):
            return item_type
    return None

# Конкретний підтип будинку (houseType) за назвою з CRM — якщо CRM дає
# щось конкретніше за узагальнене "Будинок", підставляємо точний код
# замість дефолтного "Приватний будинок".
HOUSE_TYPE_BY_LABEL = {
    "будинок": 1, "дом": 1,
    "приватний будинок": 1, "частный дом": 1,
    "дача": 4,
    "частина будинку": 5, "часть дома": 5,
    "таунхаус": 6,
    "дуплекс": 7,
    "котедж": 1, "коттедж": 1,  # котедж не має окремого коду в довіднику -> звичайний будинок
    "вілла": 1, "вилла": 1,     # так само
    "модульний будинок": 14, "модульный дом": 14,
}

COMMON_STATE_MAP = {
    "з ремонтом": 3,
    "частковий ремонт": 2,
    "без ремонту": 1,
    "без ремонта": 1,
    "під чистову": 1,
}

HOUSE_TYPE_DEFAULT = 1  # "Приватний будинок" — дефолт, якщо CRM не каже інакше

# Стара частина об'єктів у CRM досі містить російськомовний текст (лишився
# з часів, коли інтерфейс CRM був російським) — таблиця параметрів і назви
# районів можуть прийти в будь-якій з двох мов, тому пробуємо обидва варіанти.
DISTRICT_RU_TO_UK = {
    "голосеевский": "Голосіївський", "печерский": "Печерський",
    "шевченковский": "Шевченківський", "дарницкий": "Дарницький",
    "соломенский": "Солом'янський", "подольский": "Подільський",
    "днепровский": "Дніпровський", "оболонский": "Оболонський",
    "святошинский": "Святошинський", "деснянский": "Деснянський",
}


def normalize_district_name(name: str | None) -> str | None:
    if not name:
        return name
    return DISTRICT_RU_TO_UK.get(name.strip().lower(), name)


def get_param(params: dict, *keys):
    """Повертає перше знайдене значення серед можливих варіантів ключа
    (укр./рос. назва колонки в таблиці параметрів CRM)."""
    for k in keys:
        if params.get(k):
            return params[k]
    return None


PHOTO_FIELD_BY_ITEM_TYPE = {
    1: "rentaImages1",  # квартира
    2: "rentaImages1",  # кімната (припущення за аналогією, не перевірено)
    3: "rentaImages1",  # будинок — підтверджено
    4: "rentaImages",   # комерція — підтверджено, БЕЗ "1"
    5: "rentaImages",   # ділянка (припущення, не перевірено)
    6: "rentaImages",   # паркомісце (припущення, не перевірено)
}
DESCRIPTION_FIELD_BY_ITEM_TYPE = {
    1: "description1",  # квартира
    2: "description1",  # кімната (припущення за аналогією, не перевірено)
    3: "description1",  # будинок — підтверджено
    4: "description",   # комерція — підтверджено, БЕЗ "1"
    5: "description",   # ділянка (припущення, не перевірено)
    6: "description",   # паркомісце (припущення, не перевірено)
}


def parse_deal_title(deal_title: str):
    """'Оренда / Будинок' -> (operType=2, itemType=3)
    Ріжемо лише по ПЕРШОМУ '/' — сама назва типу іноді теж містить слеш
    (напр. 'Продаж / С/г ділянка')."""
    parts = [p.strip().lower() for p in deal_title.split("/", 1)]
    oper = OPER_TYPE_BY_DEAL.get(parts[0])
    item = None
    if len(parts) > 1:
        label = parts[1]
        item = ITEM_TYPE_BY_DEAL.get(label)
        if item is None:
            item = guess_item_type(label)
    return oper, item


def parse_address(address: str) -> dict:
    """
    Евристичний парсер адреси CRM. Формат непостійний, тому це best-effort:
    - "Київ, Печерський район, вул. Кловський узвіз, 7а" -> city=Київ
    - "Вишгородський район, Вишгород, вул. Шевченка"      -> district-first
    Повертає dict(city, district, street, house) — будь-яке поле може бути None.
    """
    parts = [p.strip() for p in address.split(",")]
    result = {"city": None, "district": None, "street": None, "house": None}
    if not parts:
        return result

    if parts[0].strip().lower() in ("київ", "киев"):
        result["city"] = "Київ"
        rest = parts[1:]
        if rest and "район" in rest[0]:
            result["district"] = rest[0].replace("район", "").strip()
            rest = rest[1:]
        if rest:
            result["street"] = rest[0]
        if len(rest) > 1:
            result["house"] = rest[1]
    else:
        # перший сегмент зазвичай "<Район> район"
        if parts and "район" in parts[0]:
            result["district"] = parts[0].replace("район", "").strip()
            parts = parts[1:]
        if parts:
            result["city"] = parts[0]
            parts = parts[1:]
        if parts:
            result["street"] = parts[0]
        if len(parts) > 1:
            result["house"] = parts[1]

    return result


STREET_PREFIXES = re.compile(
    r"^(вул\.|вулиця|ул\.|улица|просп\.|проспект|пров\.|провулок|пер\.|переулок|"
    r"пл\.|площа|площадь|бульв\.|б-р|бульвар|наб\.|набережна|набережная|шосе|шоссе|тупик)\s+",
    re.IGNORECASE,
)


def strip_street_prefix(street: str) -> str:
    """'вул. Кичеївська' -> 'Кичеївська' — так само, як сайт зберігає вільний текст вулиці."""
    if not street:
        return street
    return STREET_PREFIXES.sub("", street).strip()


def extract_house_number(street_or_house: str):
    """Витягує лише число з можливого '7а' -> '7'. Повертає як є, якщо номера нема."""
    if not street_or_house:
        return None
    street_or_house = str(street_or_house)
    m = re.search(r"\d+", street_or_house)
    return m.group(0) if m else street_or_house


GROUND_AREA_SOTKA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*сот(?:ок|ки|ка|их)?", re.IGNORECASE)
GROUND_AREA_HA_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:га\b|гектар\w*)", re.IGNORECASE)
EXPANSION_TRIGGER_RE = re.compile(
    r"(збільш\w*|розшир\w*|можна\s+(?:довести|збільшити|розширити)|"
    r"у\s+перспективі|потенційно|можливість\s+(?:збільшити|розширити))",
    re.IGNORECASE,
)

# Кадастровий номер ділянки: 3220882200:03:005:1306 (10:2:3:4 цифри) —
# підтверджено реальним запитом rieltor.ua (cadastralNumber у /api/offers/).
CADASTRAL_RE = re.compile(r"\b\d{10}:\d{2}:\d{3}:\d{4}\b")


def find_cadastral_in_text(text: str):
    """Шукає кадастровий номер ділянки у вільному тексті опису CRM."""
    m = CADASTRAL_RE.search(text or "")
    return m.group(0) if m else None


def extract_ground_area_from_text(text: str, context_window: int = 60):
    """
    Шукає площу ділянки в тексті опису: 'N соток' або 'N га' (га -> ×100 соток).
    Ігнорує згадки в контексті на кшталт 'можна збільшити до N соток' —
    це потенціал, а не фактичний розмір. З решти кандидатів бере МАКСИМУМ
    (загальна площа завжди більша за будь-яку її частину).
    """
    if not text:
        return None
    candidates = []
    for m in GROUND_AREA_SOTKA_RE.finditer(text):
        context = text[max(0, m.start() - context_window):m.start()]
        if EXPANSION_TRIGGER_RE.search(context):
            continue
        candidates.append(float(m.group(1).replace(",", ".")))
    for m in GROUND_AREA_HA_RE.finditer(text):
        context = text[max(0, m.start() - context_window):m.start()]
        if EXPANSION_TRIGGER_RE.search(context):
            continue
        candidates.append(float(m.group(1).replace(",", ".")) * 100)
    return max(candidates) if candidates else None


def to_number_str(value):
    """'100' -> '100'; прибирає можливі пробіли/коми з чисел параметрів CRM."""
    if value is None:
        return None
    return str(value).replace(" ", "").replace(",", ".")


def fetch_min_rate(cookie: str, city_id: int, item_type: int, oper_type: int) -> int:
    """
    Питає в rieltor.ua мінімальну дозволену ставку (monRentaRate) для
    конкретного поєднання місто+тип об'єкта+тип угоди — той самий запит,
    який сайт сам робить для ЩЕ НЕ створеного оголошення (id=-1).
    Для будинків/комерції/землі це зазвичай 0, а для квартир/кімнат —
    ненульове число (напр. 179).
    """
    if not city_id:
        return 0
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Cookie": cookie,
    }
    try:
        resp = requests.get(
            "https://rieltor.ua/api/offers/pickup-item/-1/",
            params={"id": -1, "place": 0, "cityId": city_id, "itemType": item_type, "operType": oper_type},
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "OK":
            return 0
        return int(payload["data"].get("params", {}).get("minRentaRate", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0  # якщо не вдалось дізнатись — безпечніше 0, ніж вигадане число


# ---------------------------------------------------------------- квартири (itemType=1)
ROOM_WORDS = {"одно": 1, "дво": 2, "двох": 2, "три": 3, "трьох": 3, "чотири": 4, "чотирьох": 4,
              "п'яти": 5, "пʼяти": 5, "пяти": 5, "шести": 6, "шістьох": 6}


def find_rooms_in_text(text: str):
    """'3 кімнати', '2-кімнатна', 'двокімнатна' -> число кімнат або None."""
    t = (text or "").lower().replace("’", "'")
    m = re.search(r"(\d+)\s*[-–]?\s*(?:кімн|комн|к\.?\s*кв)", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(" + "|".join(ROOM_WORDS) + r")\s*[-–]?\s*(?:кімн|комн)", t)
    return ROOM_WORDS[m.group(1)] if m else None


def find_floor_in_text(text: str):
    """Повертає (поверх, поверховість) з тексту: '24 поверх', '5 з 9 поверх', '14/24 пов.'."""
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


def _fmt_area(x):
    x = float(x)
    return str(int(round(x))) if abs(x - round(x)) < 0.05 else str(round(x, 1))


def _split_pair(val):
    """'14 / 24' -> (14, 24); '14' -> (14, None)."""
    m = re.match(r"\s*(\d+)\s*(?:/\s*(\d+))?", str(val or ""))
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else None) if m else (None, None)


def fill_flat(payload: dict, crm: dict, params: dict, geo: dict) -> list:
    """Додає в payload поля, потрібні для квартири (itemType=1). Повертає список того, що
    НЕ вдалось визначити: [{"key", "label", "kind": "int"|"area"|"choice", "options"}].
    Порядок джерел: CRM-параметри -> дані rieltor.ua про будинок (для ЖК/будинку) -> текст опису
    -> (площі) 40%/20% від загальної. Що лишилось — питаємо користувача."""
    missing = []
    qf = crm.get("quick_facts", {}) or {}
    desc = crm.get("description", "") or ""
    hints = geo.get("server_house_hints") or {}

    # --- площі
    common = parse_area(get_param(params, "Площа загальна,м²", "Площадь общая,м²")) \
        or parse_area(str(qf.get("Площа", "")).split("/")[0])
    if common:
        payload["commonArea"] = _fmt_area(common)
        useful = parse_area(get_param(params, "Площа житлова,м²", "Площадь жилая,м²"))
        kitchen = parse_area(get_param(params, "Площа кухні,м²", "Площадь кухни,м²"))
        payload["usefulArea"] = _fmt_area(useful if useful else common * 0.40)
        payload["kitchenArea"] = _fmt_area(kitchen if kitchen else common * 0.20)
        # rieltor.ua: загальна не може бути меншою за суму житлової і кухні
        if float(payload["usefulArea"]) + float(payload["kitchenArea"]) > float(payload["commonArea"]):
            missing.append({"key": "areas", "kind": "areas",
                            "label": f"Площі: у CRM загальна {payload['commonArea']}, житлова "
                                     f"{payload['usefulArea']}, кухня {payload['kitchenArea']} — сума більша за загальну"})
    else:
        missing.append({"key": "commonArea", "label": "Загальна площа, м²", "kind": "area"})

    # --- кімнати
    rooms = get_param(params, "Кіл. кімнат", "Кол. комнат") or qf.get("Кіл. кімнат")
    rooms = int(re.sub(r"\D", "", str(rooms))) if rooms and re.search(r"\d", str(rooms)) else None
    if not rooms:
        rooms = find_rooms_in_text(crm.get("deal_title", "") + "\n" + desc)
    if rooms:
        payload["roomCnt"] = min(int(rooms), 6)   # rieltor.ua: максимум 6 («6 і більше»)
    else:
        missing.append({"key": "roomCnt", "label": "Кількість кімнат", "kind": "int"})

    # --- поверх і поверховість
    floor, floors = _split_pair(get_param(params, "Поверх", "Этаж") or qf.get("Поверх"))
    f2 = get_param(params, "Поверховість", "Этажность")
    if f2:
        floors = _split_pair(f2)[0] or floors
    if not floor or not floors:
        t_floor, t_floors = find_floor_in_text(desc)
        floor = floor or t_floor
        floors = floors or (hints.get("floorCnt") and int(hints["floorCnt"])) or t_floors
    if floor:
        payload["floor"] = str(floor)
    else:
        missing.append({"key": "floor", "label": "Поверх", "kind": "int"})
    if floors:
        payload["floorCnt"] = str(floors)
    else:
        missing.append({"key": "floorCnt", "label": "Поверховість будинку", "kind": "int"})

    # --- стан (ремонт)
    state = COMMON_STATE_MAP.get((get_param(params, "Ремонт") or "").strip().lower())
    if state:
        payload["commonState"] = state
    else:
        missing.append({"key": "commonState", "label": "Стан (ремонт)", "kind": "choice",
                        "options": [(1, "Без ремонту"), (2, "Частковий ремонт"), (3, "З ремонтом")]})

    # --- дані про будинок від самого rieltor.ua (підтягуються формою після вибору ЖК/будинку)
    if hints.get("wallType") is not None:
        payload["wallType"] = hints["wallType"]
    if hints.get("houseProjectType") is not None:
        payload["houseProjectType"] = hints["houseProjectType"]
    detail = {k: hints[k] for k in ("year", "ceilHeight") if hints.get(k) is not None}
    if detail:
        payload["detailParams"] = {"year": detail.get("year"), "ceilHeight": detail.get("ceilHeight")}

    # --- район: у Києві/містах з районами потрібен districtId
    if geo.get("has_districts") and not payload.get("districtId"):
        missing.append({"key": "districtId", "label": "Район", "kind": "district",
                        "options": geo.get("district_candidates") or []})
    return missing


def fill_other(payload: dict, item_type: int, geo: dict) -> list:
    """Будинок / комерція / ділянка / інше: що з потрібного лишилось порожнім після розбору CRM —
    повертаємо списком, щоб при одиночній публікації бот спитав користувача.
    Формат як у fill_flat: [{"key", "label", "kind": "int"|"area"|"choice"|"district", ...}]."""
    missing = []

    def empty(k):
        v = payload.get(k)
        return v in (None, "", 0, "0")

    if empty("price"):
        missing.append({"key": "price", "label": "Ціна", "kind": "int"})
    if empty("commonArea"):
        missing.append({"key": "commonArea", "label": "Загальна площа" + (", соток" if item_type == 5 else ", м²"),
                        "kind": "area"})
    if item_type == 3:      # будинок
        if empty("floorCnt"):
            missing.append({"key": "floorCnt", "label": "Поверховість будинку", "kind": "int"})
        if empty("groundArea"):
            missing.append({"key": "groundArea", "label": "Площа ділянки, соток", "kind": "area"})
        if empty("roomCnt"):
            missing.append({"key": "roomCnt", "label": "Кількість кімнат", "kind": "int"})
        if empty("commonState"):
            missing.append({"key": "commonState", "label": "Стан (ремонт)", "kind": "choice",
                            "options": [(1, "Без ремонту"), (2, "Частковий ремонт"), (3, "З ремонтом")]})
    elif item_type == 4:    # комерція
        if empty("floorCnt"):
            missing.append({"key": "floorCnt", "label": "Поверховість будинку", "kind": "int"})
        if not (payload.get("detailParams") or {}).get("floor"):
            missing.append({"key": "floor", "label": "Поверх", "kind": "int"})
    if geo.get("has_districts") and not payload.get("districtId"):
        missing.append({"key": "districtId", "label": "Район", "kind": "district",
                        "options": geo.get("district_candidates") or []})
    return missing


def parse_area(v):
    if v is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(v).replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def build_payload(crm: dict, geo_client: RieltorGeoClient | None, cookie: str | None = None,
                   upload_photos_flag: bool = False, item_type_override: int | None = None,
                   address_override: dict | None = None) -> dict:
    oper_type, item_type = parse_deal_title(crm["deal_title"])
    crm_type_label = crm["deal_title"].split("/", 1)[1].strip().lower() if "/" in crm["deal_title"] else ""
    unknown_crm_type = False
    if item_type_override is not None and item_type_override != item_type:
        print(f"  Категорія в CRM-адмінці не збігається з заголовком ('{crm['deal_title']}') — "
              f"використовую категорію з CRM.")
        item_type = item_type_override
    if item_type is None:
        item_type = 4  # Комерційна — розумний дефолт для нерозпізнаних типів CRM
        unknown_crm_type = True
        print(f"  УВАГА: нерозпізнаний тип об'єкта в CRM ('{crm['deal_title']}') — "
              f"використано типове значення 'Комерційна, вільне призначення'.")
    params = crm.get("parameters", {})

    price = re.sub(r"[^\d]", "", crm.get("price", "") or "")
    common_area_raw = get_param(params, "Площа загальна,м²", "Площадь общая,м²") or crm.get("quick_facts", {}).get("Площа")
    common_area = to_number_str(common_area_raw)
    photo_field = PHOTO_FIELD_BY_ITEM_TYPE.get(item_type, "rentaImages1")
    description_field = DESCRIPTION_FIELD_BY_ITEM_TYPE.get(item_type, "description1")

    payload = {
        "operType": oper_type,
        "itemType": item_type,
        "currencyMagic": 2,  # $ — за замовчуванням, бо в CRM усі ціни в доларах
        "price": price,
        "commonArea": common_area,
        description_field: crm.get("description", ""),
        photo_field: [],  # заповнюється нижче, якщо upload_photos_flag=True
        "status": 10,
        "commissionRate": "5" if oper_type == 1 else "50",  # Продаж -> 5%, Оренда -> 50%
        "commissionType": 1,
        "withoutCommission": 0,
        "isExclusive": 0,
        "exclusiveFiles": [],
        "subways": [],  # бекенд вимагає цей ключ завжди, навіть порожнім
        "orients": [],
        "pickupRate": 0, "pickupAutoupdate": 0, "pickupAutoupdateTime": "09:00",
        "pickupBirdPremium": 0, "pickupLunTop": 0, "pickupQuickly": 0,
    }

    # --- специфіка типу об'єкта ---
    if item_type == 3:  # будинок
        floor_cnt = to_number_str(get_param(params, "Поверховість", "Этажность"))
        ground_area = to_number_str(get_param(params, "Площа ділянки,соток", "Площадь участка,соток"))
        if not ground_area:
            if oper_type == 2:  # Оренда — можна обережно вгадати з опису
                fallback = extract_ground_area_from_text(crm.get("description", ""))
                if fallback:
                    ground_area = str(int(fallback)) if fallback == int(fallback) else str(fallback)
                    print(f"  (groundArea відсутній у CRM-параметрах, взято з опису: {ground_area} соток)")
                else:
                    print("  УВАГА: groundArea не знайдено ані в параметрах, ані в описі — публікація, ймовірно, впаде з помилкою.")
            else:  # Продаж — тут часто трапляються оманливі формулювання ("можна збільшити до"),
                   # тому автоматично не вгадуємо, лише попереджаємо
                print("  УВАГА: groundArea відсутній у CRM-параметрах (категорія Продаж — автоматичне")
                print("  вгадування з опису вимкнено навмисно, впиши число вручну перед публікацією).")
        room_cnt = get_param(params, "Кіл. кімнат", "Кол. комнат") or crm.get("quick_facts", {}).get("Кіл. кімнат")
        remont_text = (get_param(params, "Ремонт") or "").lower()

        common_area_num = float(common_area) if common_area else 0
        payload.update({
            "floorCnt": floor_cnt,
            "groundArea": ground_area,
            "roomCnt": min(int(room_cnt), 6) if room_cnt else None,  # rieltor.ua приймає максимум 6 ("6 і більше")
            "commonState": COMMON_STATE_MAP.get(remont_text),
            "houseType": HOUSE_TYPE_BY_LABEL.get(crm_type_label, HOUSE_TYPE_DEFAULT),
            "usefulArea": str(round(common_area_num * 0.70)) if common_area_num else None,
            "kitchenArea": str(round(common_area_num * 0.15)) if common_area_num else None,
            "detailParams": {"year": None, "ceilHeight": None},
        })

    elif item_type == 5:  # ділянка
        cadastral = find_cadastral_in_text(crm.get("description", ""))
        if cadastral:
            payload["cadastralNumber"] = cadastral

    elif item_type == 4:  # комерція
        aim_text = (crm.get("deal_title") or "").lower()
        if unknown_crm_type:
            aim = 32  # "Приміщення вільного призначення" — коли CRM не дав розпізнаваного типу
        elif "торгів" in aim_text or "торгов" in aim_text:
            aim = 64
        elif "склад" in aim_text:
            aim = 8
        else:
            aim = 2  # дефолт "Офісне приміщення"
        floor = get_param(params, "Поверх", "Этаж")
        floor_cnt = get_param(params, "Поверховість", "Этажность")
        payload.update({
            "buildingKind": 1,
            "aim": [aim],
            "floorCnt": int(floor_cnt) if floor_cnt else None,
            "detailParams": {
                "floor": [int(floor)] if floor else [],
                "floorCnt": int(floor_cnt) if floor_cnt else None,
            },
        })

    # --- адреса ---
    addr = parse_address(crm.get("address", ""))
    geo = {"regionId": None, "cityId": None, "districtId": None, "streetId": None,
           "houseId": None, "newhouseId": None, "ptolemyHouseId": None, "matched": False}
    ov = address_override or {}
    if ov.get("street"):
        addr["street"] = ov["street"]
    if ov.get("house"):
        addr["house"] = ov["house"]
    if geo_client and addr["city"]:
        complex_name = params.get("Комплекс")
        if complex_name and "не жк" in complex_name.lower():
            complex_name = None
        if ov:                    # адресу виправлено вручну — ЖК з CRM більше не довіряємо
            complex_name = None
        geo = resolve_address(
            geo_client,
            city_query=addr["city"],
            street_query=addr["street"],
            house_query=extract_house_number(addr["house"]) if addr["house"] else None,
            district_query=normalize_district_name(addr["district"]),
            complex_query=complex_name,
            fix_district=(item_type == 1),
            district_id_override=ov.get("district_id"),
        )

    if geo.get("matched"):
        payload.update({
            "regionId": geo["regionId"], "cityId": geo["cityId"],
            "districtId": geo["districtId"], "streetId": geo["streetId"],
            "houseId": geo["houseId"], "newhouseId": geo["newhouseId"],
            "ptolemyHouseId": geo["ptolemyHouseId"],
            "streetNameUnfound": None, "houseNameUnfound": None, "osmId": None,
            "latitude": geo["latitude"] or crm.get("lat"),
            "longitude": geo["longitude"] or crm.get("lng"),
        })
    else:
        # адресу не знайдено в довіднику rieltor.ua -> вільний текст + координати CRM
        payload.update({
            "regionId": geo.get("regionId") or 111,  # 111 = Київська обл., дефолт
            "cityId": geo.get("cityId"), "districtId": geo.get("districtId"),
            "streetId": None, "houseId": None, "newhouseId": None, "ptolemyHouseId": None,
            "streetNameUnfound": strip_street_prefix(addr["street"]),
            "houseNameUnfound": addr["house"] or "",
            "osmId": None,
            "latitude": geo.get("latitude") or crm.get("lat"),
            "longitude": geo.get("longitude") or crm.get("lng"),
        })

    if item_type == 1:
        payload["_missing"] = fill_flat(payload, crm, params, geo)
    else:
        payload["_missing"] = fill_other(payload, item_type, geo)

    payload["_geo_debug"] = geo  # діагностика, прибрати перед реальною відправкою
    payload["_parsed_address"] = addr

    # --- мінімальна дозволена ставка для цієї категорії/міста (замість завжди 0) ---
    if cookie and payload.get("cityId"):
        min_rate = fetch_min_rate(cookie, payload["cityId"], item_type, oper_type)
        payload["pickupRate"] = min_rate
        if min_rate:
            print(f"  Мінімальна ставка для цієї категорії в цьому місті: {min_rate} — виставляю її.")

    # --- код об'єкта в кінці опису (якщо ще не згаданий десь у тексті) ---
    # порівнюємо без розділових знаків, бо в тексті код іноді пишуть по-іншому (A7.752 замість A7752)
    article_code = (crm.get("article") or "").lstrip("#").strip()
    normalized_article = re.sub(r"[^A-Za-zА-Яа-яІіЇїЄє0-9]", "", article_code)
    normalized_description = re.sub(r"[^A-Za-zА-Яа-яІіЇїЄє0-9]", "", payload[description_field])
    if article_code and normalized_article and normalized_article not in normalized_description:
        payload[description_field] = payload[description_field].rstrip() + "\n\n\n" + article_code

    if upload_photos_flag and cookie and crm.get("photos"):
        print(f"Завантажую {len(crm['photos'])} фото на rieltor.ua...")
        image_ids = upload_photos(crm["photos"], cookie)
        payload[photo_field] = image_ids

    return payload


def submit_offer(payload: dict, cookie: str) -> requests.Response:
    """Реальна публікація: POST payload на /api/offers/. Прибирає діагностичні поля."""
    clean_payload = {k: v for k, v in payload.items() if not k.startswith("_")}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/json",
        "Cookie": cookie,
    }
    return requests.post("https://rieltor.ua/api/offers/", headers=headers, json=clean_payload, timeout=90)


def process_source(source: str, cookie: str, do_upload: bool = True, do_publish: bool = True) -> None:
    """Обробляє одне посилання/файл цілком: CRM -> payload -> (публікація)."""
    from extract_estate import extract_estate_data

    if source.startswith("http://") or source.startswith("https://"):
        crm_data = fetch_estate_data(source)
    else:
        with open(source, encoding="utf-8") as f:
            crm_data = extract_estate_data(f.read())

    MIN_PHOTOS_REQUIRED = 3
    photos = crm_data.get("photos", [])
    if 0 < len(photos) < MIN_PHOTOS_REQUIRED:
        missing = MIN_PHOTOS_REQUIRED - len(photos)
        print(f"У CRM лише {len(photos)} фото (потрібно мінімум {MIN_PHOTOS_REQUIRED}) — "
              f"добираю до {MIN_PHOTOS_REQUIRED}, дублюючи останнє фото {missing} раз(и).")
        crm_data["photos"] = photos + [photos[-1]] * missing
    elif len(photos) == 0 and do_publish:
        print("=== ПУБЛІКАЦІЮ ЗУПИНЕНО ===")
        print("У CRM для цього об'єкта взагалі немає фото — дублювати нічого.")
        return

    client = RieltorGeoClient(cookie)
    payload = build_payload(crm_data, client, cookie=cookie, upload_photos_flag=do_upload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if do_publish:
        if payload.get("itemType") == 5 and payload.get("operType") == 1 and not payload.get("cadastralNumber"):
            print()
            print("=== ПУБЛІКАЦІЮ ЗУПИНЕНО ===")
            print("Продаж ділянки, а кадастрового номера немає в описі CRM — об'єкт пропущено.")
            return
        if payload.get("itemType") == 3 and payload.get("operType") == 1 and not payload.get("groundArea"):
            print()
            print("=== ПУБЛІКАЦІЮ ЗУПИНЕНО ===")
            print("Категорія Продаж/Будинок, а groundArea порожній — впиши число вручну")
            print("в payload (поле 'groundArea') і запусти публікацію окремо, або онови CRM.")
            return

        print()
        print("=== ПУБЛІКУЮ ОГОЛОШЕННЯ (POST /api/offers/) ===")
        try:
            resp = submit_offer(payload, cookie)
        except requests.exceptions.Timeout:
            print()
            print("=== СЕРВЕР НЕ ВІДПОВІВ ВЧАСНО (timeout) ===")
            print("Це не обов'язково провал: rieltor.ua іноді довго обробляє запит на")
            print("публікацію, і за нашим досвідом оголошення в такому випадку зазвичай")
            print("ВСЕ Ж СТВОРЮЄТЬСЯ на їхньому боці, попри те, що відповідь не прийшла.")
            print("Перевір кабінет rieltor.ua -> Оголошення, перш ніж пробувати ще раз —")
            print("щоб не опублікувати цей самий об'єкт двічі.")
            return
        print("Статус:", resp.status_code)
        print("Відповідь сервера:", resp.text[:2000])
        if resp.status_code == 200:
            print()
            print("Схоже, успішно опубліковано! Перевір у своєму кабінеті rieltor.ua -> Оголошення.")
        else:
            print()
            print("Сервер повернув помилку — скинь цей вивід мені, розберемось.")


if __name__ == "__main__":
    if "--interactive" in sys.argv:
        idx = sys.argv.index("--interactive")
        cookie = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else input("Встав cookie (один раз): ").strip()
        print("Готово! Встав одне посилання, кілька через кому, або шлях до .txt файлу зі списком (по одному на рядок).")
        print("Щоб вийти — напиши exit або просто натисни Enter на порожньому рядку.\n")
        while True:
            raw_input_line = input("Посилання/файл CRM (можна кілька через кому, або .txt зі списком): ").strip()
            if not raw_input_line or raw_input_line.lower() in ("exit", "quit"):
                print("Завершено.")
                break

            raw_input_line = raw_input_line.strip('"')  # якщо файл перетягнули мишкою у Windows

            if raw_input_line.lower().endswith(".txt"):
                try:
                    with open(raw_input_line, encoding="utf-8") as f:
                        sources = [line.strip() for line in f if line.strip()]
                except OSError as e:
                    print(f"Не вдалось прочитати файл {raw_input_line}: {e}")
                    continue
                print(f"Прочитано {len(sources)} посилань з файлу {raw_input_line}.")
            else:
                sources = [s.strip() for s in raw_input_line.split(",") if s.strip()]

            total = len(sources)
            results = {"ok": [], "fail": []}

            for i, source in enumerate(sources, start=1):
                print(f"\n{'=' * 60}")
                print(f"[{i}/{total}] {source}")
                print("=" * 60)
                try:
                    process_source(source, cookie, do_upload=True, do_publish=True)
                    results["ok"].append(source)
                except RateLimitExhausted as e:
                    print(f"\n=== ЗУПИНКА ВСЬОГО ПАКЕТУ ===\n{e}")
                    print(f"Оброблено {i - 1} з {total} до зупинки. Спробуй запустити список ще раз пізніше "
                          f"(можна лишити той самий .txt файл — уже опубліковані просто опублікуються повторно, "
                          f"якщо не видалиш їх зі списку).")
                    break
                except Exception as e:  # noqa: BLE001
                    import traceback
                    print(f"ПОМИЛКА: {e}")
                    traceback.print_exc()
                    results["fail"].append(source)

                if i < total:
                    time.sleep(5)  # невелика пауза між оголошеннями в пакеті

            if total > 1:
                print(f"\n{'#' * 60}")
                print(f"ПІДСУМОК: {len(results['ok'])} з {total} оброблено без винятків "
                      f"(не всі обов'язково опубліковані — перевір вивід кожного вище).")
                if results["fail"]:
                    print("З помилкою (Python-виняток):")
                    for s in results["fail"]:
                        print(f"  - {s}")
                print("#" * 60)

            print("\n" + "=" * 60 + "\n")
        sys.exit(0)

    if len(sys.argv) < 3:
        print("Використання: python3 crm_to_rieltor_payload.py <посилання CRM або .html файл> <COOKIE rieltor.ua> [--upload-photos] [--publish]")
        print("Або інтерактивний режим: python3 crm_to_rieltor_payload.py --interactive <COOKIE>")
        sys.exit(1)

    source, cookie = sys.argv[1], sys.argv[2]
    do_upload = "--upload-photos" in sys.argv[3:]
    do_publish = "--publish" in sys.argv[3:]
    if do_publish:
        do_upload = True  # без фото публікувати немає сенсу

    process_source(source, cookie, do_upload=do_upload, do_publish=do_publish)
