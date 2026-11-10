"""
OLX — точка входу для диспетчера (протокол: common/board_protocol.py).

    python run.py post --url <посилання CRM> [--dry-run]
    python run.py check
    python run.py login          # постійний вхід за посиланням (OAuth code + PKCE), без консолі й інкогніто
    python run.py keepalive      # оновити постійний вхід (бот викликає за розкладом, boards.json keepalive_h)
    python run.py api-login      # офіційне підключення (OAuth «Дозволити»): посилання → OLX → наша приймальна адреса
    python run.py api-probe      # перевірка офіційного API на живому токені (що повертає OLX) → JSON-файл у чат

Вміє: будинки, квартири (продаж і довгострокова оренда), комерцію всіх типів (продаж і оренда).
Категорії й обов'язкові поля — з categories.json (див. mapping.py).
Оголошення створюється одразу (як після кнопки «Опублікувати» у формі) і чекає на
оплату/активацію в кабінеті OLX — у категорії нерухомості розміщення платне.
--dry-run: усе зібрати й показати, нічого не відправляти на OLX (фото теж не вантажаться).

Дані: $REALTY_DATA_DIR/secrets/olx_{auth,refresh,cookies}.txt, $REALTY_DATA_DIR/olx/{config.json,logs,runs}
Постійний вхід: olx_refresh.txt — refresh token окремої сесії (вхід в інкогніто лише для бота, див. cookie_hint
у dispatcher/boards.json). З ним access token (живе 15 хв) оновлюється сам.
"""

import argparse
import io
import json
import os
import re
import secrets
import sys
import time
import traceback
import uuid

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [HERE, os.path.join(ROOT, "crm"), os.path.join(ROOT, "common")]

import board_protocol as bp  # noqa: E402

bp.install_print_hook()

import extract_estate_admin as ADM  # noqa: E402
import mapping as M  # noqa: E402
from olx_client import NET_ERRORS, AuthError, OlxClient, OlxError  # noqa: E402
from olx_official import OfficialClient, OlxApiError  # noqa: E402

DATA = os.environ.get("REALTY_DATA_DIR", os.path.join(ROOT, "data"))
ADM.COOKIE_FILE = os.path.join(DATA, "secrets", "crm_admin_cookie.txt")   # «🔑 Вхід» → CRM (адмінка…)
BOARD_DIR = os.path.join(DATA, "olx")
COOKIES = os.path.join(DATA, "secrets", "olx_cookies.txt")
AUTH = os.path.join(DATA, "secrets", "olx_auth.txt")
REFRESH = os.path.join(DATA, "secrets", "olx_refresh.txt")
CONFIG = os.path.join(BOARD_DIR, "config.json")
os.makedirs(BOARD_DIR, exist_ok=True)

DEFAULT_CFG = {
    "device_id": None,            # генерується один раз
    "person": None,               # контактна особа в оголошенні
    "phone": None,
    "email": None,
    "private_business": "business",
    "price_negotiable": True,     # прапорець «Договірна» поруч із ціною (так було у формі)
    "prefer_region_id": 25,       # Київська область — якщо назва населеного пункту не унікальна
}
CRM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"}


# ------------------------------------------------------------------ налаштування
def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    if not cfg.get("device_id"):
        cfg["device_id"] = str(uuid.uuid4())
        save_cfg(cfg)
    return cfg


def save_cfg(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def make_client(cfg, check_token=True):
    c = OlxClient(COOKIES if os.path.exists(COOKIES) else None, cfg["device_id"],
                  log_dir=os.path.join(BOARD_DIR, "logs"), auth_path=AUTH, refresh_path=REFRESH)
    if check_token:
        c.ensure_token()
    return c


def fmt_left(sec):
    if sec is None:
        return "невідомо"
    if sec <= 0:
        return "вже прострочений"
    return f"{sec // 3600} год {sec % 3600 // 60} хв" if sec >= 3600 else f"{sec // 60} хв"


# ------------------------------------------------------------------ питання
def ask_number(label, hint=""):
    while True:
        v = bp.ask_text(f"OLX: вкажіть «{label}» (ціле число){hint}")
        if v is None:
            return None
        n = M.num(v)
        if n:
            return M.to_int_str(n)
        print(f"   «{v}» — не число")


def ask_option(text, options, default=None):
    kind, val = bp.ask_choice(text, options)
    return val if kind == "opt" else default


# ------------------------------------------------------------------ параметри
def set_int(out, code, label, raw, notes, unit_note=""):
    """Ціле число в out[code]: з CRM (raw), а якщо там нема — питаємо."""
    n = M.num(raw)
    if n is None:
        v = ask_number(label, unit_note)
        if not v:
            raise OlxError(f"обов'язкове поле «{label}» не заповнене")
        out[code] = v
        return
    v = M.to_int_str(n)
    if float(v) != n:
        notes.append(f"{label}: {raw} → {v} (OLX приймає лише цілі ≥ 1)")
    out[code] = v


def total_area_raw(estate):
    p, qf = estate.get("parameters") or {}, estate.get("quick_facts") or {}
    return M.get(p, "Площа загальна,м²", "Площадь общая,м²") or (str(qf.get("Площа") or "").split("/")[0] or None)


def walls_and_repair(estate, out):
    walls, raw = M.walls_code(estate)
    if walls is None:
        walls = ask_option(f"OLX: тип стін у CRM «{raw}» — що поставити?",
                           list(M.WALLS_LABELS.items()), M.WALLS_DEFAULT)
    out["house_type"] = walls
    rep, raw = M.repair_code(estate)
    if raw and rep is None:
        rep = ask_option(f"OLX: ремонт у CRM «{raw}» — що поставити? (або пропустити)",
                         list(M.REPAIR_LABELS.items()))
    if rep:
        out["repair"] = rep


def house_params(estate, out, notes):
    p, qf = estate.get("parameters") or {}, estate.get("quick_facts") or {}
    floors_qf = qf.get("Поверх") if "/" not in str(qf.get("Поверх", "")) else None
    set_int(out, "total_floors", "Поверховість", M.get(p, "Поверховість", "Этажность") or floors_qf, notes)
    set_int(out, "total_area", "Загальна площа", total_area_raw(estate), notes, ", м²")
    set_int(out, "number_of_rooms", "Кількість кімнат",
            M.get(p, "Кіл. кімнат", "Кол. комнат") or qf.get("Кіл. кімнат"), notes)
    set_int(out, "land_area", "Площа ділянки",
            M.get(p, "Площа ділянки,соток", "Площадь участка,соток") or qf.get("Ділянка"), notes, ", соток")
    out["property_type_houses"] = M.house_kind(estate)
    walls_and_repair(estate, out)


def flat_params(estate, cid, out, notes):
    p, qf = estate.get("parameters") or {}, estate.get("quick_facts") or {}
    desc = estate.get("description") or ""
    set_int(out, "total_area", "Загальна площа", total_area_raw(estate), notes, ", м²")

    floor, floors = M.split_pair(M.get(p, "Поверх", "Этаж") or qf.get("Поверх"))
    f2 = M.get(p, "Поверховість", "Этажность")
    if f2:
        floors = M.split_pair(f2)[0] or floors
    if not floor or not floors:
        t_floor, t_floors = M.find_floor_in_text(desc)
        floor, floors = floor or t_floor, floors or t_floors
    set_int(out, "floor", "Поверх", floor, notes)
    set_int(out, "total_floors", "Поверховість будинку", floors, notes)

    kitchen = M.num(M.get(p, "Площа кухні,м²", "Площадь кухни,м²"))
    if kitchen:
        set_int(out, "kitchen_area", "Площа кухні", kitchen, notes)
    else:
        sug = M.to_int_str(float(out["total_area"]) * 0.2)
        while "kitchen_area" not in out:
            kind, val = bp.ask_choice(f"OLX: площа кухні обов'язкова, а в CRM її нема. Взяти ≈20% від загальної "
                                      f"({sug} м²) чи напишіть свою", [(sug, f"{sug} м² (20%)")], allow_text=True)
            if kind is None:
                raise OlxError("обов'язкове поле «Площа кухні» не заповнене")
            if M.num(val):
                out["kitchen_area"] = M.to_int_str(M.num(val))
            else:
                print(f"   «{val}» — не число")

    rooms = M.get(p, "Кіл. кімнат", "Кол. комнат") or qf.get("Кіл. кімнат")
    rooms = int(M.num(rooms)) if M.num(rooms) else M.find_rooms_in_text(f"{estate.get('deal_title')}\n{desc}")
    if rooms:
        out["number_of_rooms_string"] = M.rooms_key(rooms)
    if M.has_param(cid, "apartments_object_type"):
        out["apartments_object_type"] = "secondary_market"      # правило: завжди вторинне
    walls_and_repair(estate, out)


def commercial_params(estate, out, notes):
    set_int(out, "total_area", "Загальна площа", total_area_raw(estate), notes, ", м²")
    rep, _ = M.repair_code(estate)
    if rep:
        out["is_repaired"] = M.IS_REPAIRED[rep]


def ensure_required(cid, out):
    """Страховка: кожне обов'язкове поле категорії має бути заповнене — інакше питаємо."""
    for pd in M.required_params(cid):
        code = pd["code"]
        if code in out or pd["type"] == "price":
            continue
        if pd["type"] == "number":
            v = ask_number(pd["label"])
        elif pd.get("values"):
            v = ask_option(f"OLX: оберіть «{pd['label']}»", M.param_values(cid, code))
            v = [v] if v and pd["type"] == "multichoice" else v
        else:
            v = bp.ask_text(f"OLX: вкажіть «{pd['label']}»")
        if not v:
            raise OlxError(f"обов'язкове поле «{pd['label']}» не заповнене")
        out[code] = v


def build_params(estate, cfg, cid, notes):
    out = {f: False for f in M.FLAGS if M.has_param(cid, f)}

    price, cur = M.parse_price(estate.get("price"))
    if not price:
        price = ask_number("Ціна")
        if not price:
            raise OlxError("без ціни OLX не прийме оголошення")
    if not cur:
        cur = ask_option(f"OLX: валюта ціни {price}?", [("USD", "$"), ("UAH", "грн"), ("EUR", "€")], "USD")
    out["price"] = {"currency": cur, "price": price, "arranged": 1 if cfg.get("price_negotiable") else 0}

    fam = M.family_of(cid)
    if fam == M.HOUSE:
        house_params(estate, out, notes)
    elif fam == M.FLAT:
        flat_params(estate, cid, out, notes)
    else:
        commercial_params(estate, out, notes)
    ensure_required(cid, out)
    return out


def describe_params(cid, params):
    """Зведення полів людською мовою (назви й значення — з визначення категорії)."""
    out = []
    for code, v in params.items():
        if code in M.FLAGS or code == "price":
            continue
        pd = M.param_def(cid, code)
        if pd and pd.get("values"):
            v = M.value_label(cid, code, v[0] if isinstance(v, list) else v)
        out.append(f"{pd['label'] if pd else code}: {v}")
    return out


def crm_admin(estate):
    """Категорія/тип/угода з авторизованої сторінки CRM. Best-effort: без cookie чи при помилці — None
    (тоді визначаємо за заголовком CRM, а що не зрозуміли — питаємо)."""
    if not estate.get("estate_id"):
        print("      CRM-адмінка: у презентації нема ID об'єкта — визначаю за заголовком")
        return None
    if not os.path.exists(ADM.COOKIE_FILE):
        print("      CRM-адмінка: cookie нема (🔑 Вхід → CRM) — визначаю за заголовком")
        return None
    try:
        a = ADM.fetch_estate_admin_data(estate["estate_id"])
    except Exception as e:  # noqa: BLE001 — не блокуємо постинг
        print(f"      CRM-адмінка не відповіла ({type(e).__name__}: {str(e)[:150]}) — визначаю за заголовком. "
              f"Якщо cookie протух — оновіть: 🔑 Вхід → CRM")
        return None
    print(f"      CRM-адмінка: категорія «{a.get('category') or '?'}», тип «{a.get('type') or '?'}», "
          f"угода «{a.get('deal') or '?'}»")
    return a


def choose_category(estate, admin=None):
    d = M.detect(estate, admin)
    if d["unsupported"]:
        raise OlxError(f"«{d['deal']} / {d['kind']}»: {d['unsupported']} на OLX поки не підключено")
    oper = d["oper"]
    if not oper:
        oper = ask_option(f"OLX: не зрозумів тип угоди («{d['deal'] or 'порожньо'}»). Що це?",
                          [(M.SALE, "Продаж"), (M.RENT, "Довгострокова оренда")])
        if not oper:
            raise OlxError("не обрано тип угоди")
    cid = M.resolve_category(d, oper, estate)
    if cid is None:
        cid = ask_option(f"OLX: не знаю, куди викласти «{d['kind'] or 'тип не вказано'}». Оберіть категорію OLX",
                         M.category_choices(oper, d["family"], d["category"]))
        if not cid:
            raise OlxError("не обрано категорію OLX")
    return cid


# ------------------------------------------------------------------ населений пункт
def _norm(s):
    return (s or "").casefold().replace("’", "'").replace("ʼ", "'").replace("`", "'").replace("ё", "е").strip()


def _place_label(d):
    c, dist = d.get("city") or {}, d.get("district") or {}
    bits = [c.get("name")]
    if dist.get("name"):
        bits.append(f"{dist['name']} р-н")
    if (d.get("municipality") or {}).get("name") and _norm(d["municipality"]["name"]) != _norm(c.get("name")):
        bits.append(f"{d['municipality']['name']} р-н")
    if (d.get("region") or {}).get("name"):
        bits.append(d["region"]["name"])
    return ", ".join(b for b in bits if b)


def resolve_location(client, estate, cfg):
    city_name, district_name = M.city_and_district(estate.get("address"))
    query = city_name
    for _ in range(4):
        if not query:
            query = bp.ask_text("OLX: не знайшов населений пункт в адресі CRM. Напишіть його назву")
            if not query:
                raise OlxError("не вказано населений пункт")
        results = [d for d in client.location_autocomplete(query) if d.get("city")]
        exact = [d for d in results if _norm(d["city"].get("name")) == _norm(query)]
        city_ids = list(dict.fromkeys(d["city"]["id"] for d in exact))
        if len(city_ids) > 1:
            pref = [cid for cid in city_ids
                    if any((d.get("region") or {}).get("id") == cfg.get("prefer_region_id")
                           for d in exact if d["city"]["id"] == cid)]
            if len(pref) == 1:
                city_ids = pref
        if len(city_ids) == 1:
            cid = city_ids[0]
            break
        uniq = {}
        for d in (exact or results):
            uniq.setdefault(d["city"]["id"], {**d, "district": None})
        options = [(cid, _place_label(d)) for cid, d in uniq.items()]
        why = "кілька однакових назв" if len(city_ids) > 1 else "точного збігу немає"
        kind, val = bp.ask_choice(f"OLX: населений пункт «{query}» — {why}. Оберіть або напишіть іншу назву",
                                  options, allow_text=True)
        if kind == "opt":
            cid = val
            break
        if kind is None:
            raise OlxError("не обрано населений пункт")
        query = val
    else:
        raise OlxError("не вдалося визначити населений пункт")

    entries = [d for d in results if d["city"]["id"] == cid]
    districts = [d for d in entries if d.get("district")]
    chosen = entries[0]
    if districts:
        match = []
        if district_name:
            stem = _norm(district_name)[:5]
            match = [d for d in districts if _norm(d["district"]["name"]).startswith(stem)]
        if len(match) == 1:
            chosen = match[0]
        else:
            chosen = ask_option(f"OLX: оберіть район ({chosen['city']['name']})",
                                [(d, d["district"]["name"]) for d in districts])
            if not chosen:
                raise OlxError("для цього міста OLX вимагає район")

    city = chosen["city"]
    loc = {"city_id": city["id"]}
    lat, lon = city.get("lat"), city.get("lon")
    if chosen.get("district"):
        loc["district_id"] = chosen["district"]["id"]
        lat, lon = chosen["district"].get("lat") or lat, chosen["district"].get("lon") or lon
    if lat is None:
        info = client.city(city["id"])
        lat, lon = info.get("latitude"), info.get("longitude")
    loc.update({"latitude": lat, "longitude": lon, "radius": 1000, "zoom": 12})
    return loc, _place_label(chosen), city.get("name")


# ------------------------------------------------------------------ контакти
def contacts(client, cfg):
    me = {}
    if not (cfg.get("person") and cfg.get("phone") and cfg.get("email")):
        try:
            me = client.me()
        except OlxError:
            me = {}
    changed = False
    for key, label, guess in (("person", "Контактна особа (ім'я в оголошенні)", me.get("name")),
                              ("phone", "Телефон для оголошення", me.get("phone")),
                              ("email", "Email акаунта OLX", me.get("email"))):
        if cfg.get(key):
            continue
        if guess:
            kind, val = bp.ask_choice(f"OLX: {label}", [(guess, str(guess))], allow_text=True)
        else:
            kind, val = ("text", bp.ask_text(f"OLX: {label} (запам'ятаю для наступних оголошень)"))
        if not val:
            raise OlxError(f"не вказано: {label}")
        cfg[key] = str(val).strip()
        changed = True
    if changed:
        save_cfg(cfg)
    return {"person": cfg["person"], "phone": cfg["phone"], "email": cfg["email"]}


# ------------------------------------------------------------------ фото
def download_photo(url):
    r = requests.get(url, headers=CRM_HEADERS, timeout=60)
    r.raise_for_status()
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(r.content))
    rotated = (im.getexif().get(0x0112) or 1) != 1
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    ctype = (r.headers.get("Content-Type") or "").split(";")[0]
    if ctype == "image/jpeg" and not rotated:
        data = r.content
    else:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        data = buf.getvalue()
    return data, im.size


def upload_photos(client, urls):
    images, failed = [], 0
    for i, url in enumerate(urls, 1):
        try:
            data, (w, h) = download_photo(url)
            fn = client.upload_image(data)
            images.append({"filename": fn, "rotation": 0, "width": w, "height": h, "url": client.image_url(fn)})
            print(f"      фото {i}/{len(urls)} → {fn}")
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"      фото {i}/{len(urls)} ПОМИЛКА: {e}")
        time.sleep(0.3)
    return images, failed


# ------------------------------------------------------------------ відповідь OLX
def offer_errors(body):
    """Витягує людські тексти помилок валідації з відповіді OLX (формат точно не відомий)."""
    out = []

    def walk(x, path=""):
        if isinstance(x, dict):
            msg = x.get("detail") or x.get("title") or x.get("message")
            field = x.get("field") or x.get("source") or x.get("code") or path
            if isinstance(msg, str) and msg:
                out.append(f"{field}: {msg}" if field else msg)
            for k, v in x.items():
                if isinstance(v, (dict, list)):
                    walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, path)
    walk(body)
    return list(dict.fromkeys(out))[:15] or [str(body)[:800]]


def offer_id(body):
    if isinstance(body, dict):
        for d in (body.get("data") if isinstance(body.get("data"), dict) else {}, body):
            for k in ("id", "ad_id", "adId", "offer_id"):
                if d.get(k):
                    return d[k], d.get("url")
    return None, None


# ------------------------------------------------------------------ команди
def cmd_post(a):
    cfg = load_cfg()
    client = None if a.dry_run else make_client(cfg)
    report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "url": a.url, "dry_run": a.dry_run}
    try:
        if client:
            client.me()                                     # одразу перевіряємо, що cookie живі

        from extract_estate import fetch_estate_data
        print("[1/5] Читаю CRM…")
        estate = fetch_estate_data(a.url)
        report["crm"] = {k: estate.get(k) for k in ("article", "deal_title", "address", "price", "parameters")}
        print(f"      {estate.get('article')} · {estate.get('deal_title')} · {estate.get('address')} · {estate.get('price')}")
        admin = crm_admin(estate)
        report["crm_admin"] = {k: (admin or {}).get(k) for k in ("category", "type", "deal")}
        cid = choose_category(estate, admin)
        report["category_id"] = cid
        print(f"      Категорія OLX: {M.cat_label(cid)} ({cid})")

        notes = []
        params = build_params(estate, cfg, cid, notes)
        geo_client = client or OlxClient(None, cfg["device_id"])   # dry-run: гео-запити публічні
        print("[2/5] Населений пункт…")
        loc, place, city = resolve_location(geo_client, estate, cfg)
        print(f"      {place}")

        cands = M.title_candidates(estate, cid, params, {"city": city, "street": M.street(estate.get("address")),
                                                         "complex": M.complex_name(estate)})
        # кнопки в боті обрізають текст, тому повні варіанти — в самому питанні
        listing = "\n".join(f"{i}. {t} ({len(t)})" for i, t in enumerate(cands, 1))
        kind, title = bp.ask_choice(f"OLX: заголовок ({M.TITLE_MIN}–{M.TITLE_MAX} символів). "
                                    f"Оберіть або напишіть свій\n{listing}",
                                    [(t, f"{i}. {t}") for i, t in enumerate(cands, 1)], allow_text=True)
        title = (title or cands[0]).strip()
        while not M.title_ok(title):
            t = bp.ask_text(f"OLX: заголовок не підходить ({len(title)} символів). Треба {M.TITLE_MIN}–{M.TITLE_MAX} "
                            f"символів, без смайликів, посилань, email і 4+ розділових знаків підряд. Напишіть інший")
            if not t:
                raise OlxError("заголовок не підходить")
            title = t.strip()

        desc = M.description(estate)
        if len(desc) < M.DESC_MIN:
            t = bp.ask_text(f"OLX: опис закороткий ({len(desc)} символів, треба від {M.DESC_MIN}). Напишіть опис")
            if not t or len(t.strip()) < M.DESC_MIN:
                raise OlxError("опис закороткий")
            desc = t.strip() + ("\n\n" + M.spaced_article(estate.get("article")) if estate.get("article") else "")

        cont = contacts(client, cfg) if client else {k: cfg.get(k) or "?" for k in ("person", "phone", "email")}

        urls = estate.get("photos") or []
        limit = M.max_photos(cid)
        if len(urls) > limit:
            notes.append(f"фото: у CRM {len(urls)}, OLX приймає максимум {limit} — беру перші {limit}")
            urls = urls[:limit]

        payload = {"brand": "olxua", "lang": "uk", "category_id": cid,
                   **{k: loc[k] for k in ("city_id", "district_id") if k in loc},
                   "description": desc, "email": cont["email"], "parameters": params,
                   "person": cont["person"], "phone": cont["phone"], "images": [], "title": title,
                   "private_business": cfg.get("private_business", "business"),
                   "components_data": {"reposting": {"action": "ad_posted", "data": "{\"reposting\":false}"}},
                   **{k: loc[k] for k in ("latitude", "longitude", "radius", "zoom")}}

        summary = [f"Категорія: {M.cat_label(cid)}", f"Заголовок: {title}",
                   f"Ціна: {params['price']['price']} {params['price']['currency']}"
                   + (" (договірна)" if params["price"].get("arranged") else ""),
                   "; ".join(describe_params(cid, params)),
                   f"Місце: {place}", f"Фото: {len(urls)}"] + [f"⚠️ {n}" for n in notes]
        print("\n".join(summary))
        report["payload"] = payload

        if a.dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=1)[:3500])
            bp.result(True, "OLX: перевірка без відправки — дані зібрано (див. вище), нічого не створено")
            return

        kind, ok = bp.ask_choice("OLX: усе вірно? Після «Створити» оголошення з'явиться в кабінеті OLX "
                                 "і чекатиме на оплату розміщення",
                                 [("yes", "✅ Створити на OLX"), ("no", "❌ Скасувати")])
        if ok != "yes":
            bp.result(False, "OLX: скасовано, на OLX нічого не відправлено")
            return

        print(f"[3/5] Фото на OLX ({len(urls)})…")
        images, failed = upload_photos(client, urls)
        if not images:
            raise OlxError("не завантажилось жодне фото — оголошення не створюю")
        payload["images"] = images
        print(f"[3/5] Фото завантажено: {len(images)} з {len(urls)}")

        print("[4/5] Створюю оголошення…")
        status, body = client.create_offer(payload)
        report["response"] = {"status": status, "body": body}
        if status >= 400:
            errs = offer_errors(body)
            raise OlxError(f"OLX не прийняв дані (HTTP {status}):\n• " + "\n• ".join(errs))
        oid, url = offer_id(body)
        report["offer_id"] = oid
        print("[5/5] Готово")
        extra = f" (фото не завантажились: {failed})" if failed else ""
        bp.result(True, f"OLX: оголошення створено (№ {oid or '?'}){extra}. Воно чекає на оплату розміщення — "
                        f"активуйте в кабінеті OLX «Мої оголошення».",
                  url=url or "https://www.olx.ua/uk/myaccount/", id=oid, draft=True)
    finally:
        try:
            os.makedirs(os.path.join(BOARD_DIR, "runs"), exist_ok=True)
            art = ((report.get("crm") or {}).get("article") or "x").lstrip("#")
            path = os.path.join(BOARD_DIR, "runs", time.strftime("%Y%m%d_%H%M%S") + f"_{art}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=1, default=str)
        except Exception:  # noqa: BLE001
            pass


# Спершу — як просить сам сайт (auth0-spa-js додає offline_access); якщо OLX покаже помилку — без scope.
LOGIN_SCOPES = ["openid profile email offline_access", None]


def cmd_login(a):
    """Постійний вхід за посиланням: користувач входить на звичайній сторінці OLX (пароль бачить лише OLX),
    надсилає адресу, яка відкрилась після входу (…/callback/?code=…), — міняємо код на refresh token."""
    cfg = load_cfg()
    client = make_client(cfg, check_token=False)
    for attempt, scope in enumerate(LOGIN_SCOPES, 1):
        verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(16)
        url = client.authorize_url(verifier, state, scope)
        ans = bp.ask_text(
            "🔗 Вхід в OLX для бота" + (f" (спроба {attempt})" if attempt > 1 else "") + ":\n\n"
            f"1) Відкрийте посилання і увійдіть в OLX — логін і пароль вводите на самому сайті OLX, бот їх не бачить:\n"
            f"{url}\n\n"
            "2) Після входу відкриється сторінка OLX (може бути порожня або з помилкою — це нормально). "
            "Скопіюйте ПОВНУ адресу з рядка браузера — вона починається з https://www.olx.ua/d/uk/callback/?code=\n"
            "Якщо адреса вже змінилась — знайдіть її в історії браузера (на комп'ютері Ctrl+H, на телефоні — «Історія»).\n\n"
            "3) Надішліть адресу сюди (повідомлення я одразу видалю). Посилання діє кілька хвилин.\n"
            "Якщо замість входу OLX показав помилку — напишіть «помилка».", secret=True)
        if not ans:
            raise OlxError("вхід скасовано")
        err = re.search(r"[?&#]error=([^&\s#]+)", ans)
        code = re.search(r"[?&#]code=([^&\s#]+)", ans)
        if not code:
            if attempt < len(LOGIN_SCOPES):
                print(f"Без коду{' (' + err.group(1) + ')' if err else ''} — пробую інший варіант посилання")
                continue
            raise OlxError("в адресі нема коду входу (code=…). Перешліть, будь ласка, Claude текст помилки OLX — "
                           "налаштуємо. А поки — ручний варіант: 🔑 Вхід → OLX → «Вставити токен вручну»")
        got_state = re.search(r"[?&#]state=([^&\s#]+)", ans)
        if got_state and got_state.group(1) != state:
            raise OlxError("це адреса від іншого посилання входу — натисніть «Увійти за посиланням» ще раз")
        print("Отримав код, налаштовую вхід…")
        if not client.exchange_code(code.group(1), verifier):
            raise OlxError(f"OLX не видав вхід: {client.refresh_error}. Спробуйте ще раз (посилання одноразове)")
        try:
            me = client.me()
            who = me.get("email") or me.get("name") or me.get("id") or "?"
        except OlxError:
            who = "?"
        bp.result(True, f"OLX: постійний вхід налаштовано ✅ ({who}). Далі бот оновлює його сам.")
        return
    raise OlxError("не вдалося увійти")


def cmd_api_login(a):
    """Офіційне підключення: користувач відкриває посилання, входить на сайті OLX і тисне «Дозволити»;
    OLX веде на нашу приймальну адресу, програма сама забирає код і міняє його на токени."""
    off = OfficialClient(DATA)
    state = secrets.token_urlsafe(32)
    url = off.authorize_url(state)
    # Питання з кнопками, а не info: рядки info у чаті з'являються лише при наступному оновленні, а ми далі чекаємо.
    kind, val = bp.ask_choice(
        "✅ Підключення OLX (офіційне)\n\n"
        "1) Відкрийте посилання й увійдіть у свій акаунт OLX — пароль вводите на сайті OLX, бот його не бачить:\n"
        f"{url}\n\n"
        "2) На сторінці OLX натисніть «Дозволити».\n"
        "3) Поверніться сюди й натисніть кнопку нижче.",
        [("ok", "✅ Я натиснув(ла) «Дозволити»"), ("no", "✖️ Скасувати")])
    if kind != "opt" or val != "ok":
        raise OlxApiError("підключення скасовано")
    code = off.wait_code(state, timeout=150)
    print("Дозвіл отримано, оформлюю доступ…")
    off.exchange_code(code)
    who = "?"
    try:
        st, body = off.get("/users/me")
        d = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
        if st == 200 and isinstance(d, dict):
            who = d.get("name") or d.get("email") or d.get("id") or "?"
        else:
            print(f"/users/me → HTTP {st}: {str(body)[:200]}")
    except (OlxApiError,) + NET_ERRORS as e:
        print(f"/users/me не відповів: {e}")
    bp.result(True, f"OLX: підключено офіційно ✅ ({who}). Токен діє ще {fmt_left(off.expires_in())}, далі бот оновлює його сам.")


def _mask(x):
    """Для файлу з пробою: ховаємо email/телефон (це дані акаунта, у чат їх не треба)."""
    if isinstance(x, dict):
        return {k: ("***" if k.lower() in ("email", "phone", "phone_number", "token", "access_token", "refresh_token")
                    and isinstance(v, (str, int)) else _mask(v)) for k, v in x.items()}
    if isinstance(x, list):
        return [_mask(v) for v in x]
    return x


def _brief(x, n=500):
    t = json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x
    return t if len(t) <= n else t[:n] + f"… (+{len(t) - n} симв.)"


def _find_named(x, pat, out, depth=0):
    """Усі словники, де в якомусь рядковому полі name/label є pat (для пошуку «Нерухомість» у дереві категорій)."""
    if depth > 8:
        return
    if isinstance(x, dict):
        names = [v for k, v in x.items() if k in ("name", "label", "title") and isinstance(v, str)]
        if any(re.search(pat, n, re.I) for n in names) and "id" in x:
            out.append({"id": x.get("id"), "name": names[0], "parent_id": x.get("parent_id")})
        for v in x.values():
            _find_named(v, pat, out, depth + 1)
    elif isinstance(x, list):
        for v in x:
            _find_named(v, pat, out, depth + 1)


def _probe_round2(off, call, report):
    """Раунд 2: пошук міст/районів, пакети за категорією, окреме оголошення, статуси. Лише GET."""
    for prm in ({"name": "Київ"}, {"query": "Київ"}, {"q": "Київ"}, {"city_name": "Київ"}, {"limit": 5, "offset": 5}):
        st, b = call("/cities", prm, show=False, trim=5)
        n = (b or {}).get("_total_items") if isinstance(b, dict) else None
        tot = len(b.get("data") or []) if isinstance(b, dict) and isinstance(b.get("data"), list) else "?"
        first = ((b.get("data") or [{}])[0].get("name") if isinstance(b, dict) and b.get("data") else None)
        print(f"/cities {prm} → HTTP {st}, елементів {n or tot}, перший: {first}")
    for path in ("/cities/268", "/cities/268/districts", "/districts", "/regions", "/categories/1758"):
        call(path, {"city_id": 268} if path == "/districts" else None, trim=10)
    for cid in (1758, 1602, 1760):
        call("/packets", {"category_id": cid}, trim=10)
    st, ads = call("/adverts", {"limit": 50}, show=False, trim=0)
    if st == 200 and isinstance(ads, dict) and isinstance(ads.get("data"), list):
        from collections import Counter
        print("оголошень у відповіді: " + str(len(ads["data"])) + "; за статусами: "
              + ", ".join(f"{k}={v}" for k, v in Counter(x.get("status") for x in ads["data"]).items()))
        if ads["data"]:
            call(f"/adverts/{ads['data'][0]['id']}", trim=0, show=False)
    for k, v in report.items():
        if k.startswith(("/adverts/", "/regions", "/districts", "/cities/", "/packets")):
            print(f"{k} → {v.get('status', 'помилка')}")
    path = os.path.join(BOARD_DIR, "api_probe2.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    bp.result(True, "OLX API: розвідку (раунд 2) виконано (лише читання). Файл — нижче, перешліть його Claude.",
              files=[{"path": path, "name": "olx_api_probe2.json"}])


def cmd_api_probe(a):
    """Розвідка офіційного API на живому токені: нічого не створює й не змінює (лише GET).
    Усі відповіді складає в один JSON-файл і надсилає в чат — по ньому пишемо публікацію через офіційний API."""
    off = OfficialClient(DATA)
    off.ensure_token()
    report = {}

    def call(path, params=None, show=True, trim=0):
        key = path + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
        try:
            st, body = off.get(path, params)
        except (OlxApiError,) + NET_ERRORS as e:
            report[key] = {"error": f"{type(e).__name__}: {e}"}
            print(f"GET {key} → помилка: {type(e).__name__}: {str(e)[:150]}")
            return None, None
        b = _mask(body)
        if trim and isinstance(b, dict) and isinstance(b.get("data"), list) and len(b["data"]) > trim:
            b = {**b, "data": b["data"][:trim], "_total_items": len(b["data"])}     # у файл — лише початок списку
        report[key] = {"status": st, "body": b}
        if show:
            print(f"GET {key} → HTTP {st}: {_brief(_mask(body), 260)}")
        return st, body

    if getattr(a, "round", 1) == 2:
        return _probe_round2(off, call, report)
    call("/users/me")
    st, cats = call("/categories", show=False)
    found = []
    if st == 200:
        _find_named(cats, r"нерухом|недвиж|real.?estate", found)
        print(f"/categories → HTTP 200; категорій «нерухомість»: {len(found)}: {_brief(found[:5], 400)}")
    else:
        print(f"/categories → HTTP {st}: {_brief(_mask(cats), 300)}")
    for f in found[:2]:
        call("/categories", {"parent_id": f["id"]}, show=False)
    ids = []
    for cid in (1758, 1760, 1602, 330, 1612, 1614):       # ідентифікатори з сайту — перевіримо, чи збігаються з офіційними
        st, body = call(f"/categories/{cid}/attributes", show=False)
        ids.append(f"{cid}:{st}")
    print("атрибути категорій (id з сайту): " + ", ".join(ids))
    call("/adverts", {"limit": 3})
    for p in ("/users/me/packets", "/users/me/thresholds", "/packets", "/thresholds", "/cities"):
        call(p, show=False)
    print("Інші шляхи (пакети/ліміти/міста): " + ", ".join(
        f"{k} → {v.get('status', 'помилка')}" for k, v in report.items()
        if k.startswith(("/users/me/", "/packets", "/thresholds", "/cities"))))
    os.makedirs(BOARD_DIR, exist_ok=True)
    path = os.path.join(BOARD_DIR, "api_probe.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    bp.result(True, "OLX API: розвідку виконано (лише читання). Файл із відповідями — нижче, перешліть його Claude.",
              files=[{"path": path, "name": "olx_api_probe.json"}])


def cmd_keepalive(a):
    """Оновлює постійний вхід (і заразом не дає йому «заснути»). Без refresh token — нічого не робить."""
    off = OfficialClient(DATA)
    if off.connected():
        try:
            off.refresh()
        except (OlxApiError,) + NET_ERRORS as e:
            bp.result(False, f"OLX: офіційний доступ не оновився ({str(e)[:200]}). Підключіть заново: 🔑 Вхід → OLX → "
                             f"✅ Підключити (офіційно)")
            return
        bp.result(True, f"OLX: офіційний доступ оновлено, токен діє ще {fmt_left(off.expires_in())}")
        return
    cfg = load_cfg()
    client = make_client(cfg, check_token=False)
    if not client.has_refresh():
        bp.result(True, "OLX: постійного входу нема — пропускаю", skipped=True)
        return
    if not client.refresh_oauth():
        bp.result(False, f"OLX: постійний вхід не оновився ({client.refresh_error}). Якщо повториться — "
                         f"увійдіть в OLX в інкогніто і надішліть новий refresh token: 🔑 Вхід → OLX")
        return
    bp.result(True, f"OLX: вхід оновлено, токен діє ще {fmt_left(client.token_expires_in())}")


def cmd_check(a):
    cfg = load_cfg()
    client = make_client(cfg, check_token=False)
    if client.has_refresh():
        ok = client.refresh_oauth()
        print("Постійний вхід (refresh token): " + ("працює ✅" if ok else f"НЕ працює ❌ ({client.refresh_error})"))
    else:
        print("Постійний вхід (refresh token): не задано — токен доведеться вставляти вручну кожні 15 хв")
    d = client.describe()
    names = d["cookie_names"]
    keyish = [n for n in names if any(x in n.lower() for x in ("token", "refresh", "auth", "session", "sid"))]
    print(f"Cookie: {len(names)} шт." + (f" (з них про вхід: {', '.join(keyish)})" if keyish else ""))
    if d["token"]:
        t = d["token"]
        print(f"Токен ({d.get('source')}): {t.get('token_use') or '?'}, видавець {t.get('iss') or '?'}, "
              f"дійсний ще {fmt_left(d['expires_in'])}")
    else:
        print("Токена (Authorization: Bearer …) не знайшов")
    print("Зв'язок із OLX: " + "; ".join(client.probe()))
    rep = client.token_report() if not client.has_refresh() else []
    for x in rep:
        print(f"• токен з {x['src']}: видавець …{x['iss']}, тип {x['use'] or '?'}, діє ще {x['left_min']} хв "
              f"(всього живе {x['life_min']} хв) → users/me: HTTP {x['users_me']}")
    if not rep and not client.has_refresh():
        print("• жодного токена (eyJ…) не знайшов ні в Authorization, ні в cookie")
    good = [x for x in rep if x["users_me"] == 200]
    if good:
        client.use_token(good[0]["token"], good[0]["src"])
    client.ensure_token()
    me = client.me()
    who = me.get("email") or me.get("name") or me.get("id") or "?"
    bp.result(True, f"OLX: вхід працює ({who}). Токен дійсний ще: {fmt_left(client.token_expires_in())}")


def finish_error(e):
    if isinstance(e, AuthError):
        bp.result(False, f"OLX: немає входу — {e}.\nНалаштуйте постійний вхід: кнопка «🔑 Вхід» → OLX "
                         f"(там інструкція), або разово надішліть рядок Authorization (Bearer …)")
    elif isinstance(e, NET_ERRORS):
        bp.result(False, f"OLX не відповідає ({type(e).__name__}: {str(e)[:200]}). Спробуйте ще раз за кілька хвилин.")
    elif isinstance(e, OlxApiError):
        bp.result(False, f"OLX (офіційний доступ): {e}")
    elif isinstance(e, (OlxError, SystemExit)):
        bp.result(False, f"OLX: {e}")
    else:
        bp.info(traceback.format_exc()[-1500:])
        bp.result(False, f"OLX: непередбачена помилка {type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("post")
    p.add_argument("--url", required=True)
    p.add_argument("--techpassport", nargs="*", default=[])   # для сумісності з ботом; OLX не потребує
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("check")
    sub.add_parser("keepalive")
    sub.add_parser("login")
    sub.add_parser("api-login")
    pp = sub.add_parser("api-probe")
    pp.add_argument("--round", type=int, default=1)
    a = ap.parse_args()
    try:
        {"post": cmd_post, "check": cmd_check, "keepalive": cmd_keepalive, "login": cmd_login,
         "api-login": cmd_api_login, "api-probe": cmd_api_probe}[a.cmd](a)
    except BaseException as e:  # noqa: BLE001
        finish_error(e)
    finally:
        bp.flush_info()


if __name__ == "__main__":
    main()
