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
from rieltor_geo import RieltorGeoClient  # noqa: E402
from rieltor_photos import RateLimitExhausted, upload_photos  # noqa: E402

DATA = os.environ.get("REALTY_DATA_DIR", os.path.join(ROOT, "data"))
COOKIES = os.path.join(DATA, "secrets", "rieltor_cookies.txt")
LOG_DIR = os.path.join(DATA, "rieltor", "runs")
MIN_PHOTOS_REQUIRED = 3


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

    client = RieltorGeoClient(cookie)
    payload = R.build_payload(crm, client, cookie=cookie, upload_photos_flag=False)

    # Продаж будинку без площі ділянки — rieltor.ua не прийме: питаємо
    if payload.get("itemType") == 3 and payload.get("operType") == 1 and not payload.get("groundArea"):
        if a.no_ask:
            bp.result(False, f"rieltor: {crm.get('article') or ''} продаж будинку без площі ділянки в CRM — "
                             f"пропущено (викладіть його окремо, бот спитає площу)")
            return
        while True:
            v = bp.ask_text("Площа ділянки (соток) — у CRM немає, rieltor.ua вимагає для продажу будинку")
            if v is None:
                bp.result(False, "rieltor: без площі ділянки продаж будинку не опублікувати — скасовано")
                return
            num = R.to_number_str(v.replace(",", "."))
            if num:
                payload["groundArea"] = num
                break
            bp.info("Потрібне число, наприклад 6 або 12.5")

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
    import rieltor_refresh_dates as RF
    r = sub.add_parser("refresh")
    r.add_argument("--all", action="store_true")
    r.add_argument("--category", choices=list(RF.CATEGORIES))
    r.add_argument("--oper", choices=list(RF.OPERS))
    sub.add_parser("check")
    a = ap.parse_args()
    try:
        {"post": cmd_post, "refresh": cmd_refresh, "check": cmd_check}[a.cmd](a)
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
