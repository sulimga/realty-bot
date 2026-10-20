"""
OLX — точка входу для диспетчера (протокол: common/board_protocol.py).

    python run.py post --url <посилання CRM> [--dry-run]
    python run.py check

Поки вміє лише «Продаж будинків» (категорія 1602).
Оголошення створюється одразу (як після кнопки «Опублікувати» у формі) і чекає на
оплату/активацію в кабінеті OLX — у категорії нерухомості розміщення платне.
--dry-run: усе зібрати й показати, нічого не відправляти на OLX (фото теж не вантажаться).

Дані: $REALTY_DATA_DIR/secrets/olx_cookies.txt, $REALTY_DATA_DIR/olx/{config.json,logs,runs}
"""

import argparse
import io
import json
import os
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

import mapping as M  # noqa: E402
from olx_client import NET_ERRORS, AuthError, OlxClient, OlxError  # noqa: E402

DATA = os.environ.get("REALTY_DATA_DIR", os.path.join(ROOT, "data"))
BOARD_DIR = os.path.join(DATA, "olx")
COOKIES = os.path.join(DATA, "secrets", "olx_cookies.txt")
AUTH = os.path.join(DATA, "secrets", "olx_auth.txt")
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
                  log_dir=os.path.join(BOARD_DIR, "logs"), auth_path=AUTH)
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
def build_params(estate, cfg, notes):
    p = estate.get("parameters") or {}
    qf = estate.get("quick_facts") or {}
    out = {"from_developer": False, "commission": False, "is_exchange": False,
           "cooperate": False, "eoselia": False}

    price, cur = M.parse_price(estate.get("price"))
    if not price:
        price = ask_number("Ціна")
        if not price:
            raise OlxError("без ціни OLX не прийме оголошення")
    if not cur:
        cur = ask_option(f"OLX: валюта ціни {price}?", [("USD", "$"), ("UAH", "грн"), ("EUR", "€")], "USD")
    out["price"] = {"currency": cur, "price": price, "arranged": 1 if cfg.get("price_negotiable") else 0}

    def required(code, label, raw, unit_note=""):
        n = M.num(raw)
        if n is None:
            v = ask_number(label, unit_note)
            if not v:
                raise OlxError(f"обов'язкове поле «{label}» не заповнене")
            out[code] = v
            return
        v = M.to_int_str(n)
        if float(v) != n:
            notes.append(f"{label}: {raw} → {v} (OLX приймає лише цілі)")
        out[code] = v

    floors_qf = qf.get("Поверх") if "/" not in str(qf.get("Поверх", "")) else None
    required("total_floors", "Поверховість", M.get(p, "Поверховість", "Этажность") or floors_qf)
    area_qf = str(qf.get("Площа") or "").split("/")[0] or None
    required("total_area", "Загальна площа", M.get(p, "Площа загальна,м²", "Площадь общая,м²") or area_qf, ", м²")
    required("number_of_rooms", "Кількість кімнат", M.get(p, "Кіл. кімнат", "Кол. комнат") or qf.get("Кіл. кімнат"))
    required("land_area", "Площа ділянки", M.get(p, "Площа ділянки,соток", "Площадь участка,соток") or qf.get("Ділянка"),
             ", соток")
    out["property_type_houses"] = M.house_kind(estate)

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
    return out


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
        if not M.is_house_sale(estate):
            raise OlxError(f"поки вмію лише продаж будинків, а тут «{estate.get('deal_title')}»")

        notes = []
        params = build_params(estate, cfg, notes)
        geo_client = client or OlxClient(None, cfg["device_id"])   # dry-run: гео-запити публічні
        print("[2/5] Населений пункт…")
        loc, place, city = resolve_location(geo_client, estate, cfg)
        print(f"      {place}")

        cands = M.title_candidates(estate, params, city)
        kind, title = bp.ask_choice(f"OLX: заголовок ({M.TITLE_MIN}–{M.TITLE_MAX} символів). Оберіть або напишіть свій",
                                    [(t, t) for t in cands], allow_text=True)
        title = (title or cands[0]).strip()
        while not (M.TITLE_MIN <= len(title) <= M.TITLE_MAX):
            t = bp.ask_text(f"OLX: заголовок має бути {M.TITLE_MIN}–{M.TITLE_MAX} символів, зараз {len(title)}. Напишіть інший")
            if not t:
                raise OlxError("заголовок не підходить за довжиною")
            title = t.strip()

        desc = M.description(estate)
        if len(desc) < M.DESC_MIN:
            t = bp.ask_text(f"OLX: опис закороткий ({len(desc)} символів, треба від {M.DESC_MIN}). Напишіть опис")
            if not t or len(t.strip()) < M.DESC_MIN:
                raise OlxError("опис закороткий")
            desc = t.strip() + ("\n\n" + M.spaced_article(estate.get("article")) if estate.get("article") else "")

        cont = contacts(client, cfg) if client else {k: cfg.get(k) or "?" for k in ("person", "phone", "email")}

        urls = estate.get("photos") or []
        limit = M.MAX_PHOTOS[M.CATEGORY_HOUSE_SALE]
        if len(urls) > limit:
            notes.append(f"фото: у CRM {len(urls)}, OLX приймає максимум {limit} — беру перші {limit}")
            urls = urls[:limit]

        payload = {"brand": "olxua", "lang": "uk", "category_id": M.CATEGORY_HOUSE_SALE,
                   **{k: loc[k] for k in ("city_id", "district_id") if k in loc},
                   "description": desc, "email": cont["email"], "parameters": params,
                   "person": cont["person"], "phone": cont["phone"], "images": [], "title": title,
                   "private_business": cfg.get("private_business", "business"),
                   "components_data": {"reposting": {"action": "ad_posted", "data": "{\"reposting\":false}"}},
                   **{k: loc[k] for k in ("latitude", "longitude", "radius", "zoom")}}

        summary = [f"Заголовок: {title}", f"Ціна: {params['price']['price']} {params['price']['currency']}",
                   f"Площа {params['total_area']} м², ділянка {params['land_area']} сот., "
                   f"кімнат {params['number_of_rooms']}, поверхів {params['total_floors']}",
                   f"Тип: {dict(M.HOUSE_KIND_LABELS)[params['property_type_houses']]}, "
                   f"стіни: {M.WALLS_LABELS[params['house_type']]}"
                   + (f", ремонт: {M.REPAIR_LABELS[params['repair']]}" if params.get("repair") else ""),
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


def cmd_check(a):
    cfg = load_cfg()
    client = make_client(cfg, check_token=False)
    d = client.describe()
    names = d["cookie_names"]
    keyish = [n for n in names if any(x in n.lower() for x in ("token", "refresh", "auth", "session", "sid"))]
    print(f"Cookie: {len(names)} шт." + (f" (з них про вхід: {', '.join(keyish)})" if keyish else ""))
    if d["token"]:
        t = d["token"]
        print(f"Токен: {t.get('token_use') or '?'}, видавець {t.get('iss') or '?'}, "
              f"дійсний ще {fmt_left(d['expires_in'])}")
    else:
        print("Токена (Authorization: Bearer …) не знайшов")
    print("Зв'язок із OLX: " + "; ".join(client.probe()))
    print(f"Спосіб з'єднання: {client.engine}")
    client.ensure_token()
    me = client.me()
    who = me.get("email") or me.get("name") or me.get("id") or "?"
    bp.result(True, f"OLX: вхід працює ({who}). Токен дійсний ще: {fmt_left(client.token_expires_in())}")


def finish_error(e):
    if isinstance(e, AuthError):
        bp.result(False, f"OLX: немає входу — {e}.\nОновіть: кнопка «🍪 Cookies» → OLX, надішліть рядок "
                         f"Authorization (Bearer …) з будь-якого запиту до www.olx.ua/api/…")
    elif isinstance(e, NET_ERRORS):
        bp.result(False, f"OLX не відповідає ({type(e).__name__}: {str(e)[:200]}). Спробуйте ще раз за кілька хвилин.")
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
    a = ap.parse_args()
    try:
        {"post": cmd_post, "check": cmd_check}[a.cmd](a)
    except BaseException as e:  # noqa: BLE001
        finish_error(e)
    finally:
        bp.flush_info()


if __name__ == "__main__":
    main()
