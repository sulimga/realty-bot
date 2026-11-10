"""
Сирі дані оголошення OLX (olx_source.fetch) -> модель презентації.
Та сама логіка, що в розширенні Chrome (extension/olx-presentation/lib/normalize.js) — правити обидва місця.

Модель:
  kicker "Продаж / Квартира", group, price {"text", "show"}, rows [{"key","label","value"}],
  description (без контактів продавця), photos [url], source_url, source_id, notes
"""

import html as _html
import json
import math
import re

from olx_categories import OLX_CATS

SKIP_KEYS = {"price", "commission", "cooperate", "is_exchange", "private_business", "currency", "sell_or_rent"}
SKIP_LABEL = re.compile(r"ріелтор|риелтор|комісі|комисси|приватна особа|частное лицо|^бізнес$|^бизнес$|обмін|обмен", re.I)

COMM_TYPES = {
    3679: "Торгове приміщення", 3692: "Торгове приміщення",
    3680: "МАФ", 3695: "МАФ",
    3681: "Офіс", 3690: "Офіс",
    3682: "Склад / ангар", 3693: "Склад / ангар",
    3683: "Кафе / ресторан", 3694: "Кафе / ресторан",
    3684: "Готель / база відпочинку",
    3685: "СТО / автомийка / АЗС", 3696: "СТО / автомийка / АЗС",
    3686: "Салон краси / клініка", 3697: "Салон / кабінет краси",
    3687: "Виробниче приміщення", 3691: "Виробниче приміщення",
    3688: "Приміщення вільного призначення", 3700: "Приміщення вільного призначення",
    3689: "Комерційне приміщення", 3701: "Комерційне приміщення",
    3698: "Зал / спортивне приміщення",
    3699: "Медичне приміщення",
    3298: "Коворкінг",
}

LABEL_KEYS = [
    (re.compile(r"^(загальна площа|общая площадь)", re.I), "total_area"),
    (re.compile(r"(площа кухні|площадь кухни)", re.I), "kitchen_area"),
    (re.compile(r"(площа ділянки|площадь участка)", re.I), "land_area"),
    (re.compile(r"(площа будинку|площадь дома)", re.I), "house_area"),
    (re.compile(r"^(поверховість|этажность)", re.I), "total_floors"),
    (re.compile(r"^(поверх|этаж)$", re.I), "floor"),
    (re.compile(r"(кількість кімнат|количество комнат)", re.I), "number_of_rooms_string"),
    (re.compile(r"^(назва жк|название жк)", re.I), "zkh"),
    (re.compile(r"^(ціна|цена)$", re.I), "price"),
]

CUR_SYMBOL = {"USD": "$", "EUR": "€", "UAH": "грн"}

EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")


# ---------- дрібні помічники ----------

def clean(s):
    s = str(s if s is not None else "").replace(" ", " ").replace(" ", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def strip_emoji(s):
    return clean(EMOJI.sub("", str(s or "")))


def html_to_text(s):
    if not s:
        return ""
    s = str(s).replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"<br\s*/?>[ \t]*\n?", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|li|h\d)>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return _html.unescape(s)


def to_num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    m = re.search(r"\d[\d ]*(?:[.,]\d+)?", str(v).replace(" ", " ").replace(" ", " "))
    if not m:
        return None
    try:
        return float(m.group(0).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def fmt_num(n):
    return f"{int(math.floor(n + 0.5)):,}".replace(",", " ")


def num_text(n):
    return str(int(n)) if n == int(n) else str(n)


def parse_price(text):
    t = str(text or "")
    cur = ""
    if re.search(r"\$|usd|дол", t, re.I):
        cur = "USD"
    elif re.search(r"€|eur|євро|евро", t, re.I):
        cur = "EUR"
    elif re.search(r"грн|uah|₴", t, re.I):
        cur = "UAH"
    return to_num(t), cur


_CONTACT_WORDS = (r"тел\.?|телефон|телефонуйте|дзвоніть|звоніть|звоните|пишіть|пишите|phone|tel|viber|вайбер|"
                  r"telegram|телеграм|whatsapp|ватсап|або|или|чи|в|у|на")
_CONTACT_LINE = re.compile(rf"^[ \t]*(?:(?:{_CONTACT_WORDS})[ \t:.,;\-–/()|]*)+$", re.I | re.M)
_PHONE = re.compile(r"(?:\+?\s*3\s*8\s*)?(?:\(\s*)?0\d{2}(?:\s*\))?[\s.\-–]*\d{3}[\s.\-–]*\d{2}[\s.\-–]*\d{2}(?!\d)")


def strip_contacts(text):
    """Прибирає телефони, посилання, email і @нікнейми продавця (і рядки, де лишилось «Телефонуйте: (Viber)»)."""
    t = str(text or "")
    t = re.sub(r"https?://\S+|www\.\S+", "", t, flags=re.I)
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "", t)
    t = re.sub(r"(^|[\s(])@[A-Za-z0-9_]{4,}", r"\1", t)
    t = _PHONE.sub("", t)
    t = _CONTACT_LINE.sub("", t)
    return t


def tidy_text(text):
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in str(text or "").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ---------- категорія ----------

def cat_chain(cat_id):
    out, cur = [], int(cat_id or 0) if str(cat_id or "").isdigit() else 0
    for _ in range(10):
        if not cur or cur not in OLX_CATS:
            break
        out.append((cur, OLX_CATS[cur][0]))
        cur = OLX_CATS[cur][1]
    return out


def classify(cat_id, crumbs_text, P):
    def val(k):
        return next((p["text"] for p in P if p["key"] == k), "")

    def has(k):
        return any(p["key"] == k for p in P)

    chain = cat_chain(cat_id)
    ids = [c[0] for c in chain]
    text = (" / ".join(c[1] for c in chain) if chain else (crumbs_text or "")).lower()

    group = ("flat" if 1757 in ids else "room" if 1754 in ids else "house" if 206 in ids else "land" if 1606 in ids
             else "commercial" if 1610 in ids else "parking" if 1616 in ids else "abroad" if 1522 in ids else "")
    if not group and text:
        if re.search(r"квартир", text):
            group = "flat"
        elif re.search(r"кімнат|комнат", text):
            group = "room"
        elif re.search(r"будин|котедж|дач|(^|[^а-яіїєґ])дом", text):
            group = "house"
        elif re.search(r"земл|ділянк|участ", text):
            group = "land"
        elif re.search(r"комерц|коммерч|офіс|офис|склад|магазин|приміщ|помещ", text):
            group = "commercial"
        elif re.search(r"гараж|паркін|паркинг|парков", text):
            group = "parking"
    if not group:
        for k, g in (("number_of_rooms_string", "flat"), ("property_type_houses", "house"), ("property_type_land", "land"),
                     ("comm_re_type", "commercial"), ("comm_re_location", "commercial"), ("property_type_parking", "parking")):
            if has(k):
                group = g
                break

    deal = "Оренда" if re.search(r"оренд|аренд|коворк", text) else "Продаж" if "продаж" in text else val("sell_or_rent")
    used = []

    def take(k):
        v = val(k)
        if v:
            used.append(k)
        return v

    obj, purpose = "", ""
    if group == "flat":
        obj, purpose = "Квартира", "Житлова"
    elif group == "room":
        obj, purpose = "Кімната", "Житлова"
    elif group == "house":
        obj, purpose = take("property_type_houses") or "Будинок", "Житлова"
    elif group == "land":
        obj, purpose = "Земельна ділянка", take("property_type_land") or "Земля"
    elif group == "commercial":
        obj = COMM_TYPES.get(ids[0], "") if ids else ""
        if not obj:
            for rx, name in ((r"офіс|офис", "Офіс"), (r"склад|ангар", "Склад / ангар"), (r"торгов|магазин", "Торгове приміщення"),
                             (r"виробн|производ", "Виробниче приміщення"), (r"кафе|ресторан", "Кафе / ресторан"),
                             (r"вільного|свободного", "Приміщення вільного призначення")):
                if re.search(rx, text):
                    obj = name
                    break
        obj, purpose = obj or "Комерційне приміщення", "Нежитлова (комерційна)"
    elif group == "parking":
        obj, purpose = take("property_type_parking") or "Гараж / паркомісце", "Нежитлова"
    elif group == "abroad":
        obj = take("property_type_abroad") or "Нерухомість за кордоном"
        purpose = "Нежитлова (комерційна)" if re.search(r"комерц", obj, re.I) else "Земля" if re.search(r"земл", obj, re.I) else "Житлова"
    return {"group": group, "deal": deal, "object_type": obj, "purpose": purpose, "used": used}


# ---------- параметри ----------

def _param_from_obj(p):
    if not isinstance(p, dict):
        return None
    key = str(p.get("key") or p.get("code") or "")
    label = clean(p.get("name") or p.get("label") or "")
    v = p.get("value")
    text, num = "", None
    if isinstance(v, dict):
        if v.get("label") is not None:
            text = str(v["label"])
        elif v.get("value") is not None:
            text = str(v["value"])
        else:
            text = str(v.get("key") or "")
        num = to_num(v.get("key") if v.get("key") is not None else v.get("value"))
    elif isinstance(v, list):
        text = ", ".join(str((x.get("label") or x.get("value") or x.get("key")) if isinstance(x, dict) else x) for x in v if x)
    elif v is not None:
        text = str(v)
    if num is None:
        num = to_num(p.get("normalizedValue") if p.get("normalizedValue") is not None else text)
    if not (label or key):
        return None
    return {"key": key, "label": label, "text": clean(text), "num": num, "value": v}


def _param_from_text(s):
    s = str(s)
    i = s.find(":")
    if i < 1:
        return None
    label, text = clean(s[:i]), clean(s[i + 1:])
    key = next((k for rx, k in LABEL_KEYS if rx.search(label)), "")
    return {"key": key, "label": label, "text": text, "num": to_num(text), "value": None}


def collect_params(raw):
    api, st = raw.get("api") or {}, raw.get("main") or {}
    src = api.get("params") if isinstance(api.get("params"), list) and api.get("params") else \
        st.get("params") if isinstance(st.get("params"), list) and st.get("params") else None
    if src:
        return [p for p in map(_param_from_obj, src) if p]
    return [p for p in map(_param_from_text, (raw.get("dom") or {}).get("params") or []) if p]


def _with_unit(p, unit):
    if not p or not p["text"]:
        return ""
    return f"{p['text']} {unit}" if re.fullmatch(r"\d+(?:[.,]\d+)?", p["text"]) else p["text"]


# ---------- ціна, адреса, опис, фото ----------

def _pick_price(raw, P, deal):
    pp = next((p for p in P if p["key"] == "price"), None)
    st = raw.get("main") or {}
    ld = (raw.get("ld") or {}).get("product") or {}
    text = ""
    if pp and isinstance(pp.get("value"), dict):
        text = clean(pp["value"].get("label") or "")
    elif pp and pp["text"]:
        text = pp["text"]
    if not text and isinstance(st.get("price"), dict):
        text = clean(st["price"].get("displayValue") or "")
    if not text and (raw.get("dom") or {}).get("price"):
        text = clean(raw["dom"]["price"].split("\n")[0])
    if not text and ld.get("price") is not None:
        v = to_num(ld.get("price"))
        if v is not None:
            text = f"{fmt_num(v)} {CUR_SYMBOL.get(ld.get('currency'), ld.get('currency') or '')}".strip()
    if text and deal == "Оренда" and not re.search(r"міс|мес", text, re.I):
        text += " / міс."
    return text


def format_address(city, district, region):
    city, district, region = clean(city), clean(district), clean(region)
    parts = []
    if city:
        parts.append(city)
    if district and district.lower() != city.lower():
        parts.append(district if re.search(r"район|р-н", district, re.I) else f"{district} район")
    if region and (not city or not region.lower().startswith(city.lower()[:4])):
        parts.append(region)
    return ", ".join(parts)


def _name(x):
    return x.get("name") if isinstance(x, dict) else ""


def _pick_address(raw):
    al = (raw.get("api") or {}).get("location") or {}
    if al.get("city") or al.get("district"):
        return format_address(_name(al.get("city")), _name(al.get("district")), _name(al.get("region")))
    sl = (raw.get("main") or {}).get("location") or {}
    if sl.get("cityName") or sl.get("districtName"):
        return format_address(sl.get("cityName"), sl.get("districtName"), sl.get("regionName"))
    dl = (raw.get("dom") or {}).get("location") or []
    lines = [ln for ln in dl if not re.search(r"\d{1,2}:\d{2}|сьогодні|сегодня|вчора|вчера|карт|місцезнаходження|местоположение", ln, re.I)]
    if lines:
        first = lines[0]
        second = lines[1] if len(lines) > 1 else ""
        parts = re.split(r"\s*[,–-]\s*", first)
        return format_address(parts[0], parts[1] if len(parts) > 1 else "", second if re.search(r"обл", second, re.I) else "")
    area = ((raw.get("ld") or {}).get("product") or {}).get("area")
    return clean(area if isinstance(area, str) else "")


def _pick_description(raw):
    for c in ((raw.get("api") or {}).get("description"), (raw.get("main") or {}).get("description"),
              (raw.get("dom") or {}).get("description"), ((raw.get("ld") or {}).get("product") or {}).get("description")):
        if c and str(c).strip():
            return tidy_text(html_to_text(c))
    return ""


def _photo_key(u):
    m = re.search(r"/files/([^/]+)/", u)
    return m.group(1) if m else u.split("?")[0]


def _size_link(link, w, h):
    W, H = 1280, 960
    if w and h:
        k = min(1.0, 1600 / max(w, h))
        W, H = round(w * k), round(h * k)
    return str(link).replace("{width}", str(W)).replace("{height}", str(H))


def pick_photos(raw):
    out, seen = [], set()

    def add(u):
        if not u or not isinstance(u, str):
            return
        u = re.sub(r"^//", "https://", u)
        u = re.sub(r"(olxcdn\.com|akamaized\.net):443/", r"\1/", u)
        if not u.startswith("https://"):
            return
        k = _photo_key(u)
        if k not in seen:
            seen.add(k)
            out.append(u)

    api = raw.get("api") or {}
    for p in api.get("photos") or []:
        add(_size_link(p["link"], p.get("width"), p.get("height")) if isinstance(p, dict) and p.get("link") else p if isinstance(p, str) else "")
    if not out:
        for p in (raw.get("main") or {}).get("photos") or []:
            add(p if isinstance(p, str) else (_size_link(p["link"], p.get("width"), p.get("height")) if p.get("link") else p.get("url")) if isinstance(p, dict) else "")
    if not out:
        for u in (raw.get("dom") or {}).get("photos") or []:
            add(u)
    if not out:
        for u in ((raw.get("ld") or {}).get("product") or {}).get("image") or []:
            add(u)
    if not out:
        add((raw.get("dom") or {}).get("og_image"))
    return out


# ---------- головне ----------

def normalize_olx(raw):
    raw = raw or {}
    P = collect_params(raw)
    cat_id = ((raw.get("api") or {}).get("category") or {}).get("id") or ((raw.get("main") or {}).get("category") or {}).get("id")
    crumbs = " / ".join([*((raw.get("dom") or {}).get("breadcrumbs") or []), *((raw.get("ld") or {}).get("breadcrumbs") or []),
                         ((raw.get("ld") or {}).get("product") or {}).get("category") or ""])
    cls = classify(cat_id, crumbs, P)

    def by(k):
        return next((p for p in P if p["key"] == k), None)

    rows = []

    def add(key, label, value):
        value = strip_emoji(value)
        if value:
            rows.append({"key": key, "label": label, "value": value})

    add("address", "Адреса", _pick_address(raw))
    add("deal", "Тип угоди", cls["deal"])
    add("object_type", "Тип об'єкта", cls["object_type"])
    add("purpose", "Призначення", cls["purpose"])
    add("total_area", "Загальна площа", _with_unit(by("total_area"), "м²"))
    add("house_area", "Площа будинку", _with_unit(by("house_area"), "м²"))
    add("kitchen_area", "Площа кухні", _with_unit(by("kitchen_area"), "м²"))
    add("land_area", "Площа ділянки", _with_unit(by("land_area"), "сот."))
    add("rooms", "Кількість кімнат", (by("number_of_rooms_string") or by("number_of_rooms") or {}).get("text"))
    add("floor", "Поверх", (by("floor") or {}).get("text"))
    add("total_floors", "Поверховість", (by("total_floors") or {}).get("text"))
    used = {"total_area", "house_area", "kitchen_area", "land_area", "number_of_rooms_string", "number_of_rooms",
            "floor", "total_floors", *cls["used"]}
    for p in P:
        if not p["label"] or not p["text"] or p["key"] in used or p["key"] in SKIP_KEYS or SKIP_LABEL.search(p["label"]):
            continue
        if any(r["label"] == p["label"] for r in rows):
            continue
        add(p["key"], p["label"], p["text"])

    desc = _pick_description(raw)
    title = clean((raw.get("api") or {}).get("title") or (raw.get("main") or {}).get("title") or (raw.get("dom") or {}).get("title") or "")
    return {
        "source": "OLX",
        "source_url": raw.get("url") or "",
        "source_id": raw.get("id") or (raw.get("api") or {}).get("id") or (raw.get("main") or {}).get("id"),
        "original_title": title,
        "kicker": " / ".join(x for x in (cls["deal"], cls["object_type"]) if x) or title,
        "group": cls["group"],
        "price": {"text": _pick_price(raw, P, cls["deal"]), "show": True},
        "rows": rows,
        "description": tidy_text(strip_contacts(desc)),
        "photos": pick_photos(raw),
        "notes": raw.get("notes") or [],
    }


# ---------- похідні значення ----------

def row_val(rows, k):
    return next((r["value"] for r in rows or [] if r["key"] == k), "")


def quick_facts(rows):
    q = []
    area, rooms = row_val(rows, "total_area"), row_val(rows, "rooms")
    floor, floors, land = row_val(rows, "floor"), row_val(rows, "total_floors"), row_val(rows, "land_area")
    if area:
        q.append(area)
    if rooms:
        m = re.match(r"^(\d+\+?)", rooms)
        q.append(f"{m.group(1)} кімн." if m else rooms)

    def n(s):
        v = to_num(s)
        return num_text(v) if v is not None else s
    if floor and floors:
        q.append(f"{n(floor)}/{n(floors)} пов.")
    elif floor:
        q.append(f"{floor} пов.")
    elif floors:
        q.append(f"{n(floors)}-поверх.")
    if land:
        q.append(land)
    return q


def unit_price(model):
    if not model.get("price", {}).get("text"):
        return ""
    value, cur = parse_price(model["price"]["text"])
    if not value or not cur:
        return ""
    sym = CUR_SYMBOL.get(cur, cur)
    land = to_num(row_val(model["rows"], "land_area"))
    if model.get("group") == "land" and land:
        return f"{fmt_num(value / land)} {sym}/сот."
    area = to_num(row_val(model["rows"], "total_area"))
    return f"{fmt_num(value / area)} {sym}/м²" if area else ""


def presentation_title(model):
    head = ", ".join(x for x in (model.get("kicker"), row_val(model.get("rows"), "total_area")) if x)
    addr = row_val(model.get("rows"), "address")
    return ((f"{head} — {addr}" if addr else head)[:250]) or "Презентація об'єкта"


def dumps(model):
    return json.dumps(model, ensure_ascii=False, indent=1)
