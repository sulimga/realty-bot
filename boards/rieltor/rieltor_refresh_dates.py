"""
Оновлення дати актуальності оголошень на rieltor.ua з урахуванням добового ліміту.

rieltor.ua дає оновити обмежену кількість оголошень на добу (≈49). Тому скрипт:
  1) завантажує всі активні оголошення (status=10);
  2) сортує їх від найстарішої дати оновлення до найновішої;
  3) оновлює по черзі, доки сайт не відповість помилкою «перевищено ліміт оновлень»
     (тоді зупиняється — це нормальне завершення, а не збій).

Запит оновлення (підтверджено HAR):
    GET https://rieltor.ua/api/offers/item-action/?id={ID}&action=refresh

Поле з датою оновлення в списку оголошень шукається автоматично (див. DATE_FIELD_PRIORITY);
якщо жодного не знайдено — порядок «за зростанням ID» (старіші оголошення першими).

Використання з консолі:
    python3 rieltor_refresh_dates.py
"""

import re
import sys
import time
from datetime import datetime, timezone

import requests

SITE_BASE_URL = "https://rieltor.ua"
HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "accept": "application/json, text/plain, */*",
}

DELAY_BETWEEN_SECONDS = 2      # пауза між оголошеннями
MAX_CONSECUTIVE_FAILS = 3      # стільки «незрозумілих» помилок поспіль — зупиняємось

# Назви полів, у яких може лежати дата останнього оновлення (у порядку пріоритету).
DATE_FIELD_PRIORITY = [
    "refreshedAt", "refreshed_at", "refreshDate", "refresh_date", "dateRefresh",
    "actualAt", "actualDate", "actual_date", "dateActual",
    "upAt", "upDate", "dateUp",
    "updatedAt", "updated_at", "updateDate", "update_date", "dateUpdate", "dateUpdated", "updated",
    "publishedAt", "published_at", "publishDate", "datePublish",
    "createdAt", "created_at", "createDate", "dateCreate", "created",
]
DATE_KEY_HINT = re.compile(r"refresh|actual|update|upd|date|_at$|At$|time", re.I)

# Категорії rieltor (itemType / operType — як у crm_to_rieltor_payload.py)
CATEGORIES = {               # ключ: (назва, набір itemType)
    "houses": ("Будинки", {3}),
    "flats": ("Квартири (і кімнати)", {1, 2}),
    "commercial": ("Комерційна", {4}),
    "land": ("Земля", {5}),
    "parking": ("Паркомісця", {6}),
}
OPERS = {"sale": ("Продаж", 1), "rent": ("Оренда", 2)}
ITEM_TYPE_KEYS = ("itemType", "item_type", "itemTypeId")
OPER_TYPE_KEYS = ("operType", "oper_type", "operTypeId")


class FilterUnavailable(Exception):
    pass


def _get_int(o: dict, keys):
    for k in keys:
        v = o.get(k)
        if isinstance(v, dict):
            v = v.get("id", v.get("value"))
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def filter_offers(offers: list, category: str, oper: str) -> list:
    """Лишає оголошення потрібної категорії й типу угоди. Якщо в списку немає полів типу — FilterUnavailable."""
    items = CATEGORIES[category][1]
    op = OPERS[oper][1]
    if offers and not any(_get_int(o, ITEM_TYPE_KEYS) is not None and _get_int(o, OPER_TYPE_KEYS) is not None
                          for o in offers):
        raise FilterUnavailable("у списку оголошень rieltor не знайдено полів типу нерухомості/угоди")
    return [o for o in offers if _get_int(o, ITEM_TYPE_KEYS) in items and _get_int(o, OPER_TYPE_KEYS) == op]


LIMIT_MARKERS = ("ліміт", "лимит", "limit", "перевищ", "превыш", "exceed", "too many")


class LimitReached(Exception):
    pass


def _headers(cookie: str) -> dict:
    h = dict(HEADERS_BASE)
    h["cookie"] = cookie
    return h


def fetch_offers(cookie: str, log=print) -> list:
    """Повертає повний список активних оголошень (усі сторінки)."""
    all_items = []
    page = 1
    while True:
        resp = requests.get(
            f"{SITE_BASE_URL}/api/offers/list/",
            params={"page": page, "limit": 100, "status": 10, "mode": 10},
            headers=_headers(cookie),
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "OK":
            raise RuntimeError(f"Неочікувана відповідь API: {payload}")

        data = payload["data"]
        all_items.extend(data["items"])
        page_count = data.get("pagination", {}).get("pageCount", 1)
        if log:
            log(f"  Завантажено сторінку {page} з {page_count} ({len(data['items'])} оголошень).")
        if page >= page_count:
            break
        page += 1
    return all_items


# ---------- дати ----------

def parse_date(v):
    """Рядок/число → datetime (UTC) або None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if v > 1e12:          # мілісекунди
            v = v / 1000
        if 1e9 < v < 4e9:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        return None
    if isinstance(v, dict):   # {"date": "...", ...}
        for k in ("date", "value", "timestamp"):
            if k in v:
                return parse_date(v[k])
        return None
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit():
        return parse_date(int(s))
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def pick_date_field(offers: list):
    """Знаходить поле з датою оновлення, яке є (і розбирається) у більшості оголошень."""
    if not offers:
        return None

    def coverage(key):
        return sum(1 for o in offers if parse_date(o.get(key)) is not None)

    need = max(1, len(offers) // 2)
    for key in DATE_FIELD_PRIORITY:
        if coverage(key) >= need:
            return key
    # невідома назва — шукаємо будь-яке «датоподібне» поле
    keys = {k for o in offers for k in o.keys() if DATE_KEY_HINT.search(k)}
    best = sorted(keys, key=lambda k: (-coverage(k), k))
    return best[0] if best and coverage(best[0]) >= need else None


def sort_oldest_first(offers: list):
    """→ (відсортований список, назва поля дати або None)."""
    field = pick_date_field(offers)
    if field:
        far_past = datetime(1970, 1, 1, tzinfo=timezone.utc)
        key = lambda o: (parse_date(o.get(field)) or far_past, _int(o.get("id")))
    else:
        key = lambda o: _int(o.get("id"))
    return sorted(offers, key=key), field


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ---------- оновлення ----------

def refresh_offer(offer_id: int, cookie: str):
    """Оновлює дату одного оголошення. → (HTTP-код, тіло відповіді)."""
    resp = requests.get(
        f"{SITE_BASE_URL}/api/offers/item-action/",
        params={"id": offer_id, "action": "refresh"},
        headers=_headers(cookie),
        timeout=20,
    )
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:1000]
    return resp.status_code, body


# ---------- видалення ----------
# Запит видалення (підтверджено HAR 30.10.2026; «в архів» — така сама дія з action=draft):
#   GET /api/offers/item-action/?id={id}&action=delete&reason=24&userPrice=0&verification=false
# reason=24 — значення, яке сайт сам підставив у формі видалення.
DELETE_REQUEST = {"method": "GET", "path": "/api/offers/item-action/",
                  "params": {"id": "{id}", "action": "delete", "reason": "24",
                             "userPrice": "0", "verification": "false"}}


class DeleteNotConfigured(Exception):
    pass


def delete_offer(offer_id: int, cookie: str):
    """Видаляє одне оголошення. → (HTTP-код, тіло відповіді)."""
    if not DELETE_REQUEST:
        raise DeleteNotConfigured("запит видалення ще не налаштований (потрібен HAR)")
    fill = lambda v: v.replace("{id}", str(offer_id)) if isinstance(v, str) else v
    params = {k: fill(v) for k, v in (DELETE_REQUEST.get("params") or {}).items()}
    data = DELETE_REQUEST.get("json")
    resp = requests.request(DELETE_REQUEST.get("method", "GET"), SITE_BASE_URL + DELETE_REQUEST["path"],
                            params=params, json=data, headers=_headers(cookie), timeout=20)
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:1000]
    return resp.status_code, body


def select_offers(cookie: str, category: str, oper: str):
    """Усі активні оголошення категорії+угоди. → (список, помилка-фільтра або None)."""
    offers = fetch_offers(cookie, log=None)
    try:
        return filter_offers(offers, category, oper), None
    except FilterUnavailable as e:
        return [], str(e)


def delete_all(offers: list, cookie: str, log=print, delay=DELAY_BETWEEN_SECONDS, should_stop=None) -> dict:
    res = {"total": len(offers), "ok": 0, "failed": 0, "stopped": "", "errors": [], "deleted": []}
    fails_in_row = 0
    for i, o in enumerate(offers, start=1):
        oid = o.get("id")
        try:
            code, body = delete_offer(oid, cookie)
        except requests.RequestException as e:
            code, body = None, f"{type(e).__name__}: {e}"
        if is_ok(code, body):
            res["ok"] += 1
            res["deleted"].append(oid)
            fails_in_row = 0
        elif code in (401, 403):
            res["stopped"] = "cookie"
            break
        else:
            res["failed"] += 1
            fails_in_row += 1
            res["errors"].append({"id": oid, "http": code, "body": error_text(body)})
            if log:
                log(f"№ {oid}: помилка {code}: {error_text(body)}")
            if fails_in_row >= MAX_CONSECUTIVE_FAILS:
                res["stopped"] = "fails"
                break
        if log and i % 10 == 0:
            log(f"  оброблено {i} з {len(offers)}, видалено {res['ok']}")
        if i < len(offers):
            time.sleep(delay)
    return res


def is_ok(code, body) -> bool:
    return code == 200 and isinstance(body, dict) and str(body.get("status", "")).upper() == "OK"


def is_limit_error(code, body) -> bool:
    if code == 429:
        return True
    text = str(body).lower()
    return any(m in text for m in LIMIT_MARKERS)


def error_text(body) -> str:
    if isinstance(body, dict):
        for k in ("message", "error", "errors", "detail", "data"):
            if body.get(k):
                return str(body[k])[:300]
    return str(body)[:300]


def refresh_until_limit(cookie: str, log=print, delay=DELAY_BETWEEN_SECONDS,
                        category: str = None, oper: str = None) -> dict:
    """
    Оновлює найстаріші оголошення (усі або лише категорію+угоду), доки rieltor не скаже «ліміт».
    → {"total", "ok", "failed", "limit", "limit_text", "stopped", "date_field", "refreshed": [id…], "errors": […]}
    """
    all_offers = fetch_offers(cookie, log=None)
    sample = all_offers[0] if all_offers else {}
    scope = ""
    if category:
        scope = f"{CATEGORIES[category][0]} / {OPERS[oper][0]}"
        try:
            offers = filter_offers(all_offers, category, oper)
        except FilterUnavailable as e:
            return {"total": 0, "ok": 0, "failed": 0, "limit": False, "limit_text": "", "stopped": "filter",
                    "filter_error": str(e), "scope": scope, "date_field": None, "refreshed": [], "errors": [],
                    "sample_keys": sorted(sample.keys()),
                    "sample_types": {k: sample.get(k) for k in ITEM_TYPE_KEYS + OPER_TYPE_KEYS if k in sample}}
    else:
        offers = all_offers
    offers, field = sort_oldest_first(offers)
    res = {"scope": scope, "total": len(offers), "ok": 0, "failed": 0, "limit": False, "limit_text": "",
           "stopped": "", "date_field": field, "refreshed": [], "errors": [],
           # для діагностики: які поля є в оголошенні і які з них схожі на дати
           "sample_keys": sorted(sample.keys()),
           "sample_dates": {k: v for k, v in sample.items() if DATE_KEY_HINT.search(k)},
           "sample_types": {k: sample.get(k) for k in ITEM_TYPE_KEYS + OPER_TYPE_KEYS if k in sample}}
    if log:
        log((f"{scope}: " if scope else "") + f"оголошень до оновлення: {len(offers)}. Порядок: " +
            (f"від найстарішого за полем «{field}»" if field else "за зростанням ID (поле дати не знайдено)"))

    fails_in_row = 0
    for i, o in enumerate(offers, start=1):
        oid = o.get("id")
        try:
            code, body = refresh_offer(oid, cookie)
        except requests.RequestException as e:
            code, body = None, f"{type(e).__name__}: {e}"

        if is_ok(code, body):
            res["ok"] += 1
            res["refreshed"].append(oid)
            fails_in_row = 0
        elif code in (401, 403):
            res["stopped"] = "cookie"
            break
        elif code is not None and is_limit_error(code, body):
            res["limit"] = True
            res["limit_text"] = error_text(body)
            break
        else:
            res["failed"] += 1
            fails_in_row += 1
            res["errors"].append({"id": oid, "http": code, "body": error_text(body)})
            if log:
                log(f"№ {oid}: помилка {code}: {error_text(body)}")
            if fails_in_row >= MAX_CONSECUTIVE_FAILS:
                res["stopped"] = "fails"
                break

        if log and i % 10 == 0:
            log(f"  оброблено {i}, оновлено {res['ok']}")
        if i < len(offers):
            time.sleep(delay)
    return res


def summary_text(res: dict) -> str:
    if res["stopped"] == "filter":
        return (f"rieltor: категорію «{res['scope']}» не вдалося виділити — {res['filter_error']}. "
                "Нічого не оновлено (щоб не витратити добовий ліміт на інші оголошення). "
                "Надішліть /logs — підправлю")
    left = res["total"] - res["ok"]
    where = f" у категорії «{res['scope']}»" if res.get("scope") else ""
    if res["total"] == 0:
        return f"rieltor: активних оголошень{where} немає"
    t = f"rieltor: оновлено дати {res['ok']} з {res['total']} оголошень{where} (найстаріші першими)"
    if res["limit"]:
        t += f". Сайт повідомив про ліміт: «{res['limit_text']}». Не оновлено сьогодні: {left}"
    elif res["stopped"] == "cookie":
        t += ". Зупинено: cookie застаріли (кнопка «🍪 Cookies» → rieltor)"
    elif res["stopped"] == "fails":
        t += f". Зупинено після {MAX_CONSECUTIVE_FAILS} помилок поспіль"
    if res["failed"]:
        t += f". Помилок: {res['failed']}"
    if not res["date_field"]:
        t += ". ⚠️ Поле дати оновлення не знайдено — порядок за ID"
    return t


if __name__ == "__main__":
    cookie = input("Встав cookie з rieltor.ua (один раз): ").strip()
    if not cookie:
        print("Cookie не введено, зупиняюсь.")
        sys.exit(1)
    try:
        print(summary_text(refresh_until_limit(cookie)))
    except Exception as e:  # noqa: BLE001
        print(f"Критична помилка: {e}")
        sys.exit(1)
