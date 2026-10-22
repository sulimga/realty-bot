"""
Автопостинг оголошень з RealtSoft CRM на dom.ria.com (DIM.RIA).

Команди:
    # перевірити, що cookie живі
    python3 post_domria.py check

    # подивитись, що буде відправлено (нічого не створює на dom.ria)
    python3 post_domria.py post "<посилання на презентацію CRM>" --dry-run

    # створити оголошення (чернетка з фото, без публікації)
    python3 post_domria.py post "<посилання>"

    # створити + додати техпаспорт + безкоштовно опублікувати
    python3 post_domria.py post "<посилання>" --techpassport tehpassport.pdf

    # додати техпаспорт до вже створеного оголошення (і опублікувати)
    python3 post_domria.py techpassport 35054441 tehpassport.pdf
    #   кілька файлів (напр. фото сторінок):  ... techpassport 35054441 p1.jpg p2.jpg
    #   без публікації:                         ... --draft
"""

import argparse
import io
import json
import mimetypes
import os
import re
import sys
import time

import requests

from extract_estate import fetch_estate_data, extract_estate_data, HEADERS as CRM_HEADERS
from domria_client import DomRiaClient, DomRiaError, AuthError, _now_iso, PARTNER_ID
from mapping import (detect_deal, build_characteristics, describe, PARENT_BY_TYPE,
                     TYPE_NAMES, dict_for, find_cadastral_in_text)

# Типи "ділянка" (dom.ria): 25 — під забудову, 26 — комерційна, 27 — с/г.
LAND_TYPES = {25, 26, 27}

HERE = os.path.dirname(os.path.abspath(__file__))
POSTED_DIR = os.path.join(HERE, "posted")

DEFAULT_CONFIG = {
    "user_id": 9252610,
    "cookies_file": "cookies.txt",
    "commission": 5,                       # % — як у вашому ручному оголошенні; null щоб не вказувати
    "offer_type_label": "від посередника",
    "append_article": True,                # дописувати артикул CRM в кінець опису (A35511)
    "max_photos": 50,
    "default_tags": [],                    # напр. [1822] = «Торг можливий»
    "default_commercial_object_type": 1974,
    "default_walls": "цегла",              # тип стін, якщо в CRM не вказано
    "pause_between_photos": 0.4,
}


def load_config():
    path = os.path.join(HERE, "config.json")
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def load_cookies(cfg):
    path = cfg["cookies_file"]
    if not os.path.isabs(path):
        path = os.path.join(HERE, path)
    if not os.path.exists(path):
        sys.exit(f"Немає файлу з cookie: {path}\nДив. README — як його отримати.")
    raw = open(path, encoding="utf-8").read().strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    return raw


def make_client(cfg, need_auth=True):
    cookie = load_cookies(cfg) if need_auth else ""
    return DomRiaClient(cookie, cfg["user_id"], log_dir=os.path.join(HERE, "logs"))


# ---------------------------------------------------------------- гео
HOUSE_RE = re.compile(r"(?:^|[,\s])(?:буд\.?|д\.?|будинок|дом)?\s*(\d+[а-яіїєa-z]?(?:\s*/\s*\d+[а-яіїєa-z]?)?)\s*$",
                      re.IGNORECASE)


def parse_house_number(address):
    if not address:
        return None
    parts = [p.strip() for p in address.split(",") if p.strip()]
    for part in reversed(parts):
        m = HOUSE_RE.search(part)
        if m and not re.search(r"(р-н|район|область|обл\.)", part, re.I):
            return m.group(1).replace(" ", "")
    return None


STREET_TYPES = [
    # (як пишуть у CRM, як називає dom.ria)
    (r"вул\.?|вулиця|ул\.?|улица", "вулиця"),
    (r"просп\.?|проспект|пр-т|пр\.", "проспект"),
    (r"пров\.?|провулок|пер\.?|переулок", "провулок"),
    (r"бульв\.?|бульвар|б-р", "бульвар"),
    (r"пл\.?|площа|площадь", "площа"),
    (r"наб\.?|набережна|набережная", "набережна"),
    (r"шосе|шоссе|ш\.", "шосе"),
    (r"узвіз|спуск", "узвіз"),
    (r"тупик", "тупик"),
]
DISTRICT_RE = re.compile(r"(?<![а-яіїєґa-z])(р-н|район|область|області|обл\.|ЖК|ж/м|масив|мкр\.?|мікрорайон|КМ|ОТГ|громада)(?![а-яіїєґa-z])", re.I)


def _street_core(name):
    """'вул. Болсунівська' -> ('вулиця', 'болсунівська')"""
    s = (name or "").strip()
    s_type = None
    for pat, t in STREET_TYPES:
        m = re.match(rf"^\s*(?:{pat})\s+", s, re.I) or re.search(rf"\s+(?:{pat})\s*$", s, re.I)
        if m:
            s_type = t
            s = (s[:m.start()] + s[m.end():]).strip()
            break
    s = re.sub(r"\(.*?\)", "", s)          # «(Мічуріна)» — стара назва
    s = s.lower().replace("’", "'").replace("ё", "е").strip(" .")
    return s_type, s


def _street_names(label):
    """Усі назви вулиці з dom.ria: нова + стара в дужках.
    'вулиця Юлії Здановської (Михайла Ломоносова)' -> ['юлії здановської', 'михайла ломоносова', 'ломоносова']"""
    _, main = _street_core(label)
    names = [main]
    for old in re.findall(r"\((.*?)\)", label or ""):
        o = old.lower().strip()
        names.append(o)
        names.append(o.split()[-1])           # «Михайла Ломоносова» -> «ломоносова»
    return [n for n in names if n]


CITY_PREFIX_RE = re.compile(r"^(м\.|місто|г\.|город|смт|с\.|село|сел\.|пгт|селище)\s*", re.I)


def _is_street_part(p):
    return any(re.match(rf"^\s*(?:{pat})\s+", p, re.I) or re.search(rf"\s+(?:{pat})\s*$", p, re.I)
               for pat, _ in STREET_TYPES)


def split_address(address):
    """
    'Київ, Печерський район, вул. Болсунівська, 23'           -> ('Київ', 'вул. Болсунівська', '23')
    'Києво-Святошинський район, Гатне, вул. Доброго Дуба'     -> ('Гатне', 'вул. Доброго Дуба', None)
    Частини з «район», «область», «ЖК» пропускаються; місто — перша частина, що лишилась.
    """
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    house = parse_house_number(address)
    rest = []
    for part in parts:
        if DISTRICT_RE.search(part):
            continue
        p = part
        if house and p.replace(" ", "").endswith(house):
            p = p[: len(p) - len(house)].rstrip(" ,") if p.endswith(house) else re.sub(r"\s*\d.*$", "", p)
        p = p.strip()
        if re.search(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]{3,}", p) and not re.fullmatch(r"(буд\.?|д\.?)\s*", p, re.I):
            rest.append(p)
    city, street = "", None
    for p in rest:
        if not city and not _is_street_part(p):
            city = CITY_PREFIX_RE.sub("", p).strip()
        elif _is_street_part(p) or street is None:
            street = p
    return city, street, house


def _similar(a, b):
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


PREFERRED_STATE_ID = 10   # Київська область — серед однакових назв сіл беремо її


def find_city(client, city_name, prefer_state=PREFERRED_STATE_ID, asker=None):
    """Тільки ТОЧНИЙ збіг назви. Якщо однакових кілька — Київська область.
    Якщо точного немає — питає зі списку (або None без питань)."""
    res = client.autocomplete_city(city_name) or []
    norm = lambda x: re.sub(r"\s*\(.*?\)", "", (x or "")).replace("’", "'").strip().lower()
    exact = [r for r in res if norm(r.get("label")) == norm(city_name)]
    for r in exact:
        if (r.get("payload") or {}).get("stateId") == prefer_state:
            return r
    if len(exact) == 1:
        return exact[0]
    cands = exact or res
    if asker and asker.enabled and cands:
        print(f"\n  Населений пункт «{city_name}» — оберіть на dom.ria:")
        kind, val = asker.choose([(r, r.get("output") or r.get("label")) for r in cands[:10]], "населений пункт")
        if kind == "opt":
            return val
    return None


def find_street(client, city_id, crm_street, near=None):
    """Повертає (streetId, назва, payload) або (None, None, кандидати)."""
    s_type, core = _street_core(crm_street)
    if not core:
        return None, None, []
    found = {}
    # пошук за повною назвою і за коротким початком (CRM і dom.ria по-різному пишуть: -івська/-овська)
    for q in (core, core[:6]):
        for r in client.autocomplete_street(city_id, q) or []:
            found[r["payload"]["streetId"]] = r
    scored = []
    for r in found.values():
        r_type, _ = _street_core(r["label"])
        score = max(_similar(core, n) for n in _street_names(r["label"]))
        if s_type and r_type and s_type != r_type:
            score -= 0.3
        if near and r["payload"].get("lat"):
            d = abs(float(r["payload"]["lat"]) - near[0]) + abs(float(r["payload"]["lng"]) - near[1])
            score += 0.05 if d < 0.05 else 0
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    if scored and scored[0][0] >= 0.75:
        r = scored[0][1]
        return r["payload"]["streetId"], r["label"], r
    uniq, seen = [], set()
    for _, r in scored:
        if r["label"] not in seen:
            seen.add(r["label"])
            uniq.append(r)
    return None, None, uniq[:6]


# ---------------------------------------------------------------- запитання в консолі
class Asker:
    """Питає в консолі, як заповнити те, що програма не змогла визначити сама.
    Enter без відповіді — пропустити. Якщо enabled=False (--no-ask, Telegram-бот) — не питає."""

    def __init__(self, enabled=True):
        self.enabled = bool(enabled) and sys.stdin is not None and sys.stdin.isatty()

    def text(self, prompt):
        if not self.enabled:
            return None
        try:
            v = input(f"  → {prompt}: ").strip()
        except EOFError:
            return None
        return v or None

    def choose(self, options, prompt="ваш вибір", allow_text=False):
        """options: [(значення, підпис)]. Повертає ('opt', значення) / ('text', рядок) / (None, None)."""
        if not self.enabled:
            return None, None
        for i, (_, label) in enumerate(options, 1):
            print(f"     {i:>2}. {label}")
        hint = "номер зі списку" + (" або нова назва" if allow_text else "") + ", Enter — пропустити"
        while True:
            v = self.text(f"{prompt} ({hint})")
            if v is None:
                return None, None
            if v.isdigit() and 1 <= int(v) <= len(options):
                return "opt", options[int(v) - 1][0]
            if allow_text and not v.isdigit():
                return "text", v
            print(f"     Введіть число від 1 до {len(options)}")


def ask_characteristics(L, asker):
    """Допитує поля, які не вдалося заповнити з CRM: спершу ті, де значення з CRM
    не підійшло до списку dom.ria, потім обов'язкові порожні."""
    if not asker.enabled or not L["dict_key"]:
        return
    from mapping import CHARS, parse_number
    d, cv = CHARS[L["dict_key"]], L["cv"]
    todo = [(c, txt) for c, txt in L.get("unmatched", []) if c not in cv]
    todo += [(c, None) for c, meta in d.items()
             if meta.get("r") and c not in cv and c not in [t[0] for t in todo]]
    if not todo:
        return
    print("\nНе вдалося заповнити з CRM — вкажіть вручну (Enter — пропустити):")
    answered = set()
    for c, crm_text in todo:
        meta = d[c]
        req = " (обов'язкове)" if meta.get("r") else ""
        print(f"\n  «{meta['l']}»{req}" + (f" — у CRM: «{crm_text}»" if crm_text else " — у CRM немає"))
        if meta.get("o"):
            opts = [(int(k), v) for k, v in meta["o"].items()]
            kind, val = asker.choose(opts)
            if kind == "opt":
                cv[c] = val
                answered.add(meta["l"])
        else:
            while True:
                v = asker.text("значення")
                if v is None:
                    break
                if "кадастр" in meta["l"].lower():
                    cv[c] = v
                    answered.add(meta["l"])
                    break
                n = parse_number(v)
                if n is not None:
                    cv[c] = n
                    answered.add(meta["l"])
                    break
                print("     Потрібне число")
        if meta["l"] not in answered and meta.get("r"):
            print("     ⚠ залишиться порожнім — заповніть у кабінеті dom.ria перед публікацією")
    L["warnings"] = [w for w in L["warnings"] if not any(f"«{a}»" in w for a in answered)]


# Що обов'язково для якої категорії — так само, як перевіряє форма dom.ria:
#   номер будинку — тільки квартири (1) і кімнати (40);
#   вулиця — квартири, кімнати, будинки (4), комерція (13).
HOUSE_REQUIRED = {1, 40}
STREET_REQUIRED = {1, 40, 4, 13}


def resolve_geo(client, estate, parent, street_override=None, house_override=None, asker=None):
    """
    Місто, вулиця і номер будинку — з тексту адреси CRM.
    Координати з CRM НЕ використовуються: точку на карті ставить сама dom.ria
    за вулицею/будинком (той самий запит, що робить форма після вибору вулиці).
    """
    address = estate.get("address", "")
    city_name, crm_street, house = split_address(address)
    crm_street = street_override or crm_street
    house = house_override or house

    asker = asker or Asker(False)

    # --- місто
    city = find_city(client, city_name, asker=asker) if city_name else None
    while not city:
        print(f"\n  Не знайшов місто «{city_name}» на dom.ria (адреса CRM: {address})")
        city_name = asker.text("назва міста/села (Enter — скасувати)")
        if not city_name:
            raise DomRiaError(f"Не знайшов місто на dom.ria (адреса CRM: {address}). Оголошення НЕ створено")
        city = find_city(client, city_name, asker=asker)
    city_id = city["payload"]["cityId"]
    geo = {"state_id": city["payload"]["stateId"], "city_id": city_id,
           "city_name": city.get("output") or city["label"]}

    # --- вулиця
    street_id, extra = None, []
    if crm_street:
        street_id, street_label, extra = find_street(client, city_id, crm_street)
    while not street_id and (crm_street or parent in STREET_REQUIRED) and asker.enabled:
        req = "обов'язкова" if parent in STREET_REQUIRED else "не обов'язкова"
        if crm_street:
            print(f"\n  Не знайшов на dom.ria вулицю «{crm_street}» у місті {geo['city_name']} ({req}).")
        else:
            print(f"\n  В адресі CRM «{address}» немає вулиці ({req}).")
        if extra:
            print("  Схожі на dom.ria:")
        kind, val = asker.choose([(r, r["label"]) for r in extra], "вулиця", allow_text=True) if extra \
            else ("text", asker.text("назва вулиці (Enter — пропустити)"))
        if kind == "opt":
            street_id, street_label = val["payload"]["streetId"], val["label"]
        elif kind == "text" and val:
            crm_street = val
            street_id, street_label, extra = find_street(client, city_id, crm_street)
            if not street_id and len(extra) == 1:
                street_id, street_label = extra[0]["payload"]["streetId"], extra[0]["label"]
        else:
            break
    if street_id:
        geo["street_id"], geo["street_name"] = street_id, street_label
    if not street_id and parent in STREET_REQUIRED:
        if crm_street:
            raise DomRiaError(
                f"Не знайшов на dom.ria вулицю «{crm_street}» у місті {geo['city_name']}.\n"
                f"  Схожі: {', '.join(r['label'] for r in extra) or 'нічого'}\n"
                f"  Вкажіть точну назву, як на dom.ria: --street \"<назва>\" (оголошення НЕ створено)")
        raise DomRiaError(f"В адресі CRM «{address}» немає вулиці, а для цього типу вона обов'язкова.\n"
                          f"  Вкажіть: --street \"вул. Назва\" (оголошення НЕ створено)")

    # --- номер будинку (якщо є)
    if not house and parent in HOUSE_REQUIRED:
        print(f"\n  Для квартири потрібен номер будинку, а в адресі CRM «{address}» його немає.")
        house = asker.text("номер будинку (Enter — скасувати)")
    if not house and parent in HOUSE_REQUIRED:
        raise DomRiaError(f"Для квартири потрібен номер будинку, а в адресі CRM «{address}» його немає.\n"
                          f"  Вкажіть: --house 23 (оголошення НЕ створено)")
    if house and street_id:
        geo["building_number_str"] = str(house)

    # --- точка на карті: її дає dom.ria за вулицею/будинком
    if street_id:
        pt = client.point_by_geo(city_id, street_id, house or "",
                                 f"{city_name} ,{geo['street_name']}" + (f", {house}" if house else ""))
        if pt and pt.get("latitude"):
            geo["latitude"] = f'{float(pt["latitude"]):.8f}'
            geo["longitude"] = f'{float(pt["longitude"]):.8f}'
            if pt.get("from") == "ENTITY":
                if pt.get("osmBuildingId"):
                    geo["osm_building_id"] = pt["osmBuildingId"]
                # район і метро — тільки коли dom.ria знає точний будинок
                try:
                    g = client.geo_by_point(pt["latitude"], pt["longitude"]) or {}
                except Exception as e:          # район/метро не обов'язкові — dom.ria підставить сама
                    print(f"  ⚠ dom.ria не віддала район/метро ({type(e).__name__}) — без них")
                    g = {}
                if g.get("area"):
                    geo["district_id"] = g["area"]["areaID"]
                    geo["district_type_id"] = 1
                    geo["district_name"] = g["area"]["name"]
                if g.get("metro"):
                    geo["metro_station_id"] = g["metro"]["stationID"]
                    geo["metro_station_name"] = g["metro"]["name"]
    return geo


# ---------------------------------------------------------------- фото
def download_photo(url):
    r = requests.get(url, headers=CRM_HEADERS, timeout=60)
    r.raise_for_status()
    data = r.content
    ctype = r.headers.get("Content-Type", "").split(";")[0] or "image/jpeg"
    if ctype != "image/jpeg":
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data)).convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
            data, ctype = buf.getvalue(), "image/jpeg"
        except Exception:
            pass
    return data, ctype


# ---------------------------------------------------------------- записи
def save_record(realty_id, record):
    os.makedirs(POSTED_DIR, exist_ok=True)
    path = os.path.join(POSTED_DIR, f"{realty_id}.json")
    old = {}
    if os.path.exists(path):
        old = json.load(open(path, encoding="utf-8"))
    old.update(record)
    json.dump(old, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def load_record(realty_id):
    path = os.path.join(POSTED_DIR, f"{realty_id}.json")
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return None


# ---------------------------------------------------------------- команди
def cmd_check(args, cfg):
    client = make_client(cfg)
    bal = client.check_auth()
    print("Cookie працюють. Відповідь балансу:", json.dumps(bal, ensure_ascii=False)[:300])


def build_listing(client, estate, cfg, street=None, house=None, asker=None):
    op, rt = detect_deal(estate.get("deal_title", ""))
    if not rt:
        raise DomRiaError(f"Не зміг визначити тип об'єкта з «{estate.get('deal_title')}»")
    if op == 1 and rt in LAND_TYPES and not find_cadastral_in_text(estate.get("description")):
        raise DomRiaError(
            "Пропущено: у описі CRM немає кадастрового номера ділянки "
            "(формат 3220882200:03:005:1306). Оголошення НЕ створено.")
    parent = PARENT_BY_TYPE[rt]
    cv, warnings, dkey, leftovers, unmatched = build_characteristics(estate, rt, op, cfg)
    geo = resolve_geo(client, estate, parent, street, house, asker)

    desc = (estate.get("description") or "").strip()
    article = (estate.get("article") or "").lstrip("#")
    if cfg.get("append_article") and article and article not in desc:
        desc = f"{desc}\n\n{article}".strip()

    base = {
        "add_form_version": 3, "partner_id": PARTNER_ID, "is_show_building_no": 1,
        "is_show_flat_no": 1, "radius_location": "0", "advert_type_id": op,
        "start_time": _now_iso(), "is_not_ready_to_publish": 1,
        "user_id": int(cfg["user_id"]), "isSuperUser": 0,
        "realty_type_parent_id": parent, "realty_type_id": rt, "number_not_assigned": 0,
        "state_id": geo["state_id"], "city_id": geo["city_id"], "city_name": geo["city_name"],
        "isNewModel": True,
    }
    return {
        "op": op, "rt": rt, "parent": parent, "geo": geo, "cv": cv, "dict_key": dkey,
        "warnings": warnings, "leftovers": leftovers, "unmatched": unmatched,
        "description": desc, "base": base,
    }


def print_summary(estate, L):
    op_name = {1: "продаж", 3: "оренда", 4: "подобова оренда"}[L["op"]]
    g = L["geo"]
    print(f"\nОб'єкт CRM {estate.get('article')}  ({estate.get('deal_title')})")
    print(f"  dom.ria: {op_name} / {TYPE_NAMES.get(L['rt'], L['rt'])} (type {L['rt']}, parent {L['parent']})")
    print(f"  Адреса:  {g['city_name']}, {g.get('street_name', 'без вулиці')}, {g.get('building_number_str', 'без номера')}"
          f"  | район: {g.get('district_name', '-')} | метро: {g.get('metro_station_name', '-')}")
    print(f"  CRM:     {estate.get('address')}")
    print(f"  Фото:    {estate.get('photos_count', 0)}")
    print("  Характеристики:")
    print(describe(L["cv"], L["dict_key"]))
    if L["warnings"]:
        print("  ⚠ Попередження:")
        for w in L["warnings"]:
            print("    -", w)
    if L["leftovers"]:
        print("  Параметри CRM, які не пішли в dom.ria:")
        for x in L["leftovers"]:
            print("    ·", x)


RUNS_DIR = os.path.join(HERE, "runs")


def write_run_report(report):
    """Звіт по кожному запуску (і dry-run, і невдалому) — для аналізу всіх категорій разом."""
    os.makedirs(RUNS_DIR, exist_ok=True)
    art = (report.get("crm", {}).get("article") or "noart").lstrip("#")
    name = f"{time.strftime('%Y%m%d_%H%M%S')}_{art}_{report.get('mode', 'run')}.json"
    with open(os.path.join(RUNS_DIR, name), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return name


def cmd_post(args, cfg):
    report = {"mode": "dryrun" if args.dry_run else ("publish" if args.techpassport else "draft"),
              "args": {k: v for k, v in vars(args).items() if k != "func"}}
    try:
        _cmd_post(args, cfg, report)
    except BaseException as e:
        report["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        try:
            name = write_run_report(report)
            print(f"\nЗвіт: runs/{name}")
        except Exception:
            pass


def _cmd_post(args, cfg, report):
    src = args.source
    if src.startswith("http"):
        estate = fetch_estate_data(src)
    else:
        estate = extract_estate_data(open(src, encoding="utf-8").read())
        estate["source_url"] = src
    report["crm"] = {k: estate.get(k) for k in ("source_url", "article", "deal_title", "address", "price",
                                                 "quick_facts", "parameters", "photos_count")}

    client = make_client(cfg, need_auth=not args.dry_run)
    asker = Asker(not args.no_ask)
    L = build_listing(client, estate, cfg, args.street, args.house, asker)
    ask_characteristics(L, asker)
    print_summary(estate, L)
    from mapping import CHARS
    dct = CHARS.get(L["dict_key"] or "", {})
    report["domria"] = {
        "advert_type_id": L["op"], "realty_type_id": L["rt"], "realty_type_parent_id": L["parent"],
        "dict_key": L["dict_key"], "geo": L["geo"],
        "characteristics": {c: {"label": dct.get(c, {}).get("l"), "value": v,
                                "value_label": (dct.get(c, {}).get("o") or {}).get(str(v))}
                            for c, v in L["cv"].items()},
        "required_missing": [f"{c} {m['l']}" for c, m in dct.items() if m.get("r") and c not in L["cv"]],
        "warnings": L["warnings"], "crm_not_used": L["leftovers"], "unmatched": L.get("unmatched"),
    }

    if args.dry_run:
        print("\n--dry-run: нічого не відправлено.")
        return

    client.check_auth()
    base, geo = L["base"], L["geo"]

    # 1. Створюємо чернетку → realty_id
    realty_id, _ = client.save_realty(base, step="geo")
    print(f"\n[1/5] Чернетку створено: realty_id = {realty_id}")
    report["realty_id"] = realty_id
    save_record(realty_id, {"crm": estate.get("source_url"), "article": estate.get("article"),
                            "geo": geo, "parent": L["parent"], "rt": L["rt"], "op": L["op"],
                            "status": "draft_created"})

    # 2. Геоданні
    geo_payload = dict(base, realty_id=realty_id, isGeoStep=True)
    geo_payload.update({k: v for k, v in geo.items() if k not in ("state_id", "city_id", "city_name")})
    client.save_realty(geo_payload, step="geo")
    print("[2/5] Адресу збережено")

    # 3. Фото
    photo_ids = []
    photos = (estate.get("photos") or [])[: int(cfg["max_photos"])]
    for i, url in enumerate(photos, 1):
        try:
            data, ctype = download_photo(url)
            pid, _ = client.upload_photo(realty_id, data, ctype)
            photo_ids.append(pid)
            print(f"      фото {i}/{len(photos)} → {pid}")
        except Exception as e:
            print(f"      фото {i}/{len(photos)} ПОМИЛКА: {e}")
        time.sleep(float(cfg["pause_between_photos"]))
    if photo_ids:
        client.save_photos_ordering(realty_id, photo_ids)
    print(f"[3/5] Фото завантажено: {len(photo_ids)} з {len(photos)}")
    save_record(realty_id, {"photo_ids": photo_ids})

    # 4. Характеристики, ціна, опис
    final = dict(geo_payload)
    final.pop("isGeoStep", None)
    final["realty_sale_type"] = 1          # завжди «вторинне житло» (новобудова в CRM і на dom.ria — різні речі)
    try:
        b = client.building_by_location(geo["state_id"], geo["city_id"], geo.get("street_id", 0),
                                        geo.get("building_number_str", ""))
        m = re.search(r'"([0-9a-f]{24})"', json.dumps(b))
        if m:
            final["building_entity_id"] = m.group(1)
    except Exception:
        pass
    final["characteristics_values"] = L["cv"]
    if photo_ids:
        final["mainPhoto"] = photo_ids[0]
    final["description"] = ""
    final["description_uk"] = L["description"]
    client.save_realty(final, step="price")
    print("[4/5] Характеристики, ціну й опис збережено")
    save_record(realty_id, {"status": "draft_complete"})

    # Перевірка від самої dom.ria: чи вистачає обов'язкових характеристик для публікації
    try:
        chk = client.check_main_characteristics(realty_id)
        report["domria_check"] = chk
        print("      Перевірка dom.ria:", json.dumps(chk, ensure_ascii=False)[:300])
    except Exception as e:
        report["domria_check"] = f"помилка: {e}"

    # 5. Техпаспорт і публікація
    if args.techpassport:
        add_techpassport(client, realty_id, args.techpassport, publish=not args.draft,
                         record=load_record(realty_id))
    else:
        print("[5/5] Техпаспорт не вказано — оголошення лишається чернеткою.")
        print(f"      Додати пізніше:  python3 post_domria.py techpassport {realty_id} <файл>")
        try:
            price = client.publication_price(realty_id)
            print("      Ціна платної публікації:", json.dumps(price, ensure_ascii=False)[:200])
        except Exception:
            pass
    print(f"\nГотово. Мої оголошення: https://dom.ria.com/v/uk/mypage")


IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}


def _natural_key(path):
    """«фото 2.jpg» раніше за «фото 10.jpg»."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", os.path.basename(path))]


def prepare_techpassport_files(paths, realty_id):
    """
    Приймає файли і/або папки. PDF ідуть як є. Усі фото (у порядку назв)
    склеюються в ОДИН PDF — по сторінці на фото — і йдуть тим самим шляхом, що й PDF.
    Повертає [(ім'я файлу, байти, content-type)].
    """
    expanded = []
    for p in paths:
        if os.path.isdir(p):
            inner = [os.path.join(p, f) for f in os.listdir(p)
                     if os.path.splitext(f)[1].lower() in IMAGE_EXT | {".pdf"}]
            if not inner:
                raise DomRiaError(f"У папці «{p}» немає фото чи PDF")
            expanded += sorted(inner, key=_natural_key)
        elif os.path.isfile(p):
            expanded.append(p)
        else:
            raise DomRiaError(f"Файл техпаспорта не знайдено: {p}")

    pdfs = [p for p in expanded if p.lower().endswith(".pdf")]
    images = [p for p in expanded if os.path.splitext(p)[1].lower() in IMAGE_EXT]
    other = [p for p in expanded if p not in pdfs and p not in images]
    if other:
        raise DomRiaError(f"Непідтримуваний формат техпаспорта: {', '.join(map(os.path.basename, other))} "
                          f"(можна PDF або фото JPG/PNG)")

    out = []
    for p in pdfs:
        with open(p, "rb") as f:
            out.append((os.path.basename(p), f.read(), "application/pdf"))
    if images:
        out.append((f"techpassport_{realty_id}.pdf", images_to_pdf(images), "application/pdf"))
        print(f"      {len(images)} фото техпаспорта склеєно в один PDF")
    return out


def images_to_pdf(images, max_side=2500):
    from PIL import Image, ImageOps
    if any(p.lower().endswith((".heic", ".heif")) for p in images):
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            raise DomRiaError("Фото у форматі HEIC (iPhone): встановіть  pip install pillow-heif  "
                              "або перешліть фото як JPG")
    pages = []
    for p in images:
        im = Image.open(p)
        im = ImageOps.exif_transpose(im).convert("RGB")      # правильний поворот фото з телефона
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        pages.append(im)
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", save_all=True, append_images=pages[1:], resolution=150)
    return buf.getvalue()


def add_techpassport(client, realty_id, files, publish=True, record=None):
    record = record or load_record(realty_id)
    if record and record.get("geo"):
        geo, parent = record["geo"], record["parent"]
    else:
        # оголошення створене не цим скриптом — беремо дані з dom.ria
        r = client.get_realty(realty_id)
        r = r.get("data", r) if isinstance(r, dict) else {}
        geo = {"state_id": r.get("state_id"), "city_id": r.get("city_id"),
               "street_id": r.get("street_id", 0),
               "building_number_str": r.get("building_number_str", ""),
               "flat_number": r.get("flat_number", "")}
        parent = r.get("realty_type_parent_id")
        if not geo["state_id"] or not parent:
            raise DomRiaError(f"Не вдалося отримати дані оголошення {realty_id}: {str(r)[:300]}")

    file_ids, docs = [], []
    for name, data, ctype in prepare_techpassport_files(files, realty_id):
        fid, stored, _ = client.upload_techpassport_file(realty_id, name, data, ctype)
        file_ids.append(fid)
        docs.append(stored)
        print(f"      техпаспорт {name} → file {fid} ({stored})")

    client.create_techpassport_order(realty_id, parent, geo, file_ids, docs)
    print("[5/5] Техпаспорт відправлено на перевірку dom.ria")
    save_record(realty_id, {"techpassport": {"file_ids": file_ids, "documents": docs},
                            "status": "techpassport_sent"})
    if publish:
        client.publish_free_by_techpassport(realty_id)
        print("      Опубліковано безкоштовно (за техпаспортом)")
        save_record(realty_id, {"status": "published"})
    else:
        print("      --draft: публікацію пропущено")


def cmd_techpassport(args, cfg):
    client = make_client(cfg)
    client.check_auth()
    add_techpassport(client, args.realty_id, args.files, publish=not args.draft)


def main():
    ap = argparse.ArgumentParser(description="Автопостинг CRM → dom.ria")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="перевірити cookie")

    p = sub.add_parser("post", help="створити оголошення з презентації CRM")
    p.add_argument("source", help="посилання на презентацію CRM або збережений .html")
    p.add_argument("--techpassport", nargs="+", metavar="FILE",
                   help="техпаспорт: PDF, кілька фото або папка з фото (фото склеюються в один PDF)")
    p.add_argument("--draft", action="store_true", help="не публікувати після техпаспорта")
    p.add_argument("--dry-run", action="store_true", help="тільки показати, що буде відправлено")
    p.add_argument("--street", help="вулиця вручну, якщо в CRM записана інакше (напр. \"вул. Болсуновська\")")
    p.add_argument("--house", help="номер будинку вручну")
    p.add_argument("--no-ask", action="store_true",
                   help="нічого не питати в консолі (для автоматичного запуску)")

    t = sub.add_parser("techpassport", help="додати техпаспорт до існуючого оголошення")
    t.add_argument("realty_id", type=int)
    t.add_argument("files", nargs="+")
    t.add_argument("--draft", action="store_true", help="не публікувати")

    args = ap.parse_args()
    cfg = load_config()
    try:
        {"check": cmd_check, "post": cmd_post, "techpassport": cmd_techpassport}[args.cmd](args, cfg)
    except AuthError as e:
        sys.exit(f"\n✗ Авторизація: {e}\nОновіть cookies.txt (див. README).")
    except DomRiaError as e:
        sys.exit(f"\n✗ {e}\nДеталі запитів/відповідей — у папці logs/.")


if __name__ == "__main__":
    main()
