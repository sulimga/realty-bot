// Перетворює сирі дані зі сторінки OLX (sources/olx.js → extract) на модель презентації.
// Чисті функції без chrome.* і DOM — їх проганяють тести в Node (test/normalize.test.js).

import { OLX_CATS } from "./categories.js";

// Параметри OLX, які не показуємо клієнту (службові або про умови продавця).
const SKIP_KEYS = new Set(["price", "commission", "cooperate", "is_exchange", "private_business",
  "currency", "sell_or_rent"]);
const SKIP_LABEL = /ріелтор|риелтор|комісі|комисси|приватна особа|частное лицо|^бізнес$|^бизнес$|обмін|обмен/i;

// Тип об'єкта для листових категорій комерції (див. boards/olx/categories.json).
const COMM_TYPES = {
  3679: "Торгове приміщення", 3692: "Торгове приміщення",
  3680: "МАФ", 3695: "МАФ",
  3681: "Офіс", 3690: "Офіс",
  3682: "Склад / ангар", 3693: "Склад / ангар",
  3683: "Кафе / ресторан", 3694: "Кафе / ресторан",
  3684: "Готель / база відпочинку",
  3685: "СТО / автомийка / АЗС", 3696: "СТО / автомийка / АЗС",
  3686: "Салон краси / клініка", 3697: "Салон / кабінет краси",
  3687: "Виробниче приміщення", 3691: "Виробниче приміщення",
  3688: "Приміщення вільного призначення", 3700: "Приміщення вільного призначення",
  3689: "Комерційне приміщення", 3701: "Комерційне приміщення",
  3698: "Зал / спортивне приміщення",
  3699: "Медичне приміщення",
  3298: "Коворкінг",
};

// Для запасного варіанта (параметри з розмітки сторінки: «Поверх: 5»).
const LABEL_KEYS = [
  [/^(загальна площа|общая площадь)/i, "total_area"],
  [/(площа кухні|площадь кухни)/i, "kitchen_area"],
  [/(площа ділянки|площадь участка)/i, "land_area"],
  [/(площа будинку|площадь дома)/i, "house_area"],
  [/^(поверховість|этажность)/i, "total_floors"],
  [/^(поверх|этаж)$/i, "floor"],
  [/(кількість кімнат|количество комнат)/i, "number_of_rooms_string"],
  [/^(назва жк|название жк)/i, "zkh"],
  [/^(ціна|цена)$/i, "price"],
];

export const CUR_SYMBOL = { USD: "$", EUR: "€", UAH: "грн" };

// ---------- дрібні помічники ----------

const ENT = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", laquo: "«", raquo: "»", ndash: "–", mdash: "—", hellip: "…" };

export function htmlToText(s) {
  if (!s) return "";
  return String(s)
    .replace(/\r\n?/g, "\n")
    .replace(/<br\s*\/?>[ \t]*\n?/gi, "\n")
    .replace(/<\/(p|div|li|h\d)>/gi, "\n")
    .replace(/<li[^>]*>/gi, "• ")
    .replace(/<[^>]+>/g, "")
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(+d))
    .replace(/&([a-z]+);/gi, (m, n) => ENT[n.toLowerCase()] ?? m);
}

const clean = (s) => String(s ?? "").replace(/[  ]/g, " ").replace(/[ \t]+/g, " ").trim();
const stripEmoji = (s) => clean(String(s ?? "").replace(/[\p{Extended_Pictographic}\u{FE0F}\u{200D}]/gu, ""));

export function toNum(v) {
  if (v == null) return null;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  const m = String(v).replace(/[  ]/g, " ").match(/\d[\d ]*(?:[.,]\d+)?/);
  if (!m) return null;
  const n = parseFloat(m[0].replace(/ /g, "").replace(",", "."));
  return Number.isFinite(n) ? n : null;
}

export function fmtNum(n) {
  const r = Math.round(n);
  return String(r).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

// «95 000 $» → { value: 95000, currency: "USD" }
export function parsePrice(text) {
  const t = String(text || "");
  let currency = "";
  if (/\$|usd|дол/i.test(t)) currency = "USD";
  else if (/€|eur|євро|евро/i.test(t)) currency = "EUR";
  else if (/грн|uah|₴/i.test(t)) currency = "UAH";
  return { value: toNum(t), currency };
}

// Прибирає з опису телефони, посилання, email і @нікнейми (щоб клієнт не вийшов на продавця напряму).
export function stripContacts(text) {
  let t = String(text || "");
  t = t.replace(/https?:\/\/\S+|www\.\S+/gi, "");
  t = t.replace(/[\w.+-]+@[\w-]+\.[\w.-]+/g, "");
  t = t.replace(/(^|[\s(])@[A-Za-z0-9_]{4,}/g, "$1");
  t = t.replace(/(?:\+?\s*3\s*8\s*)?(?:\(\s*)?0\d{2}(?:\s*\))?[\s.\-–]*\d{3}[\s.\-–]*\d{2}[\s.\-–]*\d{2}(?!\d)/g, "");
  // рядки, від яких лишились самі «Телефонуйте: (Viber)», «Telegram:» тощо
  const W = "тел\\.?|телефон|телефонуйте|дзвоніть|звоніть|звоните|пишіть|пишите|phone|tel|viber|вайбер|telegram|телеграм|whatsapp|ватсап|або|или|чи|в|у|на";
  t = t.replace(new RegExp(`^[ \\t]*(?:(?:${W})[ \\t:.,;\\-–/()|]*)+$`, "gim"), "");
  return t;
}

export function tidyText(text) {
  return String(text || "")
    .split("\n").map((l) => l.replace(/[ \t ]+/g, " ").trim()).join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

// ---------- категорія → тип угоди / тип об'єкта / призначення ----------

function catChain(id) {
  const out = [];
  let cur = Number(id) || 0;
  for (let i = 0; cur && OLX_CATS[cur] && i < 10; i++) {
    out.push({ id: cur, name: OLX_CATS[cur][0] });
    cur = OLX_CATS[cur][1];
  }
  return out;
}

export function classify(catId, crumbsText, P) {
  const val = (k) => (P.find((p) => p.key === k) || {}).text || "";
  const has = (k) => P.some((p) => p.key === k);
  const chain = catChain(catId);
  const ids = chain.map((c) => c.id);
  const text = (chain.length ? chain.map((c) => c.name).join(" / ") : crumbsText || "").toLowerCase();

  let group = ids.includes(1757) ? "flat" : ids.includes(1754) ? "room" : ids.includes(206) ? "house"
    : ids.includes(1606) ? "land" : ids.includes(1610) ? "commercial" : ids.includes(1616) ? "parking"
    : ids.includes(1522) ? "abroad" : "";
  if (!group && text) {
    if (/квартир/.test(text)) group = "flat";
    else if (/кімнат|комнат/.test(text)) group = "room";
    else if (/будин|котедж|дач|(^|[^а-яіїєґ])дом/.test(text)) group = "house";
    else if (/земл|ділянк|участ/.test(text)) group = "land";
    else if (/комерц|коммерч|офіс|офис|склад|магазин|приміщ|помещ/.test(text)) group = "commercial";
    else if (/гараж|паркін|паркинг|парков/.test(text)) group = "parking";
  }
  if (!group) {
    if (has("number_of_rooms_string")) group = "flat";
    else if (has("property_type_houses")) group = "house";
    else if (has("property_type_land")) group = "land";
    else if (has("comm_re_type") || has("comm_re_location")) group = "commercial";
    else if (has("property_type_parking")) group = "parking";
  }

  const deal = /оренд|аренд|коворк/.test(text) ? "Оренда" : /продаж/.test(text) ? "Продаж" : val("sell_or_rent");
  const used = [];
  let objectType = "";
  let purpose = "";
  const take = (k) => { const v = val(k); if (v) used.push(k); return v; };
  switch (group) {
    case "flat": objectType = "Квартира"; purpose = "Житлова"; break;
    case "room": objectType = "Кімната"; purpose = "Житлова"; break;
    case "house": objectType = take("property_type_houses") || "Будинок"; purpose = "Житлова"; break;
    case "land": objectType = "Земельна ділянка"; purpose = take("property_type_land") || "Земля"; break;
    case "commercial": {
      let t = COMM_TYPES[ids[0]] || "";
      if (!t) {
        for (const [re, name] of [[/офіс|офис/, "Офіс"], [/склад|ангар/, "Склад / ангар"], [/торгов|магазин/, "Торгове приміщення"],
          [/виробн|производ/, "Виробниче приміщення"], [/кафе|ресторан/, "Кафе / ресторан"], [/вільного|свободного/, "Приміщення вільного призначення"]]) {
          if (re.test(text)) { t = name; break; }
        }
      }
      objectType = t || "Комерційне приміщення";
      purpose = "Нежитлова (комерційна)";
      break;
    }
    case "parking": objectType = take("property_type_parking") || "Гараж / паркомісце"; purpose = "Нежитлова"; break;
    case "abroad": {
      objectType = take("property_type_abroad") || "Нерухомість за кордоном";
      purpose = /комерц/i.test(objectType) ? "Нежитлова (комерційна)" : /земл/i.test(objectType) ? "Земля" : "Житлова";
      break;
    }
  }
  return { group, deal, objectType, purpose, usedKeys: used };
}

// ---------- параметри ----------

function paramFromObj(p) {
  if (!p || typeof p !== "object") return null;
  const key = String(p.key || p.code || "");
  const label = clean(p.name || p.label || "");
  const v = p.value;
  let text = "";
  let num = null;
  if (v && typeof v === "object" && !Array.isArray(v)) {
    text = v.label ?? (v.value != null ? String(v.value) : v.key ?? "");
    num = toNum(v.key ?? v.value);
  } else if (Array.isArray(v)) {
    text = v.map((x) => (x && typeof x === "object" ? x.label || x.value || x.key : x)).filter(Boolean).join(", ");
  } else if (v != null) {
    text = String(v);
  }
  if (num == null) num = toNum(p.normalizedValue ?? text);
  return { key, label, text: clean(text), num, value: v };
}

function paramFromText(s) {
  const i = String(s).indexOf(":");
  if (i < 1) return null;
  const label = clean(s.slice(0, i));
  const text = clean(s.slice(i + 1));
  const hit = LABEL_KEYS.find(([re]) => re.test(label));
  return { key: hit ? hit[1] : "", label, text, num: toNum(text) };
}

export function collectParams(raw) {
  const api = raw.api || {};
  const st = raw.main || {};
  const src = Array.isArray(api.params) && api.params.length ? api.params
    : Array.isArray(st.params) && st.params.length ? st.params : null;
  if (src) return src.map(paramFromObj).filter((p) => p && (p.label || p.key));
  return ((raw.dom && raw.dom.params) || []).map(paramFromText).filter(Boolean);
}

function withUnit(p, unit) {
  if (!p || !p.text) return "";
  return /^\d+(?:[.,]\d+)?$/.test(p.text) ? `${p.text} ${unit}` : p.text;
}

// ---------- ціна, адреса, опис, фото ----------

function pickPrice(raw, P, deal) {
  const pp = P.find((p) => p.key === "price");
  const st = raw.main || {};
  const ld = (raw.ld && raw.ld.product) || {};
  let text = "";
  let value = null;
  let currency = "";
  if (pp && pp.value && typeof pp.value === "object") {
    text = clean(pp.value.label || "");
    value = toNum(pp.value.value);
    currency = pp.value.currency || "";
  } else if (pp && pp.text) {
    text = pp.text;
  }
  if (!text && st.price) {
    text = clean(st.price.displayValue || "");
    const rp = st.price.regularPrice || {};
    value = value ?? toNum(rp.value);
    currency = currency || rp.currencyCode || "";
  }
  if (!text && raw.dom && raw.dom.price) text = clean(raw.dom.price.split("\n")[0]);
  if (!text && ld.price != null) {
    value = toNum(ld.price);
    currency = ld.currency || "";
    if (value != null) text = `${fmtNum(value)} ${CUR_SYMBOL[currency] || currency}`.trim();
  }
  if (text && deal === "Оренда" && !/міс|мес/i.test(text)) text += " / міс.";
  return text;
}

export function formatAddress(city, district, region) {
  city = clean(city);
  district = clean(district);
  region = clean(region);
  const parts = [];
  if (city) parts.push(city);
  if (district && district.toLowerCase() !== city.toLowerCase()) parts.push(/район|р-н/i.test(district) ? district : `${district} район`);
  if (region && (!city || !region.toLowerCase().startsWith(city.toLowerCase().slice(0, 4)))) parts.push(region);
  return parts.join(", ");
}

function pickAddress(raw) {
  const al = (raw.api && raw.api.location) || null;
  if (al && (al.city || al.district)) {
    return formatAddress(al.city && al.city.name, al.district && al.district.name, al.region && al.region.name);
  }
  const sl = (raw.main && raw.main.location) || null;
  if (sl && (sl.cityName || sl.districtName)) return formatAddress(sl.cityName, sl.districtName, sl.regionName);
  const dl = (raw.dom && raw.dom.location) || [];
  if (dl.length) {
    // «Київ, Печерський» + «Київська область»; дату («Сьогодні о …») і «Показати на карті» відкидаємо
    const lines = dl.filter((l) => !/\d{1,2}:\d{2}|сьогодні|сегодня|вчора|вчера|карт|місцезнаходження|местоположение/i.test(l));
    if (lines.length) {
      const [first, second] = lines;
      const [city, district] = first.split(/\s*[,–-]\s*/);
      return formatAddress(city, district, second && /обл/i.test(second) ? second : "");
    }
  }
  const ld = (raw.ld && raw.ld.product) || {};
  return clean(typeof ld.area === "string" ? ld.area : "");
}

function pickDescription(raw) {
  const cands = [raw.api && raw.api.description, raw.main && raw.main.description, raw.dom && raw.dom.description,
    raw.ld && raw.ld.product && raw.ld.product.description];
  for (const c of cands) if (c && String(c).trim()) return tidyText(htmlToText(c));
  return "";
}

function photoKey(u) {
  const m = String(u).match(/\/files\/([^/]+)\//);
  return m ? m[1] : String(u).split("?")[0];
}

function sizeLink(link, w, h) {
  let W = 1280, H = 960;
  if (w && h) {
    const k = Math.min(1, 1600 / Math.max(w, h));
    W = Math.round(w * k);
    H = Math.round(h * k);
  }
  return String(link).replace("{width}", W).replace("{height}", H);
}

export function pickPhotos(raw) {
  const out = [];
  const seen = new Set();
  const add = (u) => {
    if (!u || typeof u !== "string") return;
    u = u.replace(/^\/\//, "https://").replace(/(olxcdn\.com|akamaized\.net):443\//, "$1/");
    if (!/^https:\/\//.test(u)) return;
    const k = photoKey(u);
    if (seen.has(k)) return;
    seen.add(k);
    out.push(u);
  };
  const api = raw.api || {};
  if (Array.isArray(api.photos) && api.photos.length) {
    for (const p of api.photos) add(p && p.link ? sizeLink(p.link, p.width, p.height) : typeof p === "string" ? p : "");
  }
  if (!out.length && raw.main && Array.isArray(raw.main.photos)) {
    for (const p of raw.main.photos) add(typeof p === "string" ? p : p && (p.link ? sizeLink(p.link, p.width, p.height) : p.url));
  }
  if (!out.length && raw.dom && Array.isArray(raw.dom.photos)) raw.dom.photos.forEach(add);
  if (!out.length && raw.ld && raw.ld.product) raw.ld.product.image.forEach(add);
  if (!out.length && raw.dom && raw.dom.ogImage) add(raw.dom.ogImage);
  return out;
}

// ---------- головне ----------

export function normalizeOlx(raw) {
  raw = raw || {};
  const P = collectParams(raw);
  const catId = (raw.api && raw.api.category && raw.api.category.id) || (raw.main && raw.main.category && raw.main.category.id) || null;
  const crumbs = [...((raw.dom && raw.dom.breadcrumbs) || []), ...((raw.ld && raw.ld.breadcrumbs) || []),
    (raw.ld && raw.ld.product && raw.ld.product.category) || ""].join(" / ");
  const cls = classify(catId, crumbs, P);
  const address = pickAddress(raw);
  const by = (k) => P.find((p) => p.key === k);

  const rows = [];
  const add = (key, label, value) => {
    value = stripEmoji(value);
    if (value) rows.push({ key, label, value });
  };
  add("address", "Адреса", address);
  add("deal", "Тип угоди", cls.deal);
  add("object_type", "Тип об'єкта", cls.objectType);
  add("purpose", "Призначення", cls.purpose);
  add("total_area", "Загальна площа", withUnit(by("total_area"), "м²"));
  add("house_area", "Площа будинку", withUnit(by("house_area"), "м²"));
  add("kitchen_area", "Площа кухні", withUnit(by("kitchen_area"), "м²"));
  add("land_area", "Площа ділянки", withUnit(by("land_area"), "сот."));
  add("rooms", "Кількість кімнат", (by("number_of_rooms_string") || by("number_of_rooms") || {}).text);
  add("floor", "Поверх", (by("floor") || {}).text);
  add("total_floors", "Поверховість", (by("total_floors") || {}).text);
  const used = new Set(["total_area", "house_area", "kitchen_area", "land_area", "number_of_rooms_string",
    "number_of_rooms", "floor", "total_floors", ...cls.usedKeys]);
  for (const p of P) {
    if (!p.label || !p.text || used.has(p.key) || SKIP_KEYS.has(p.key) || SKIP_LABEL.test(p.label)) continue;
    if (rows.some((r) => r.label === p.label)) continue;
    add(p.key || "", p.label, p.text);
  }

  const description = pickDescription(raw);
  const title = clean((raw.api && raw.api.title) || (raw.main && raw.main.title) || (raw.dom && raw.dom.title) || "");
  return {
    v: 1,
    source: "OLX",
    sourceUrl: raw.url || "",
    sourceId: raw.id || (raw.api && raw.api.id) || (raw.main && raw.main.id) || null,
    originalTitle: title,
    kicker: [cls.deal, cls.objectType].filter(Boolean).join(" / ") || title,
    group: cls.group,
    price: { text: pickPrice(raw, P, cls.deal), show: true },
    rows,
    descriptionOriginal: description,
    description: tidyText(stripContacts(description)),
    cleanContacts: true,
    photos: pickPhotos(raw).map((url) => ({ url, on: true })),
    showAgent: true,
    telegraph: null,
    notes: raw.notes || [],
  };
}

// ---------- похідні значення (рахуються наживо з рядків, які редагує користувач) ----------

const rowVal = (rows, k) => ((rows || []).find((r) => r.key === k) || {}).value || "";

export function quickFacts(rows) {
  const q = [];
  const area = rowVal(rows, "total_area");
  const rooms = rowVal(rows, "rooms");
  const floor = rowVal(rows, "floor");
  const floors = rowVal(rows, "total_floors");
  const land = rowVal(rows, "land_area");
  if (area) q.push({ icon: "area", text: area });
  if (rooms) {
    const m = rooms.match(/^(\d+\+?)/);
    q.push({ icon: "rooms", text: m ? `${m[1]} кімн.` : rooms });
  }
  if (floor && floors) q.push({ icon: "floor", text: `${toNum(floor) ?? floor}/${toNum(floors) ?? floors} пов.` });
  else if (floor) q.push({ icon: "floor", text: `${floor} пов.` });
  else if (floors) q.push({ icon: "floor", text: `${toNum(floors) ?? floors}-поверх.` });
  if (land) q.push({ icon: "land", text: land });
  return q;
}

// Ціна за м² (для ділянки — за сотку), як у презентаціях CRM.
export function unitPrice(model) {
  if (!model || !model.price || !model.price.text) return "";
  const { value, currency } = parsePrice(model.price.text);
  if (!value || !currency) return "";
  const sym = CUR_SYMBOL[currency] || currency;
  const land = toNum(rowVal(model.rows, "land_area"));
  if (model.group === "land" && land) return `${fmtNum(value / land)} ${sym}/сот.`;
  const area = toNum(rowVal(model.rows, "total_area"));
  if (area) return `${fmtNum(value / area)} ${sym}/м²`;
  return "";
}

export function addressOf(model) {
  return rowVal(model && model.rows, "address");
}

export function presentationTitle(model) {
  const area = rowVal(model.rows, "total_area");
  const head = [model.kicker, area].filter(Boolean).join(", ");
  const addr = addressOf(model);
  return (addr ? `${head} — ${addr}` : head).slice(0, 250) || "Презентація об'єкта";
}
