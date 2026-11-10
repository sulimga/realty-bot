"""
Сирі дані dom.ria (domria_source.fetch) -> модель презентації (та сама, що для OLX: normalize.normalize_olx).

Звідки поля (по черзі): JSON оголошення (API сайту або вбудований у сторінку) — поля верхнього рівня
(price, total_square_meters, city_name_uk…) і characteristics_values {ID: значення}; назви характеристик і варіантів —
з довідника boards/domria/characteristics.json; запасні — JSON-LD і розмітка сторінки.
"""

import json
import os
import re

from normalize import (CUR_SYMBOL, clean, fmt_num, format_address, html_to_text, num_text, strip_contacts,
                       strip_emoji, tidy_text, to_num)

HERE = os.path.dirname(os.path.abspath(__file__))
CHARS_PATH = os.path.join(os.path.dirname(HERE), "domria", "characteristics.json")

TYPE_NAMES = {2: "Квартира", 3: "Кімната", 5: "Будинок", 6: "Частина будинку", 7: "Дача", 8: "Дуплекс", 9: "Таунхаус",
              11: "Офісне приміщення", 12: "Офісна будівля", 14: "Торгова площа", 15: "Склад", 16: "Виробниче приміщення",
              17: "Кафе / ресторан", 18: "Приміщення сфери послуг", 19: "Готель", 20: "База відпочинку",
              21: "Приміщення вільного призначення", 22: "Готовий бізнес", 25: "Ділянка під житлову забудову",
              26: "Комерційна ділянка", 27: "Ділянка с/г призначення", 31: "Місце в гаражному кооперативі",
              32: "Гараж", 33: "Підземний паркінг", 34: "Гараж", 35: "Місце на стоянці"}
ADVERT = {1: "Продаж", 3: "Оренда", 4: "Подобова оренда", 2: "Обмін"}
CUR_BY_ID = {1: "USD", 2: "EUR", 3: "UAH"}
CUR_BY_SYM = {"$": "USD", "€": "EUR", "грн": "UAH", "грн.": "UAH", "uah": "UAH", "usd": "USD", "eur": "EUR"}

# характеристики, що вже в окремих рядках або службові (ціна, комісія, від кого)
USED_CHARS = {209, 210, 211, 214, 215, 216, 217, 218, 219, 226, 227, 228, 229, 1917,
              234, 235, 242, 246, 247, 251, 1437, 2014, 2099, 1748, 118, 149, 729}
WALL_CHARS = (118, 149, 729)


def group_of(type_id):
    t = int(type_id or 0)
    if t in (2,):
        return "flat"
    if t == 3:
        return "room"
    if 5 <= t <= 9:
        return "house"
    if 10 <= t <= 22:
        return "commercial"
    if 24 <= t <= 29:
        return "land"
    if 30 <= t <= 39:
        return "parking"
    return ""


_CHARS = None


def chars_dict():
    global _CHARS
    if _CHARS is None:
        try:
            with open(CHARS_PATH, encoding="utf-8") as f:
                _CHARS = json.load(f)
        except (OSError, ValueError):
            _CHARS = {}
    return _CHARS


def char_def(cid, type_id, advert_id):
    """Опис характеристики з довідника: спершу для свого типу/угоди, потім будь-який."""
    d = chars_dict()
    for key in (f"{type_id}_{advert_id}", f"{type_id}_1"):
        if str(cid) in d.get(key, {}):
            return d[key][str(cid)]
    for v in d.values():
        if str(cid) in v:
            return v[str(cid)]
    return None


def char_text(cid, val, type_id, advert_id):
    """Значення характеристики людською мовою ('' — не вміємо показати)."""
    cd = char_def(cid, type_id, advert_id)
    if val in (None, "", [], 0, "0") and not isinstance(val, bool):
        return ""
    opts = (cd or {}).get("o") or {}
    vals = val if isinstance(val, list) else [val]
    if opts:
        out = [opts.get(str(v)) for v in vals if opts.get(str(v))]
        return ", ".join(out)
    if isinstance(val, (int, float, str)) and str(val).strip():
        return num_text(float(val)) if isinstance(val, (int, float)) else clean(val)
    return ""


def char_label(cid, type_id, advert_id):
    cd = char_def(cid, type_id, advert_id)
    if not cd:
        return ""
    lbl = clean(cd.get("l") or cd.get("g") or "")
    return lbl[:1].upper() + lbl[1:]


def _pick(d, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _cv(d):
    cv = d.get("characteristics_values") or d.get("characteristicsValues") or {}
    if isinstance(cv, list):
        out = {}
        for x in cv:
            if isinstance(x, dict) and x.get("id") is not None:
                out[str(x["id"])] = x.get("value")
        cv = out
    return {str(k): v for k, v in cv.items()} if isinstance(cv, dict) else {}


def _area(v, unit="м²"):
    n = to_num(v)
    if not n:
        return ""
    return f"{num_text(round(n, 2))} {unit}"


def _price(d):
    price = to_num(_pick(d, "price", "price_total"))
    cur = None
    cid = _pick(d, "currency_type_id", "currencyTypeId")
    if cid:
        cur = CUR_BY_ID.get(int(to_num(cid) or 0))
    if not cur:
        sym = str(_pick(d, "currency_type_uk", "currency_type", "currency") or "").strip().lower()
        cur = CUR_BY_SYM.get(sym)
    arr = d.get("priceArr") or d.get("price_arr")
    if isinstance(arr, dict) and cur:
        k = {"USD": "1", "EUR": "2", "UAH": "3"}[cur]
        if arr.get(k):
            price = to_num(arr[k]) or price
    if not price:
        return ""
    return f"{fmt_num(price)} {CUR_SYMBOL.get(cur, '$' if not cur else cur)}"


def _photos(raw, d):
    """URL фото: спершу з JSON (порядок за ordering), адреси — як на сторінці (photosnew/<slug>__<id>xl.jpg)."""
    dom = raw.get("dom") or {}
    slug = dom.get("slug") or ""
    page_ids = [p["id"] for p in dom.get("photos") or []]
    ids, files = [], {}
    ph = d.get("photos") if d else None
    if isinstance(ph, dict):
        items = sorted(ph.items(), key=lambda kv: (to_num((kv[1] or {}).get("ordering")) or 0) if isinstance(kv[1], dict) else 0)
        for pid, info in items:
            ids.append(str(pid))
            if isinstance(info, dict) and info.get("file"):
                files[str(pid)] = info["file"]
    elif isinstance(ph, list):
        for info in ph:
            if isinstance(info, dict):
                pid = str(info.get("id") or info.get("photo_id") or "")
                if pid:
                    ids.append(pid)
                    if info.get("file"):
                        files[pid] = info["file"]
    if not ids:
        ids = page_ids
    out = []
    for pid in ids:
        if slug:
            out.append(f"https://cdn.riastatic.com/photosnew/dom/photo/{slug}__{pid}xl.jpg")
        elif pid in files:
            out.append("https://cdn.riastatic.com/photos/" + re.sub(r"\.(jpg|jpeg|png|webp)$", r"xl.\1", files[pid]))
    if not out and d and d.get("main_photo"):
        out.append("https://cdn.riastatic.com/photos/" + re.sub(r"\.(jpg|jpeg|png|webp)$", r"xl.\1", d["main_photo"]))
    if not out and dom.get("og_image"):
        out.append(dom["og_image"])
    return out


def _ld_fields(raw):
    """Запасне з JSON-LD: адреса, площа, кімнати, ціна, опис, фото."""
    out = {}
    for it in raw.get("ld") or []:
        addr = it.get("address") if isinstance(it.get("address"), dict) else None
        if addr and not out.get("address"):
            street = clean(addr.get("streetAddress"))
            out["address"] = ", ".join(x for x in (clean(addr.get("addressLocality")), street) if x)
        fs = it.get("floorSize")
        if isinstance(fs, dict) and fs.get("value") and not out.get("area"):
            out["area"] = fs.get("value")
        if it.get("numberOfRooms") and not out.get("rooms"):
            out["rooms"] = it.get("numberOfRooms")
        offers = it.get("offers")
        offers = offers[0] if isinstance(offers, list) and offers else offers
        if isinstance(offers, dict) and offers.get("price") and not out.get("price"):
            cur = str(offers.get("priceCurrency") or "").upper()
            out["price"] = f"{fmt_num(to_num(offers['price']) or 0)} {CUR_SYMBOL.get(cur, cur)}".strip()
        if it.get("description") and not out.get("description"):
            out["description"] = it["description"]
    return out


def normalize_domria(raw):
    raw = raw or {}
    d = dict(raw.get("embedded") or {})
    d.update({k: v for k, v in (raw.get("api") or {}).items() if v not in (None, "", [], {})})   # API важливіший
    cv = _cv(d)
    ld = _ld_fields(raw)
    dom = raw.get("dom") or {}
    notes = list(raw.get("notes") or [])

    type_id = int(to_num(_pick(d, "realty_type_id", "realtyTypeId")) or 0)
    advert_id = int(to_num(_pick(d, "advert_type_id", "advertTypeId")) or 0)
    group = group_of(type_id)
    if not group:   # без JSON — за адресою сторінки
        slug = dom.get("slug") or ""
        for kw, g, t in (("kvartira", "flat", 2), ("komnata", "room", 3), ("dom", "house", 5), ("uchastok", "land", 25),
                         ("ofis", "commercial", 11), ("pomeschenie", "commercial", 21), ("garazh", "parking", 34)):
            if kw in slug:
                group, type_id = g, type_id or t
                break
        if not advert_id:
            advert_id = 3 if "arenda" in (dom.get("slug") or "") or "dolgosrochnaya" in (dom.get("slug") or "") else 1
    deal = clean(_pick(d, "advert_type_name_uk") or "") or ADVERT.get(advert_id, "")
    deal = "Продаж" if re.match(r"прода", deal, re.I) else "Оренда" if re.search(r"оренд|аренд", deal, re.I) else deal
    obj = clean(_pick(d, "realty_type_name_uk") or "") or TYPE_NAMES.get(type_id, "")
    obj = obj[:1].upper() + obj[1:]

    def ch(*ids):
        for cid in ids:
            v = cv.get(str(cid))
            if v not in (None, "", 0, "0", []):
                return v
        return None

    # адреса: місто, район, вулиця з номером (як у публічних презентаціях CRM)
    city = clean(_pick(d, "city_name_uk", "city_name") or "")
    district = clean(_pick(d, "district_name_uk", "district_name") or "")
    region = clean(_pick(d, "state_name_uk", "state_name") or "")
    street = clean(_pick(d, "street_name_uk", "street_name") or "")
    house = clean(_pick(d, "building_number_str", "building_number") or "")
    if region and "обл" not in region.lower() and region.lower() not in ("київ", "kyiv"):
        region += " обл."                     # dom.ria дає «Київська» без «область»
    address = format_address(city, district, region)
    if street:
        address = ", ".join(x for x in (address, f"{street}, {house}".strip(", ")) if x)
    if not address:
        address = ld.get("address", "")

    purpose = ""
    if group in ("flat", "room", "house"):
        purpose = "Житлова"
    elif group == "commercial":
        purpose = "Нежитлова (комерційна)"
    elif group == "parking":
        purpose = "Нежитлова"
    elif group == "land":
        purpose = char_text(2099, ch(2099), type_id, advert_id) or "Земля"

    total = _pick(d, "total_square_meters") or ch(214, 215) or ld.get("area")
    living = _pick(d, "living_square_meters") or ch(216, 217)
    kitchen = _pick(d, "kitchen_square_meters") or ch(218)
    land = ch(219, 1917) or _pick(d, "plot_area", "ares_count")
    land_unit = char_text(226, ch(226), type_id, advert_id) if ch(226) else ""
    land_txt = ""
    if to_num(land):
        unit = "га" if re.search(r"гект", land_unit or "", re.I) else "м²" if re.search(r"кв", land_unit or "", re.I) else "сот."
        land_txt = f"{num_text(to_num(land))} {unit}"
    rooms = _pick(d, "rooms_count") or ch(209, 210) or ld.get("rooms")
    floor = _pick(d, "floor") or ch(227)
    floors = _pick(d, "floors_count") or ch(228, 229)
    walls = clean(_pick(d, "wall_type_uk") or "") or char_text(next((c for c in WALL_CHARS if ch(c)), 0), ch(*WALL_CHARS), type_id, advert_id)
    zk = clean(_pick(d, "user_newbuild_name_uk", "newbuild_name_uk", "user_newbuild_name", "newbuild_name") or "")

    rows = []

    def add(key, label, value):
        value = strip_emoji(value)
        if value:
            rows.append({"key": key, "label": label, "value": value})

    add("address", "Адреса", address)
    add("deal", "Тип угоди", deal)
    add("object_type", "Тип об'єкта", obj)
    add("purpose", "Призначення", purpose)
    add("zk", "ЖК", zk)
    add("total_area", "Загальна площа", _area(total))
    add("living_area", "Житлова площа" if group != "commercial" else "Корисна площа", _area(living))
    add("kitchen_area", "Площа кухні", _area(kitchen))
    add("land_area", "Площа ділянки", land_txt)
    if to_num(rooms):
        add("rooms", "Кількість кімнат" if group != "commercial" else "Кількість приміщень", num_text(to_num(rooms)))
    if to_num(floor):
        add("floor", "Поверх", num_text(to_num(floor)))
    if to_num(floors):
        add("total_floors", "Поверховість", num_text(to_num(floors)))
    add("walls", "Тип стін", walls)
    for cid, val in sorted(cv.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0):
        if not str(cid).isdigit() or int(cid) in USED_CHARS:
            continue
        label, text = char_label(cid, type_id, advert_id), char_text(cid, val, type_id, advert_id)
        if label and text and not any(r["label"] == label for r in rows):
            add(f"ch_{cid}", label, text)

    price = _price(d) or ld.get("price", "")
    if price and deal == "Оренда" and not re.search(r"міс|мес", price):
        price += " / міс."
    elif price and deal == "Подобова оренда":
        price += " / доба"

    desc_src = _pick(d, "description_uk", "description") or ld.get("description") or dom.get("description") or ""
    desc = tidy_text(html_to_text(desc_src))
    if not (raw.get("api") or raw.get("embedded")):
        notes.append("JSON оголошення не знайдено — дані лише з розмітки сторінки")

    title = clean(dom.get("h1") or dom.get("title") or "")
    return {
        "source": "dom.ria",
        "source_url": raw.get("url") or "",
        "source_id": raw.get("id"),
        "original_title": title,
        "kicker": " / ".join(x for x in (deal, obj) if x) or title,
        "group": group,
        "price": {"text": price, "show": True},
        "rows": rows,
        "description": tidy_text(strip_contacts(desc)),
        "photos": _photos(raw, d),
        "notes": notes,
    }
