"""
rieltor.ua — точка входу для диспетчера (протокол: common/board_protocol.py).

    python run.py post --url <посилання CRM> [--no-ask]
    python run.py refresh          # спитати: усі чи категорія (у боті — кнопками)
    python run.py refresh --all    # усі, від найстаріших, до добового ліміту
    python run.py refresh --category houses --oper sale   # одна категорія
    python run.py check

Логіка — у crm_to_rieltor_payload.py / rieltor_geo.py / rieltor_photos.py (ті самі файли,
що працювали з комп'ютера). Cookie — у $REALTY_DATA_DIR/secrets/rieltor_cookies.txt.
"""

import argparse
import json
import os
import re
import sys
import traceback

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [HERE, os.path.join(ROOT, "crm"), os.path.join(ROOT, "common")]

import board_protocol as bp  # noqa: E402

bp.install_print_hook()

import crm_to_rieltor_payload as R  # noqa: E402
from extract_estate import fetch_estate_data  # noqa: E402
import extract_estate_admin as ADM  # noqa: E402
from rieltor_geo import RieltorGeoClient  # noqa: E402
from rieltor_photos import RateLimitExhausted, upload_photos  # noqa: E402

DATA = os.environ.get("REALTY_DATA_DIR", os.path.join(ROOT, "data"))
COOKIES = os.path.join(DATA, "secrets", "rieltor_cookies.txt")
LOG_DIR = os.path.join(DATA, "rieltor", "runs")
MIN_PHOTOS_REQUIRED = 3

# cookie самої CRM-адмінки (інший, ніж rieltor_cookies.txt) — лише для звірки категорії/типу
ADM.COOKIE_FILE = os.path.join(DATA, "secrets", "crm_admin_cookie.txt")


def check_land_category(crm):
    """Best-effort звірка через авторизовану сторінку CRM: якщо Категорія об'єкта —
    «Земельні ділянки», примусово land (itemType=5), навіть якщо заголовок хтось зіпсував
    вручну. Якщо cookie CRM-адмінки нема або запит не вдався — просто повертає None,
    постинг іде далі за звичайним визначенням типу з заголовка (нічого не ламає)."""
    estate_id = crm.get("estate_id")
    if not estate_id or not os.path.exists(ADM.COOKIE_FILE):
        return None
    try:
        admin = ADM.fetch_estate_admin_data(estate_id)
        cat = (admin.get("category") or "").strip()
        bp.info(f"CRM-адмінка: категорія «{cat}», тип «{admin.get('type')}»")
        return 5 if cat == "Земельні ділянки" else None
    except Exception as e:  # noqa: BLE001 — best-effort, не блокуємо постинг
        bp.info(f"Не вдалось звірити категорію в CRM-адмінці ({type(e).__name__}: {e}) — "
                f"покладаюсь на заголовок CRM")
        return None


class CookieMissing(Exception):
    pass


def load_cookie():
    if not os.path.exists(COOKIES):
        raise CookieMissing("немає cookie. Додайте їх: кнопка «🍪 Cookies» → rieltor")
    raw = open(COOKIES, encoding="utf-8").read().strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if not raw:
        raise CookieMissing("файл cookie порожній. Додайте їх: кнопка «🍪 Cookies» → rieltor")
    return raw


def save_log(name, obj):
    os.makedirs(LOG_DIR, exist_ok=True)
    import time
    path = os.path.join(LOG_DIR, f"{time.strftime('%Y%m%d_%H%M%S')}_{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


def resolve_missing(payload, client, crm, no_ask):
    """Будь-яка категорія: те, що не вдалось визначити з CRM/тексту/rieltor.ua, питаємо в користувача.
    У пакетному режимі (no_ask) — пропускаємо об'єкт з поясненням. True — можна публікувати."""
    missing = payload.get("_missing") or []
    if not missing:
        return True
    art = crm.get("article") or ""
    noun = {1: "квартиру", 2: "кімнату", 3: "будинок", 4: "комерційну", 5: "ділянку"}.get(
        payload.get("itemType"), "оголошення")
    if no_ask:
        bp.result(False, f"rieltor: {art} не вдалось визначити: "
                         f"{', '.join(m['label'] for m in missing)} — пропущено "
                         f"(викладіть окремо, бот спитає)")
        return False
    for m in missing:
        key, label, kind = m["key"], m["label"], m["kind"]
        if kind == "choice":
            k, v = bp.ask_choice(f"{label} — у CRM не вказано. Оберіть:", [(val, name) for val, name in m["options"]])
            if k != "opt":
                bp.result(False, f"rieltor: без поля «{label}» не опублікувати — скасовано")
                return False
            payload[key] = v
        elif kind == "areas":
            bp.info(label)
            while True:
                vals = []
                for name in ("Загальна площа, м²", "Житлова площа, м²", "Площа кухні, м²"):
                    v = bp.ask_text(f"{name} (число)")
                    num = R.to_number_str((v or "").replace(",", "."))
                    if not (v and num and re.fullmatch(r"\d+(\.\d+)?", num)):
                        bp.result(False, "rieltor: без правильних площ не опублікувати — скасовано")
                        return False
                    vals.append(float(num))
                if vals[1] + vals[2] <= vals[0]:
                    break
                bp.info("Загальна площа має бути не меншою за суму житлової і кухні — введіть ще раз")
            payload["commonArea"], payload["usefulArea"], payload["kitchenArea"] = (R._fmt_area(x) for x in vals)
        elif kind == "district":
            opts = [(d["districtId"], d["name"]) for d in m.get("options") or []]
            if not opts and payload.get("cityId"):
                opts = [(int(d["districtId"]), d["name"]) for d in client.search_districts(payload["cityId"])]
            k, v = bp.ask_choice("Не вдалось визначити район на rieltor.ua. Оберіть:", opts)
            if k != "opt":
                bp.result(False, "rieltor: без району не опублікувати — скасовано")
                return False
            payload["districtId"] = v
        else:
            while True:
                v = bp.ask_text(f"{label} — у CRM і в описі не знайшов. Вкажіть число")
                if v is None:
                    bp.result(False, f"rieltor: без поля «{label}» не опублікувати — скасовано")
                    return False
                num = R.to_number_str(v.replace(",", "."))
                if num and re.fullmatch(r"\d+(\.\d+)?", num):
                    break
                bp.info("Потрібне число, наприклад 3 або 54.5")
            payload[key] = num if kind == "area" else num.split(".")[0]
            if key == "commonArea":      # площі житлова/кухня — за правилом 40%/20% (будинок 70%/15%)
                c = float(num)
                u, k = (0.70, 0.15) if payload.get("itemType") == 3 else (0.40, 0.20)
                if payload.get("itemType") in (1, 3):
                    payload.setdefault("usefulArea", R._fmt_area(c * u))
                    payload.setdefault("kitchenArea", R._fmt_area(c * k))
            if key == "roomCnt":
                payload[key] = str(min(int(num.split(".")[0]), 6))
    if payload.get("itemType") == 4:     # комерція: поверх/поверховість дублюються в detailParams
        dp = payload.setdefault("detailParams", {})
        if payload.get("floor"):
            dp["floor"] = [int(payload["floor"])]
            payload.pop("floor", None)
        if payload.get("floorCnt"):
            payload["floorCnt"] = int(payload["floorCnt"])
            dp["floorCnt"] = payload["floorCnt"]
    return True


resolve_missing_flat = resolve_missing


def cmd_post(a):
    cookie = load_cookie()
    crm = fetch_estate_data(a.url)
    art = (crm.get("article") or "").lstrip("#")
    _res = bp.result
    bp.result = lambda ok, text, **kw: _res(ok, text, article=crm.get("article"), **kw)   # артикул — у звіт пакета

    photos = crm.get("photos", [])
    if not photos:
        bp.result(False, "rieltor: у CRM немає жодного фото — rieltor.ua вимагає мінімум 3")
        return
    if len(photos) < MIN_PHOTOS_REQUIRED:
        missing = MIN_PHOTOS_REQUIRED - len(photos)
        bp.info(f"У CRM {len(photos)} фото (потрібно {MIN_PHOTOS_REQUIRED}) — дублюю останнє {missing} раз(и)")
        crm["photos"] = photos + [photos[-1]] * missing

    item_type_override = check_land_category(crm)

    client = RieltorGeoClient(cookie)
    address_override = {k: v for k, v in (("district_id", a.district_id), ("street", a.street),
                                          ("house", a.house)) if v} or None
    payload = R.build_payload(crm, client, cookie=cookie, upload_photos_flag=False,
                              item_type_override=item_type_override, address_override=address_override)

    # Продаж ділянки без кадастрового номера в описі CRM — не питаємо, одразу пропускаємо
    # (при масовому завантаженні землі нема часу перевіряти кожен об'єкт вручну)
    if payload.get("itemType") == 5 and payload.get("operType") == 1 and not payload.get("cadastralNumber"):
        bp.result(False, f"rieltor: {crm.get('article') or ''} продаж ділянки без кадастрового номера "
                         f"в описі CRM — пропущено")
        return

    # Що не вдалось заповнити (будь-яка категорія) — питаємо; у пакеті — пропускаємо з поясненням
    if not resolve_missing(payload, client, crm, a.no_ask):
        return

    photo_field = R.PHOTO_FIELD_BY_ITEM_TYPE.get(payload.get("itemType"), "rentaImages1")
    bp.info(f"Завантажую {len(crm['photos'])} фото на rieltor.ua…")
    payload[photo_field] = upload_photos(crm["photos"], cookie)
    if not payload[photo_field]:
        bp.result(False, "rieltor: жодне фото не завантажилось — публікацію скасовано")
        return

    bp.info("Публікую на rieltor.ua…")
    try:
        resp = R.submit_offer(payload, cookie)
    except requests.exceptions.Timeout:
        save_log(f"{art}_timeout", {"payload": payload})
        bp.result(False, "rieltor: сервер не відповів вчасно. Зазвичай оголошення все одно створюється — "
                         "перевірте кабінет rieltor.ua, перш ніж пробувати ще раз (щоб не було дубля)")
        return

    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:3000]
    save_log(art or "post", {"status": resp.status_code, "response": body,
                             "payload": {k: v for k, v in payload.items() if not k.startswith("_")}})

    api_ok = not isinstance(body, dict) or str(body.get("status", "OK")).upper() == "OK"
    if resp.status_code == 200 and api_ok:
        offer_id = None
        if isinstance(body, dict):
            d = body.get("data") if isinstance(body.get("data"), dict) else body
            offer_id = d.get("id") or d.get("offerId")
        bp.result(True, "rieltor: опубліковано" + (f" (№ {offer_id})" if offer_id else "") +
                  ". Перевірте в кабінеті rieltor.ua → Оголошення", id=offer_id)
    elif resp.status_code in (401, 403):
        bp.result(False, "rieltor: cookie застаріли. Оновіть: кнопка «🍪 Cookies» → rieltor", stop_batch=True)
    else:
        bp.result(False, f"rieltor: сервер повернув помилку {resp.status_code}: {str(body)[:500]}")


def cmd_districts(a):
    """Для кнопки «Змінити адресу» в боті: райони міста цього об'єкта (ім'я міста — з адреси CRM)."""
    cookie = load_cookie()
    crm = fetch_estate_data(a.url)
    city_q = R.parse_address(crm.get("address", ""))["city"]
    client = RieltorGeoClient(cookie)
    cities = client.search_cities(city_q) if city_q else []
    if not cities:
        bp.result(False, f"rieltor: не знайшов місто «{city_q}» на rieltor.ua")
        return
    city = cities[0]
    districts = client.search_districts(int(city["cityId"])) if city.get("hasDistricts") else []
    bp.result(True, "ok", city=city_q, districts=[[int(d["districtId"]), d["name"]] for d in districts])


def cmd_refresh(a):
    import rieltor_refresh_dates as RF
    cookie = load_cookie()
    category, oper = a.category, a.oper
    if not a.all and not category:
        kind, v = bp.ask_choice("Що оновити на rieltor?", [("all", "Усі оголошення (від найстарішого)"),
                                                          ("cat", "Окрему категорію")])
        if kind != "opt":
            bp.result(False, "rieltor: оновлення дат скасовано")
            return
        if v == "cat":
            kind, category = bp.ask_choice("Категорія", [(k, n) for k, (n, _) in RF.CATEGORIES.items()])
            if kind != "opt":
                bp.result(False, "rieltor: оновлення дат скасовано")
                return
    if category and not oper:
        kind, oper = bp.ask_choice("Продаж чи оренда?", [(k, n) for k, (n, _) in RF.OPERS.items()])
        if kind != "opt":
            bp.result(False, "rieltor: оновлення дат скасовано")
            return
    bp.info("rieltor: оновлюю дати від найстаріших, доки сайт не скаже «ліміт»…")
    res = RF.refresh_until_limit(cookie, log=bp.info, category=category, oper=oper)
    save_log("refresh", res)
    ok = res["stopped"] == "" and (res["failed"] == 0 or res["ok"] > 0)
    bp.result(ok, RF.summary_text(res), stop_batch=res["stopped"] == "cookie")


def cmd_delete(a):
    import rieltor_refresh_dates as RF
    cookie = load_cookie()
    category, oper = a.category, a.oper
    if not category:
        kind, category = bp.ask_choice("Видалити оголошення якої категорії?",
                                       [(k, n) for k, (n, _) in RF.CATEGORIES.items() if k != "parking"])
        if kind != "opt":
            bp.result(False, "rieltor: видалення скасовано")
            return
    if not oper:
        kind, oper = bp.ask_choice("Продаж чи оренда?", [(k, n) for k, (n, _) in RF.OPERS.items()])
        if kind != "opt":
            bp.result(False, "rieltor: видалення скасовано")
            return
    scope = f"{RF.CATEGORIES[category][0]} / {RF.OPERS[oper][0]}"
    bp.info(f"rieltor: шукаю активні оголошення «{scope}»…")
    offers, err = RF.select_offers(cookie, category, oper)
    if err:
        bp.result(False, f"rieltor: категорію не вдалося виділити — {err}. Нічого не видалено")
        return
    if not offers:
        bp.result(True, f"rieltor: активних оголошень «{scope}» немає")
        return
    if not RF.DELETE_REQUEST:
        bp.result(False, f"rieltor: знайдено {len(offers)} оголошень «{scope}», але запит видалення ще не "
                         f"налаштований — потрібен HAR видалення одного оголошення. Нічого не видалено")
        return
    kind, v = bp.ask_choice(f"⚠️ Видалити НАЗАВЖДИ {len(offers)} оголошень «{scope}» на rieltor.ua?",
                            [("yes", f"Так, видалити {len(offers)}"), ("no", "Ні, скасувати")])
    if kind != "opt" or v != "yes":
        bp.result(False, "rieltor: видалення скасовано, нічого не видалено")
        return
    res = RF.delete_all(offers, cookie, log=bp.info)
    save_log("delete", res)
    t = f"rieltor: видалено {res['ok']} з {res['total']} оголошень «{scope}»"
    if res["stopped"] == "cookie":
        t += ". Зупинено: cookie застаріли (кнопка «🍪 Cookies» → rieltor)"
    elif res["stopped"] == "fails":
        t += f". Зупинено після {RF.MAX_CONSECUTIVE_FAILS} помилок поспіль"
    if res["failed"]:
        t += f". Помилок: {res['failed']}"
    bp.result(res["stopped"] == "" and res["failed"] == 0, t)


def cmd_check(a):
    import rieltor_refresh_dates as RF
    offers = RF.fetch_offers(load_cookie())
    bp.result(True, f"rieltor: cookie робочі, активних оголошень: {len(offers)}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("post")
    p.add_argument("--url", required=True)
    p.add_argument("--techpassport", nargs="*", default=[])   # rieltor не потребує — ігноруємо
    p.add_argument("--no-ask", action="store_true")            # пакетний режим: нічого не питати
    p.add_argument("--district-id", type=int)                  # адреса, виправлена вручну в боті
    p.add_argument("--street")
    p.add_argument("--house")
    d = sub.add_parser("districts")
    d.add_argument("--url", required=True)
    import rieltor_refresh_dates as RF
    dl = sub.add_parser("delete")
    dl.add_argument("--category", choices=list(RF.CATEGORIES))
    dl.add_argument("--oper", choices=list(RF.OPERS))
    r = sub.add_parser("refresh")
    r.add_argument("--all", action="store_true")
    r.add_argument("--category", choices=list(RF.CATEGORIES))
    r.add_argument("--oper", choices=list(RF.OPERS))
    sub.add_parser("check")
    a = ap.parse_args()
    try:
        {"post": cmd_post, "refresh": cmd_refresh, "delete": cmd_delete, "check": cmd_check, "districts": cmd_districts}[a.cmd](a)
    except CookieMissing as e:
        bp.result(False, f"rieltor: {e}", stop_batch=True)
    except RateLimitExhausted as e:
        bp.result(False, f"rieltor: {e}", stop_batch=True)
    except requests.HTTPError as e:
        code = getattr(e.response, "status_code", "")
        hint = " — схоже, cookie застаріли (кнопка «🍪 Cookies» → rieltor)" if code in (401, 403) else ""
        bp.result(False, f"rieltor: помилка HTTP {code}{hint}", stop_batch=code in (401, 403))
    except BaseException as e:  # noqa: BLE001
        bp.info(traceback.format_exc()[-1500:])
        bp.result(False, f"rieltor: непередбачена помилка {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
