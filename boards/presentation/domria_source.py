"""
Зчитування оголошення dom.ria (DIM.RIA) з сервера, без входу в акаунт.

Посилання: https://dom.ria.com/uk/realty-prodaja-kvartira-kiev-...-35131292.html (ID — число в кінці).
Сторінку завжди беремо українською (/uk/). Джерела, усі що знайдуться (нормалізація — normalize_domria.py):
  1) JSON сайту по ID (кандидати API_CANDIDATES — пробуємо по черзі; ⚠️ на живому сайті не перевірені);
  2) JSON, вбудований у сторінку (будь-який об'єкт зі "realty_id": <ID> у <script>);
  3) JSON-LD (schema.org) і meta og:*;
  4) фото з розмітки: cdn.riastatic.com/photosnew/dom/photo/<slug>__<photoId>xl.jpg (формат перевірено 10.11.2026).
Дані продавця/рієлтора (телефони, ім'я, агенція) не зберігаються.
"""

import json
import os
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

AD_URL_RE = re.compile(r"https?://(?:www\.)?dom\.ria\.com/(?:[a-z]{2}/)?realty-[^\s?#]*?-(\d{5,})\.html\S*", re.I)
API_CANDIDATES = [
    "https://dom.ria.com/node/searchEngine/v2/view/realty/{id}?lang_id=4",
    "https://dom.ria.com/node/getCouchRealtyById/{id}/",
]
PHOTO_RE = re.compile(r"https://cdn\.riastatic\.com/photosnew/dom/photo/([a-z0-9-]+)__(\d+)(xl|fl|xg|b|f|m)\.(?:jpg|webp)", re.I)
PRIVATE_KEYS = re.compile(r"phone|email|^user$|^user_(?!newbuild)|userid|owner|agency|realtor|contact|seller", re.I)


def new_session():
    try:
        from curl_cffi import requests as creq
        return creq.Session(impersonate="chrome")
    except ImportError:
        import requests
        s = requests.Session()
        s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
        return s


def is_ad_url(url):
    return bool(AD_URL_RE.match(str(url or "").strip()))


def ad_id(url):
    m = AD_URL_RE.match(str(url or "").strip())
    return m.group(1) if m else None


def uk_url(url):
    """Будь-яка мовна версія -> українська сторінка (/uk/realty-…), без параметрів."""
    p = urlparse(url.strip())
    path = re.sub(r"^/(?:[a-z]{2}/)?(realty-)", r"/uk/\1", p.path)
    return f"https://dom.ria.com{path}"


def strip_private(o):
    """Прибирає з даних телефони, імена/ID продавця, агенції (рекурсивно)."""
    if isinstance(o, dict):
        return {k: strip_private(v) for k, v in o.items() if not PRIVATE_KEYS.search(str(k))}
    if isinstance(o, list):
        return [strip_private(x) for x in o]
    return o


def find_realty(o, rid, depth=0):
    """Шукає в JSON словник оголошення з realty_id == rid (або realtyId)."""
    if depth > 12:
        return None
    if isinstance(o, dict):
        for k in ("realty_id", "realtyId"):
            if str(o.get(k) or "") == str(rid) and len(o) > 5:
                return o
        for v in o.values():
            r = find_realty(v, rid, depth + 1)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o[:200]:
            r = find_realty(v, rid, depth + 1)
            if r is not None:
                return r
    return None


def embedded_json(html, rid):
    """Об'єкт оголошення з JSON, вбудованого в <script> (window.__…__ = {…}, <script type=application/json> тощо)."""
    dec = json.JSONDecoder()
    best = None
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        body = m.group(1)
        if str(rid) not in body or ("realty_id" not in body and "realtyId" not in body):
            continue
        # кожна позиція «{» або «JSON.parse("…")» — пробуємо розібрати й знайти потрібний об'єкт
        starts = [i for i in (body.find("{"),) if i >= 0]
        starts += [mm.end() - 1 for mm in re.finditer(r"=\s*\{", body)]
        for st in sorted(set(starts)):
            try:
                val, _ = dec.raw_decode(body, st)
            except ValueError:
                continue
            r = find_realty(val, rid)
            if r is not None and (best is None or len(r) > len(best)):
                best = r
        for mm in re.finditer(r'JSON\.parse\(\s*("(?:[^"\\]|\\.)*")\s*\)', body):
            try:
                val = json.loads(json.loads(mm.group(1)))
            except ValueError:
                continue
            r = find_realty(val, rid)
            if r is not None and (best is None or len(r) > len(best)):
                best = r
    return best


def parse_page(html, url, rid):
    res = {"embedded": None, "ld": [], "dom": {}, "notes": []}
    try:
        res["embedded"] = embedded_json(html, rid)
    except Exception as e:  # noqa: BLE001
        res["notes"].append(f"вбудований JSON: {e}")
    soup = BeautifulSoup(html, "lxml")
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            j = json.loads(s.string or s.get_text() or "")
        except ValueError:
            continue
        items = j if isinstance(j, list) else j.get("@graph", [j]) if isinstance(j, dict) else []
        res["ld"] += [x for x in items if isinstance(x, dict)]

    def meta(p):
        el = soup.find("meta", attrs={"property": p}) or soup.find("meta", attrs={"name": p})
        return (el.get("content") or "").strip() if el else ""

    h1 = soup.find("h1")
    slug = (re.search(r"/realty-(.+)-\d+\.html", url) or [None, ""])[1]
    photos, seen = [], set()
    for m in PHOTO_RE.finditer(html):
        pslug, pid, size = m.group(1), m.group(2), m.group(3).lower()
        if pid in seen or (slug and pslug and pslug != slug and not slug.startswith(pslug)):
            continue
        seen.add(pid)
        photos.append({"id": pid, "url": f"https://cdn.riastatic.com/photosnew/dom/photo/{pslug}__{pid}xl.jpg", "size": size})
    res["dom"] = {"h1": h1.get_text(" ", strip=True) if h1 else "", "title": meta("og:title") or (soup.title.string if soup.title else ""),
                  "description": meta("og:description") or meta("description"), "og_image": meta("og:image"),
                  "photos": photos, "slug": slug}
    return res


def _secrets_cookie(data_dir):
    for name in ("domria_cookies.txt",):
        p = os.path.join(data_dir or "", "secrets", name)
        try:
            with open(p, encoding="utf-8") as f:
                c = f.read().strip()
            if c:
                return c
        except OSError:
            pass
    return None


def fetch_api(s, rid, cookie=None, log=print):
    notes = []
    for tpl in API_CANDIDATES:
        url = tpl.format(id=rid)
        try:
            h = {"Accept": "application/json", "Accept-Language": "uk", "Referer": "https://dom.ria.com/uk/"}
            if cookie:
                h["Cookie"] = cookie
            r = s.get(url, headers=h, timeout=40)
            if r.status_code != 200:
                notes.append(f"{url.split('/node/')[1].split('{')[0][:40]}: HTTP {r.status_code}")
                continue
            d = r.json()
        except Exception as e:  # noqa: BLE001
            notes.append(f"{url.split('/node/')[1][:40]}: {type(e).__name__}")
            continue
        d = d.get("data", d) if isinstance(d, dict) and isinstance(d.get("data"), dict) else d
        found = find_realty(d, rid) or (d if isinstance(d, dict) and ("realty_type_id" in d or "characteristics_values" in d) else None)
        if found:
            return found, url, notes
        notes.append(f"{url.split('/node/')[1][:40]}: не те (ключі: {list(d)[:8] if isinstance(d, dict) else type(d).__name__})")
    return None, None, notes


def fetch(url, session=None, data_dir=None, log=print):
    """Сторінка + JSON. Повертає (raw, html). RuntimeError — якщо сторінка не відкрилась."""
    rid = ad_id(url)
    if not rid:
        raise RuntimeError("не знайшов ID оголошення в посиланні dom.ria")
    s = session or new_session()
    page = uk_url(url)
    r = s.get(page, headers={"Accept-Language": "uk-UA,uk;q=0.9"}, timeout=40, allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"dom.ria не віддав сторінку оголошення (HTTP {r.status_code}). "
                           + ("Оголошення видалене?" if r.status_code in (404, 410) else ""))
    html = r.text
    raw = {"source": "domria", "url": page, "id": rid, "api": None, "api_url": None, "notes": []}
    raw.update(parse_page(html, page, rid))
    api, api_url, notes = fetch_api(s, rid, log=log)
    if not api:
        cookie = _secrets_cookie(data_dir)
        if cookie:
            api, api_url, notes2 = fetch_api(s, rid, cookie=cookie, log=log)
            notes += [n + " (з cookie)" for n in notes2]
    raw["api"], raw["api_url"] = api, api_url
    raw["notes"] += notes if not api else []
    raw["api"] = strip_private(raw["api"]) if raw["api"] else None
    raw["embedded"] = strip_private(raw["embedded"]) if raw["embedded"] else None
    log("Дані: " + ", ".join(k for k in ("api", "embedded") if raw.get(k))
        + f", фото на сторінці: {len(raw['dom']['photos'])}" + (f" ({'; '.join(raw['notes'])})" if raw["notes"] else ""))
    return raw, html
