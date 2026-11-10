"""
Зчитування оголошення OLX з сервера (без входу в акаунт): сторінка оголошення + публічне API.

Порядок, як у розширенні Chrome (extension/olx-presentation/sources/olx.js):
  1) window.__PRERENDERED_STATE__ зі сторінки (якщо є);
  2) GET https://www.olx.ua/api/v1/offers/<числовий ID>/ (ID — «ID: …» на сторінці або зі стану);
  3) розмітка сторінки (data-cy / data-testid) і JSON-LD — запасний варіант.
OLX за CloudFront — звичайні запити Python отримують 403, тому curl_cffi з імітацією Chrome.
Дані продавця (user, contact) не зберігаються.
"""

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

KEEP = ["id", "url", "title", "description", "category", "params", "photos", "location", "price", "status"]
AD_URL_RE = re.compile(r"https?://(?:www\.|m\.)?olx\.ua/\S*obyavlenie/\S+", re.I)


def new_session():
    try:
        from curl_cffi import requests as creq
        return creq.Session(impersonate="chrome")
    except ImportError:          # без curl_cffi OLX, найпевніше, відповість 403
        import requests
        s = requests.Session()
        s.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
        return s


def is_ad_url(url):
    return bool(AD_URL_RE.match(str(url or "").strip()))


def _pick(d, keys):
    return {k: d[k] for k in keys if isinstance(d, dict) and k in d}


def parse_state(html):
    """window.__PRERENDERED_STATE__ = "…json у рядку…"; -> dict або None."""
    i = html.find("__PRERENDERED_STATE__")
    if i < 0:
        return None
    j = html.find("=", i)
    k = j + 1
    while k < len(html) and html[k] in " \t\r\n":
        k += 1
    if j < 0 or k >= len(html):
        return None
    dec = json.JSONDecoder()
    val, _ = dec.raw_decode(html, k)
    if isinstance(val, str):
        val = json.loads(val)
    return val if isinstance(val, dict) else None


def parse_page(html, url):
    """Усе, що можна взяти з HTML сторінки (без мережі). Повертає raw без api."""
    res = {"url": url, "id": None, "main": None, "api": None, "dom": None, "ld": None, "notes": []}
    note = res["notes"].append

    st = None
    try:
        st = parse_state(html)
    except ValueError as e:
        note(f"стан сторінки: {e}")
    if st:
        res["state_keys"] = list(st.keys())[:40]
        ad = st.get("ad")
        ad = ad.get("ad", ad) if isinstance(ad, dict) else None
        if isinstance(ad, dict) and (ad.get("id") or ad.get("title")):
            m = re.search(r"-ID([0-9A-Za-z]+)\.html", urlparse(url).path)
            if m and ad.get("url") and ("ID" + m.group(1)) not in str(ad.get("url")):
                note("стан сторінки від іншого оголошення — не використовую")
            else:
                res["main"] = _pick(ad, KEEP)

    soup = BeautifulSoup(html, "lxml")

    def txt(el):
        return el.get_text("\n", strip=True) if el else ""

    def sel(*css):
        for c in css:
            el = soup.select_one(c)
            if el:
                return el
        return None

    def meta(p):
        el = soup.find("meta", attrs={"property": p}) or soup.find("meta", attrs={"name": p})
        return (el.get("content") or "") if el else ""

    foot = sel('[data-cy="ad-footer-bar-section"]', '[data-testid="ad-footer-bar-section"]')
    m = re.search(r"ID[:\s]*(\d{5,})", txt(foot)) or re.search(r"\bID:\s*(\d{5,})", soup.get_text(" ", strip=True))
    if m:
        res["id"] = m.group(1)
    elif res["main"] and res["main"].get("id"):
        res["id"] = str(res["main"]["id"])
    else:
        m = re.search(r'"ad_id"\s*:\s*"?(\d{5,})', html)
        if m:
            res["id"] = m.group(1)

    dom = {}
    dom["title"] = txt(sel('[data-cy="ad_title"]', '[data-testid="ad_title"]', '[data-cy="offer_title"]', "h1")) or meta("og:title")
    pr = sel('[data-testid="ad-price-container"] h3', '[data-testid="ad-price-container"]')
    dom["price"] = txt(pr)
    dom["description"] = txt(sel('[data-cy="ad_description"] > div', '[data-cy="ad_description"]', '[data-testid="ad_description"]'))
    params = []
    pc = sel('[data-testid="ad-parameters-container"]', '[data-cy="ad-parameters"]', '[data-testid="ad-parameters"]')
    if pc:
        for el in pc.select("p, li"):
            if el.select_one("p, li"):
                continue
            t = el.get_text(" ", strip=True)
            if t and t not in params:
                params.append(t)
    dom["params"] = params
    bc = sel('[data-testid="breadcrumbs"]', '[data-cy="breadcrumbs"]', 'nav[aria-label*="readcrumb"]')
    dom["breadcrumbs"] = [t for t in (e.get_text(" ", strip=True) for e in (bc.select("a, li > span") if bc else [])) if t]
    loc = sel('[data-testid="map-aside-section"]', '[data-cy="ad-location"]', '[data-testid="location-date"]')
    dom["location"] = [ln for ln in txt(loc).split("\n") if ln.strip()][:4]
    photos = []
    gal = sel('[data-testid="image-galery-container"]', '[data-testid="ad-photo"]', ".swiper-wrapper")
    for img in (gal.select("img") if gal else []):
        srcset = [s.strip().split()[0] for s in (img.get("srcset") or img.get("data-srcset") or "").split(",") if s.strip()]
        u = srcset[-1] if srcset else img.get("data-src") or img.get("src") or ""
        u = re.sub(r"^//", "https://", u)
        if re.search(r"olxcdn\.com|akamaized\.net", u) and u not in photos:
            photos.append(u)
    dom["photos"] = photos
    dom["og_image"] = meta("og:image")
    res["dom"] = dom

    ld = {"product": None, "breadcrumbs": []}
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            j = json.loads(s.string or s.get_text() or "")
        except ValueError:
            continue
        items = j if isinstance(j, list) else j.get("@graph", [j]) if isinstance(j, dict) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("@type") == "Product" and not ld["product"]:
                o = it.get("offers") or {}
                o = o[0] if isinstance(o, list) and o else o if isinstance(o, dict) else {}
                area = o.get("areaServed")
                img = it.get("image")
                ld["product"] = {
                    "name": it.get("name") or "", "description": it.get("description") or "", "category": it.get("category") or "",
                    "image": img if isinstance(img, list) else [img] if img else [],
                    "price": o.get("price"), "currency": o.get("priceCurrency") or "",
                    "area": (area.get("name") if isinstance(area, dict) else area) or "",
                }
            if it.get("@type") == "BreadcrumbList" and isinstance(it.get("itemListElement"), list):
                ld["breadcrumbs"] = [x.get("name") or (x.get("item") or {}).get("name") or ""
                                     for x in it["itemListElement"] if isinstance(x, dict)]
    res["ld"] = ld
    if not res["id"]:
        note("не знайшов числовий ID оголошення на сторінці")
    return res


def fetch(url, session=None, log=print):
    """Завантажує сторінку й API. Повертає raw (як у розширенні). Кидає RuntimeError, якщо сторінка не відкрилась."""
    s = session or new_session()
    url = url.strip()
    r = s.get(url, headers={"Accept-Language": "uk-UA,uk;q=0.9"}, timeout=40, allow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"OLX не віддав сторінку оголошення (HTTP {r.status_code}). "
                           + ("Оголошення видалене?" if r.status_code in (404, 410) else ""))
    final = str(getattr(r, "url", url) or url)
    raw = parse_page(r.text, final)
    if raw["id"]:
        try:
            ra = s.get(f"https://www.olx.ua/api/v1/offers/{raw['id']}/",
                       headers={"Accept": "application/json", "Accept-Language": "uk"}, timeout=40)
            if ra.status_code == 200:
                d = ra.json()
                d = d.get("data", d) if isinstance(d, dict) else None
                if isinstance(d, dict):
                    raw["api"] = _pick(d, KEEP)
            else:
                raw["notes"].append(f"API OLX: HTTP {ra.status_code}")
        except Exception as e:  # noqa: BLE001 — API лише покращує дані, сторінки вистачає
            raw["notes"].append(f"API OLX: {e}")
    log("Дані: " + ", ".join(k for k in ("api", "main", "dom") if raw.get(k)) + (f" ({'; '.join(raw['notes'])})" if raw["notes"] else ""))
    return raw
