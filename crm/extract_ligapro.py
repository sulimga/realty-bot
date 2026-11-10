"""
Витягує дані об'єкта з публічної сторінки LigaPro (сайти агенцій на *.ligapro.ua,
напр. http://120064.ligapro.ua/kvartiry_prodaga/ann-3451458.html?agents_conts_id=…)
і віддає їх у ТОМУ Ж ФОРМАТІ, що й extract_estate.py (RealtSoft):
deal_title «Продаж / Квартира», address «Київ, Дарницький район, вул. Зарічна, 2»,
parameters з назвами як у RealtSoft («Площа загальна,м²», «Поверх», «Поверховість»…).
Тому програми сайтів (dom.ria, rieltor, OLX) працюють з LigaPro без змін.

Особливості сайту (перевірено 10.11.2026 на сайті АН, 6 типів об'єктів):
- лише http (https переадресовує назад на http), кодування windows-1251, без API — звичайний HTML;
- тип угоди й об'єкта — з розділу в адресі сторінки (/kvartiry_prodaga/, /doma_dachi_prodaga/ …),
  уточнення — рядок «Тип:» у таблиці (будинок/дуплекс/дача, торгівельні площі, паркінг…);
- адреса — «Зарічна вул., 2к.1, Київ, Дарницький, м. Славутич» (вулиця, будинок, місто, район,
  мікрорайон, метро; іноді з префіксом «Адреса:», без вулиці або без номера);
- «Площа: 72 / 32 / 25 м2» у квартирах = загальна / житлова / кухня;
- фото в повному розмірі — атрибут data-image у #gallery (назви мініатюр інколи інші);
- координати НЕ беремо (правило проєкту), estate_id НЕ ставимо — інакше програми сайтів підуть
  звіряти категорію в адмінку RealtSoft за чужим номером.

Використання:
    python3 extract_ligapro.py "http://120064.ligapro.ua/kvartiry_prodaga/ann-3451458.html"
    python3 extract_ligapro.py saved_page.html [url-сторінки]   # розділ береться з url
"""

import json
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# розділ сайту -> (тип угоди, тип об'єкта за замовчуванням, родина для уточнення з «Тип:»)
SECTIONS = {
    "kvartiry_prodaga": ("Продаж", "Квартира", "flat"),
    "kvartiry_komnaty_arenda": ("Оренда", "Квартира", "flat"),
    "kvartiry_posutochno_arenda": ("Подобова оренда", "Квартира", "flat"),
    "doma_dachi_prodaga": ("Продаж", "Будинок", "house"),
    "doma_dachy_arenda": ("Оренда", "Будинок", "house"),
    "doma_dachi_arenda": ("Оренда", "Будинок", "house"),
    "officy_prodaga": ("Продаж", "Офіс", "office"),
    "officy_arenda": ("Оренда", "Офіс", "office"),
    "kommerch_prodaga": ("Продаж", "Комерційне приміщення", "commercial"),
    "kommerch_arenda": ("Оренда", "Комерційне приміщення", "commercial"),
    "proizv_promushl_prodaga": ("Продаж", "Виробниче приміщення", "commercial"),
    "proizv_promushl_arenda": ("Оренда", "Виробниче приміщення", "commercial"),
    "zem_uchastki_prodaga": ("Продаж", "Земельна ділянка", "land"),
    "zem_uchastki_arenda": ("Оренда", "Земельна ділянка", "land"),
    "garagy_prodaga": ("Продаж", "Гараж", "garage"),
    "garagy_arenda": ("Оренда", "Гараж", "garage"),
}
# частина фото-шляху (…/pic/gallery/sell_app/…) — запасне джерело, якщо розділу в url нема
GALLERY_KINDS = {
    "sell_app": "kvartiry_prodaga", "rent_app": "kvartiry_komnaty_arenda",
    "sell_house": "doma_dachi_prodaga", "rent_house": "doma_dachy_arenda",
    "sell_office": "officy_prodaga", "rent_office": "officy_arenda",
    "sell_komm": "kommerch_prodaga", "rent_komm": "kommerch_arenda",
    "sell_zu": "zem_uchastki_prodaga", "rent_zu": "zem_uchastki_arenda",
    "sell_garage": "garagy_prodaga", "rent_garage": "garagy_arenda",
}

# «Тип:» з таблиці -> назва типу, яку розуміють програми сайтів (ключові слова dom.ria/rieltor/OLX)
HOUSE_TYPES = [
    (r"дуплекс", "Дуплекс"), (r"таунхаус", "Таунхаус"), (r"котедж", "Котедж"),
    (r"частин|пів\s*будин", "Частина будинку"), (r"дач", "Дача"), (r"вілл", "Вілла"),
    (r"будин|дом", "Будинок"),
]
COMMERCIAL_TYPES = [
    (r"торг|магазин|фасад", "Торгова площа"),
    (r"склад|ангар", "Склад"),
    (r"вироб|цех", "Виробниче приміщення"),
    (r"кафе|ресторан|\bбар\b", "Кафе / ресторан"),
    (r"готел|хостел|баз[аи] відпоч", "Готель"),
    (r"офіс|бізнес.?центр", "Офіс"),
    (r"сто\b|автомий|азс", "СТО"),
]
GARAGE_TYPES = [(r"паркінг|паркомісц|машиномісц", "Паркомісце"), (r"гараж|бокс", "Гараж")]

KYIV_NAMES = {"київ", "киев"}
STREET_SUFFIX_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<type>вул\.|просп\.|пров\.|бульв\.|пл\.|наб\.|туп\.|пр-д\.?|проїзд|узвіз|шосе|"
    r"тупик|алея|лінія|майдан|спуск|в'їзд|площа|провулок|проспект|бульвар|вулиця)$",
    re.IGNORECASE,
)
STREET_WORD_RE = re.compile(
    r"(?<![а-яіїєґ])(вул\.|вулиця|просп\.|проспект|пров\.|провулок|бульв\.|бульвар|пл\.|площа|наб\.|"
    r"набережна|шосе|узвіз|тупик|алея|лінія|майдан|спуск|проїзд)(?![а-яіїєґ])",
    re.IGNORECASE,
)
# номер будинку: «2к.1» -> «2», «б.3» -> «3», «13/7», «15г», «51/1А»; «к/корп» — корпус, не літера
HOUSE_PART_RE = re.compile(
    r"^(?:б\.|буд\.?|будинок|д\.)?\s*(\d+(?:[а-яіїєґa-z](?![.\d]))?(?:\s*/\s*\d+(?:[а-яіїєґa-z](?![.\d]))?)?)",
    re.IGNORECASE,
)
RAION_RE = re.compile(r"(ськ|цьк|зьк)ий$", re.IGNORECASE)
SETTLEMENT_PREFIX_RE = re.compile(r"^(м\.|місто|с\.|село|смт\.?|сел\.)\s*", re.IGNORECASE)


# ------------------------------------------------------------------ завантаження
def is_ligapro_url(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host == "ligapro.ua" or host.endswith(".ligapro.ua")


def decode_html(content: bytes, content_type: str = "") -> str:
    """Сайт віддає windows-1251; беремо кодування з заголовка/мета, за замовчуванням cp1251."""
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I) or \
        re.search(rb"charset=[\"']?([\w-]+)", content[:3000] or b"", re.I)
    if not m:                      # не вказано (напр. збережений файл): спершу utf-8, інакше cp1251
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            return content.decode("cp1251", errors="replace")
    enc = m.group(1)
    if isinstance(enc, bytes):
        enc = enc.decode("ascii", "ignore")
    try:
        return content.decode(enc, errors="replace")
    except LookupError:
        return content.decode("cp1251", errors="replace")


def fetch_ligapro_data(url: str, timeout: int = 15) -> dict:
    """Завантажує сторінку об'єкта і повертає дані у форматі extract_estate.py."""
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    data = extract_ligapro_data(decode_html(r.content, r.headers.get("Content-Type", "")), url=url)
    data["source_url"] = url
    return data


# ------------------------------------------------------------------ дрібні розбори
def _clean(s) -> str:
    return " ".join((s or "").replace("\xa0", " ").split())


def _num(s):
    """'72' / '14.4' / '22,1' -> '72' / '14.4' / '22.1' (рядком, як у RealtSoft)."""
    m = re.search(r"\d+(?:[.,]\d+)?", s or "")
    if not m:
        return None
    v = m.group(0).replace(",", ".")
    return v[:-2] if v.endswith(".0") else v


def _nums(s):
    return [x.replace(",", ".") for x in re.findall(r"\d+(?:[.,]\d+)?", s or "")]


def _pick(rules, text, default=None):
    t = (text or "").lower()
    for rx, name in rules:
        if re.search(rx, t):
            return name
    return default


def section_of(url: str, html_text: str = ""):
    path = urlparse(url or "").path
    for part in path.split("/"):
        if part in SECTIONS:
            return part
    m = re.search(r"/pic/gallery/([a-z_]+)/", html_text or "")
    if m and m.group(1) in GALLERY_KINDS:
        return GALLERY_KINDS[m.group(1)]
    return None


def street_to_crm(street: str) -> str:
    """'Зарічна вул.' -> 'вул. Зарічна'; 'Сергія Колоса вул. (Леніна)' -> 'вул. Сергія Колоса'."""
    s = _clean(re.sub(r"\(.*?\)", " ", street or ""))
    m = STREET_SUFFIX_RE.match(s)
    if m:
        return f"{m.group('type')} {m.group('name')}".strip()
    return s


def parse_address(raw: str) -> dict:
    """
    'Зарічна вул., 2к.1, Київ, Дарницький, м. Славутич'
      -> {city: 'Київ', district: 'Дарницький', street: 'вул. Зарічна', house: '2'}
    'Київ, Дніпровський, м. Лівобережна' -> без вулиці; 'Садова, Київ, …' — вулиця без типу.
    Мікрорайон і метро відкидаються.
    """
    s = re.sub(r"^\s*адреса\s*:\s*", "", _clean(raw), flags=re.I)
    parts = [p.strip() for p in s.split(",") if p.strip()]
    res = {"city": None, "district": None, "street": None, "house": None}
    if not parts:
        return res

    def is_city(p):
        return p.lower() in KYIV_NAMES or bool(SETTLEMENT_PREFIX_RE.match(p))

    i = 0
    if not is_city(parts[0]):
        res["street"] = parts[0]
        i = 1
        if i < len(parts) and HOUSE_PART_RE.match(parts[i]) and not is_city(parts[i]):
            res["house"] = HOUSE_PART_RE.match(parts[i]).group(1).replace(" ", "")
            i += 1
    if i < len(parts):
        res["city"] = SETTLEMENT_PREFIX_RE.sub("", parts[i]).strip()
        i += 1
    if i < len(parts) and not parts[i].lower().startswith("м. "):
        res["district"] = re.sub(r"\s*(район|р-н)\.?$", "", parts[i], flags=re.I).strip()

    # «Гатне, Києво-Святошинський» — без вулиці: друге слово — район, а не місто
    if res["street"] and not res["house"] and res["city"] and RAION_RE.search(res["city"]) \
            and res["city"].lower() not in KYIV_NAMES and not STREET_WORD_RE.search(res["street"]):
        res["district"], res["city"], res["street"] = res["city"], res["street"], None

    if res["street"]:
        res["street"] = street_to_crm(res["street"])
    return res


def address_to_crm(a: dict) -> str:
    """Збирає адресу так, як її пише RealtSoft: 'Київ, Дарницький район, вул. Зарічна, 2'."""
    district = a.get("district")
    if district and not re.search(r"район|р-н|обл", district, re.I):
        district = f"{district} район"
    return ", ".join(x for x in (a.get("city"), district, a.get("street"), a.get("house")) if x)


def kind_for(family: str, default_kind: str, type_text: str) -> str:
    if not type_text:
        return default_kind
    if family == "house":
        return _pick(HOUSE_TYPES, type_text, default_kind)
    if family == "commercial":
        return _pick(COMMERCIAL_TYPES, type_text, default_kind)
    if family == "garage":
        return _pick(GARAGE_TYPES, type_text, default_kind)
    if family == "flat" and re.search(r"кімнат", type_text.lower()):
        return "Кімната"
    return default_kind


# ------------------------------------------------------------------ головний розбір
def extract_ligapro_data(html: str, url: str = "") -> dict:
    soup = BeautifulSoup(html, "lxml")
    data = {"source": "ligapro", "estate_id": None}

    detail = soup.select_one(".property-detail") or soup

    # --- таблиця «Характеристики»
    raw = {}
    for tr in detail.select("table tr"):
        th, td = tr.find("th"), tr.find("td")
        if th and td:
            raw[_clean(th.get_text()).rstrip(":").strip()] = _clean(td.get_text(" "))
    data["ligapro_parameters"] = dict(raw)

    # --- номер об'єкта (артикул): «213-451-458»; запасний — з h1 «(№ …)»
    h1 = soup.find("h1")
    h1_text = _clean(h1.get_text(" ")) if h1 else ""
    article = raw.get("№ об'єкта") or raw.get("№ об’єкта")
    if not article:
        m = re.search(r"№\s*([\d-]+)\)?\s*$", h1_text)
        article = m.group(1) if m else None
    data["article"] = article
    data["ligapro_id"] = re.sub(r"\D", "", article or "") or None
    data["title"] = re.sub(r"\s*\(№\s*[\d-]+\)\s*$", "", h1_text).strip() or None

    # --- ціна: «129 000 $», «44 000 грн. + комунальні» -> «44 000 грн»
    price = raw.get("Ціна") or ""
    utilities = bool(re.search(r"комунал", price, re.I))
    price = re.sub(r"\s*\+.*$", "", price).strip()
    price = re.sub(r"грн\.?", "грн", price)
    data["price"] = price or None
    data["price_per_m2"] = None

    # --- тип угоди / об'єкта
    section = section_of(url, html)
    data["ligapro_section"] = section
    deal, kind, family = SECTIONS.get(section, (None, None, None))
    type_text = raw.get("Тип") or ""
    if kind:
        kind = kind_for(family, kind, type_text)
        data["deal_title"] = f"{deal} / {kind}"
    else:
        data["deal_title"] = None          # невідомий розділ — програма сайту спитає / зупиниться

    # --- адреса
    # <h3><p>адреса</p></h3> — lxml виносить <p> з <h3>, тому беремо весь перший блок .overview
    # без таблиці й заголовка h2 (далі йдуть «Характеристики», «Опис», карта)
    address_raw = ""
    for ov in detail.select(".overview"):
        if ov.find("table") or ov.find("h2"):
            break
        address_raw = _clean(ov.get_text(" "))
        if address_raw:
            break
    data["address_raw"] = re.sub(r"^\s*адреса\s*:\s*", "", address_raw, flags=re.I) or None
    parsed = parse_address(address_raw)
    data["address_parts"] = parsed
    data["address"] = address_to_crm(parsed) or None

    # --- параметри у словнику RealtSoft
    p = {}
    for label, value in raw.items():
        low = label.lower()
        if low in ("ціна", "№ об'єкта", "№ об’єкта"):
            continue
        if low == "кімнат":
            if _num(value):
                p["Кількість кімнат"] = _num(value)
        elif low == "поверх":
            n = _nums(value)
            if n:
                p["Поверх"] = n[0]
            if len(n) > 1:
                p["Поверховість"] = n[1]
        elif low in ("поверхів", "поверховість"):
            if _num(value):
                p["Поверховість"] = _num(value)
        elif low == "площа":
            n = _nums(value)
            if family == "land" or re.search(r"сот|\bга\b", value, re.I):
                v = _num(value)
                if v and re.search(r"\bга\b", value, re.I):
                    v = _num(str(float(v) * 100))
                if v:
                    p["Площа ділянки,соток"] = v
            elif n:
                p["Площа загальна,м²"] = n[0]
                if family == "flat" and len(n) >= 3:
                    p["Площа житлова,м²"] = n[1]
                    p["Площа кухні,м²"] = n[2]
                elif family == "flat" and len(n) == 2:
                    p["Площа житлова,м²"] = n[1]
        elif low == "площа будинку":
            if _num(value):
                p["Площа загальна,м²"] = _num(value)
        elif low == "ділянка":
            v = _num(value)
            if v and re.search(r"\bга\b", value, re.I):
                v = _num(str(float(v) * 100))
            if v:
                p["Площа ділянки,соток"] = v
        elif low == "спалень":
            p["Кількість спалень"] = _num(value) or value
        elif low == "кабінетів":
            p["Кількість приміщень"] = _num(value) or value
        elif low == "тип":
            if family in ("office", "commercial"):
                p["Тип об'єкта"] = value
            # для будинків/гаражів тип уже в deal_title
        else:
            p[label] = value
    if utilities:
        p["Комунальні"] = "окремо"

    # ЖК — посилання на /novostroyki/ під таблицею («ЖК Славутич» -> «Славутич», як «Комплекс» у RealtSoft)
    for a in detail.select(".tags-list a"):
        href = a.get("href") or ""
        name = _clean(a.get_text())
        if "novostroyk" in href or name.lower().startswith("жк"):
            p["Комплекс"] = re.sub(r"^ЖК\s*", "", name, flags=re.I).strip(" «»\"'")
            break
    data["parameters"] = p
    data["quick_facts"] = {}

    # --- опис
    desc = soup.select_one(".descr_in")
    if desc:
        for br in desc.find_all("br"):
            br.replace_with("\n")
        text = desc.get_text().replace("\xa0", " ").replace("\r", "")
        text = "\n".join(line.strip() for line in text.split("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        data["description"] = text
    else:
        data["description"] = ""
    # у будинках таблиця дає лише «Спалень», а кімнати LigaPro часто дописує в кінець опису
    # («Кількість кімнат: 3;») — беремо звідти, щоб dom.ria/OLX не питали зайве
    if "Кількість кімнат" not in p:
        m = re.search(r"Кількість кімнат\s*:\s*(\d+)", data["description"], re.I)
        if m:
            p["Кількість кімнат"] = m.group(1)

    # --- фото: повнорозмірні з data-image; запасний — мініатюра без префікса «s»
    photos = []
    for img in soup.select("#gallery img"):
        src = img.get("data-image") or ""
        if not src:
            thumb = img.get("src") or ""
            src = re.sub(r"/s([0-9a-f]{5,}_?[0-9a-f]*\.\w+)$", r"/\1", thumb)
        if src and src not in photos:
            photos.append(src)
    if not photos:
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and og.get("content"):
            photos.append(og["content"])
    data["photos"] = photos
    data["photos_count"] = len(photos)
    return data


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Використання: python3 extract_ligapro.py <URL або файл.html> [url сторінки для файлу]")
        sys.exit(1)
    src = sys.argv[1]
    if src.startswith(("http://", "https://")):
        result = fetch_ligapro_data(src)
    else:
        with open(src, "rb") as f:
            result = extract_ligapro_data(decode_html(f.read()), url=sys.argv[2] if len(sys.argv) > 2 else "")
    print(json.dumps(result, ensure_ascii=False, indent=2))
