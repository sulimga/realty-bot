// Зчитування оголошення зі сторінки OLX.
//
// Функції readMain і extract вставляються у вкладку через chrome.scripting.executeScript,
// тому вони мають бути самодостатніми: жодних імпортів і змінних ззовні.
//
// Джерела (по черзі, що знайшлось — те й беремо; нормалізація — у lib/normalize.js):
//   1) window.__PRERENDERED_STATE__ сторінки (readMain, світ MAIN);
//   2) публічне API OLX /api/v1/offers/<id>/ (той самий домен, з браузера користувача);
//   3) розмітка сторінки (DOM) і JSON-LD — запасний варіант.
// Дані продавця (ім'я, телефон, контакти) не збираються взагалі.

export const OLX = {
  id: "olx",
  name: "OLX",
  match: (url) => /^https:\/\/(www\.|m\.)?olx\.ua\/.*obyavlenie\/.+/.test(url || ""),
  readMain,
  extract,
};

// Світ MAIN (сторінка): лише читаємо змінні сторінки.
function readMain() {
  const KEEP = ["id", "url", "title", "description", "category", "params", "photos", "location",
    "price", "status", "isActive", "createdTime", "lastRefreshTime", "breadcrumbs"];
  const out = { stateType: typeof window.__PRERENDERED_STATE__, ad: null, stateKeys: null, dlAdId: null };
  try {
    let st = window.__PRERENDERED_STATE__;
    if (typeof st === "string") st = JSON.parse(st);
    if (st && typeof st === "object") {
      out.stateKeys = Object.keys(st).slice(0, 40);
      let ad = st.ad && typeof st.ad === "object" ? (st.ad.ad || st.ad) : null;
      if (ad && typeof ad === "object" && (ad.id || ad.title)) {
        const picked = {};
        for (const k of KEEP) if (ad[k] !== undefined) picked[k] = ad[k];
        out.ad = JSON.parse(JSON.stringify(picked));
      }
    }
  } catch (e) {
    out.err = String((e && e.message) || e);
  }
  try {
    const dl = window.dataLayer;
    if (Array.isArray(dl)) {
      for (const x of dl) {
        const v = x && (x.ad_id || x.adId || x.offer_id || x.item_id);
        if (v && /^\d{5,}$/.test(String(v))) out.dlAdId = String(v);
      }
    }
  } catch (e) { /* немає dataLayer — не страшно */ }
  return out;
}

// Ізольований світ розширення: DOM, JSON-LD і запит до API OLX.
async function extract(main) {
  const res = { url: location.href, at: new Date().toISOString(), id: null, main: null, api: null,
    dom: null, ld: null, notes: [] };
  const note = (t) => res.notes.push(t);
  const txt = (el) => (el && el.innerText ? el.innerText.trim() : "");
  const q = (s) => document.querySelector(s);
  const meta = (p) => {
    const el = document.querySelector(`meta[property="${p}"]`) || document.querySelector(`meta[name="${p}"]`);
    return el ? el.content || "" : "";
  };
  const API_KEEP = ["id", "url", "title", "description", "category", "params", "photos", "location", "status"];

  if (main && main.err) note("стан сторінки: " + main.err);
  if (main) res.stateKeys = main.stateKeys || null;

  // 1) Стан сторінки — лише якщо він саме про це оголошення (OLX може змінити адресу без перезавантаження).
  const urlCode = (location.pathname.match(/-ID([0-9A-Za-z]+)\.html/) || [])[1] || null;
  let stAd = main && main.ad;
  if (stAd && urlCode && stAd.url && !String(stAd.url).includes("ID" + urlCode)) {
    note("стан сторінки від іншого оголошення — не використовую");
    stAd = null;
  }
  res.main = stAd || null;

  // 2) Числовий ID оголошення: підвал сторінки «ID: 123…», стан, dataLayer.
  const foot = q('[data-cy="ad-footer-bar-section"]') || q('[data-testid="ad-footer-bar-section"]');
  let m = txt(foot).match(/ID[:\s]*(\d{5,})/);
  if (!m) m = (document.body ? document.body.innerText : "").match(/\bID:\s*(\d{5,})/);
  if (m) res.id = m[1];
  else if (stAd && stAd.id) res.id = String(stAd.id);
  else if (main && main.dlAdId) res.id = main.dlAdId;
  if (!res.id) note("не знайшов числовий ID оголошення");

  if (res.id) {
    try {
      const r = await fetch(`${location.origin}/api/v1/offers/${res.id}/`, {
        headers: { Accept: "application/json", "Accept-Language": "uk" },
        credentials: "include",
      });
      if (r.ok) {
        const j = await r.json();
        const d = (j && j.data) || j;
        if (d && typeof d === "object") {
          const picked = {};
          for (const k of API_KEEP) if (d[k] !== undefined) picked[k] = d[k];
          res.api = picked;
        }
      } else note("API OLX: HTTP " + r.status);
    } catch (e) {
      note("API OLX: " + ((e && e.message) || e));
    }
  }

  // 3) Розмітка сторінки.
  const dom = {};
  dom.title = txt(q('[data-cy="ad_title"]')) || txt(q('[data-testid="ad_title"]')) || txt(q('[data-cy="offer_title"]')) || txt(q("h1")) || meta("og:title");
  dom.price = txt(q('[data-testid="ad-price-container"] h3')) || txt(q('[data-testid="ad-price-container"]'));
  const descEl = q('[data-cy="ad_description"] > div') || q('[data-cy="ad_description"]') || q('[data-testid="ad_description"]');
  dom.description = txt(descEl);
  const pc = q('[data-testid="ad-parameters-container"]') || q('[data-cy="ad-parameters"]') || q('[data-testid="ad-parameters"]');
  const params = [];
  if (pc) {
    for (const el of pc.querySelectorAll("p, li")) {
      if (el.querySelector("p, li")) continue;
      const t = txt(el);
      if (t && !params.includes(t)) params.push(t);
    }
  }
  dom.params = params;
  const bc = q('[data-testid="breadcrumbs"]') || q('[data-cy="breadcrumbs"]') || q('nav[aria-label*="breadcrumb" i]');
  dom.breadcrumbs = bc ? [...bc.querySelectorAll("a, li > span")].map(txt).filter(Boolean) : [];
  const locEl = q('[data-testid="map-aside-section"]') || q('[data-cy="ad-location"]') || q('[data-testid="location-date"]');
  dom.location = txt(locEl).split("\n").map((s) => s.trim()).filter(Boolean).slice(0, 4);

  // Фото — лише з галереї оголошення (не з блоку «схожі оголошення»).
  const photos = [];
  const addPhoto = (u) => {
    if (!u || !/olxcdn\.com|akamaized\.net/.test(u)) return;
    u = u.replace(/^\/\//, "https://");
    if (!photos.includes(u)) photos.push(u);
  };
  const gal = q('[data-testid="image-galery-container"]') || q('[data-testid="ad-photo"]') || q('[data-cy="adPhotos-swiperSlide"]')?.parentElement || q(".swiper-wrapper");
  if (gal) {
    for (const img of gal.querySelectorAll("img")) {
      const set = (img.getAttribute("srcset") || img.getAttribute("data-srcset") || "").split(",").map((s) => s.trim().split(/\s+/)[0]).filter(Boolean);
      addPhoto(set.length ? set[set.length - 1] : img.getAttribute("data-src") || img.currentSrc || img.src);
    }
  }
  dom.photos = photos;
  dom.ogImage = meta("og:image");
  res.dom = dom;

  // JSON-LD (Product / BreadcrumbList).
  const ld = { product: null, breadcrumbs: [] };
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    let j;
    try { j = JSON.parse(s.textContent); } catch (e) { continue; }
    const items = Array.isArray(j) ? j : j && j["@graph"] ? j["@graph"] : [j];
    for (const it of items) {
      if (!it || typeof it !== "object") continue;
      if (it["@type"] === "Product" && !ld.product) {
        const o = Array.isArray(it.offers) ? it.offers[0] : it.offers || {};
        ld.product = {
          name: it.name || "", description: it.description || "", category: it.category || "",
          image: Array.isArray(it.image) ? it.image : it.image ? [it.image] : [],
          price: o.price != null ? o.price : null, currency: o.priceCurrency || "",
          area: (o.areaServed && (o.areaServed.name || o.areaServed)) || "",
        };
      }
      if (it["@type"] === "BreadcrumbList" && Array.isArray(it.itemListElement)) {
        ld.breadcrumbs = it.itemListElement.map((x) => (x && (x.name || (x.item && x.item.name))) || "").filter(Boolean);
      }
    }
  }
  res.ld = ld;
  return res;
}
