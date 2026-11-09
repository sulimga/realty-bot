// Тести нормалізації на штучних даних (форма — як у API OLX /api/v1/offers/<id>/ і в стані сторінки).
// Запуск: node test/normalize.test.js
import assert from "node:assert/strict";
import { normalizeOlx, stripContacts, quickFacts, unitPrice, presentationTitle, formatAddress, parsePrice } from "../lib/normalize.js";
import { buildTelegraph } from "../lib/telegraph.js";

let n = 0;
const t = (name, fn) => { fn(); n++; console.log("✓", name); };
const row = (m, k) => (m.rows.find((r) => r.key === k) || {}).value;

const API_FLAT = {
  url: "https://www.olx.ua/d/uk/obyavlenie/x-IDabc12.html", id: "900000001", notes: [],
  api: {
    id: 900000001, title: "Продам 3к квартиру!!!",
    description: "Гарна квартира<br />\nТелефон: +38 (067) 123-45-67<br /><br />Пишіть @owner_kyiv або https://t.me/x<br />Ціна 95 000 $, 2001-2010 р.",
    category: { id: 1758, type: "real_estate" },
    location: { city: { id: 268, name: "Київ" }, district: { id: 15, name: "Печерський" }, region: { id: 25, name: "Київська область" } },
    params: [
      { key: "price", name: "Ціна", type: "price", value: { value: 95000, currency: "USD", label: "95 000 $" } },
      { key: "private_business", name: "Приватна особа / Бізнес", type: "select", value: { key: "private", label: "Приватна особа" } },
      { key: "cooperate", name: "Готовий співпрацювати з ріелторами", type: "checkbox", value: { key: "1", label: "Так" } },
      { key: "property_type_appartments_sale", name: "Тип будинку", type: "select", value: { key: "2", label: "Сталінка" } },
      { key: "floor", name: "Поверх", type: "input", value: { key: "5", label: "5" } },
      { key: "total_floors", name: "Поверховість", type: "input", value: { key: "9", label: "9" } },
      { key: "total_area", name: "Загальна площа", type: "input", value: { key: "85", label: "85 м²" } },
      { key: "kitchen_area", name: "Площа кухні", type: "input", value: { key: "12", label: "12 м²" } },
      { key: "number_of_rooms_string", name: "Кількість кімнат", type: "select", value: { key: "trehkomnatnye", label: "3 кімнати" } },
      { key: "repair", name: "Ремонт", type: "select", value: { key: "2", label: "Євроремонт" } },
      { key: "pets", name: "Домашні улюбленці", type: "select", value: { key: "yes_cat", label: "Так, котик 😸" } },
    ],
    photos: [
      { id: 1, link: "https://ireland.apollo.olxcdn.com:443/v1/files/aaa-UA/image;s={width}x{height}", width: 2000, height: 1500 },
      { id: 2, link: "https://ireland.apollo.olxcdn.com:443/v1/files/bbb-UA/image;s={width}x{height}", width: 900, height: 1200 },
      { id: 3, link: "https://ireland.apollo.olxcdn.com:443/v1/files/aaa-UA/image;s={width}x{height}", width: 2000, height: 1500 },
    ],
  },
};

t("квартира з API: тип, призначення, адреса, площі, кімнати, поверх", () => {
  const m = normalizeOlx(API_FLAT);
  assert.equal(m.kicker, "Продаж / Квартира");
  assert.equal(row(m, "address"), "Київ, Печерський район");
  assert.equal(row(m, "purpose"), "Житлова");
  assert.equal(row(m, "total_area"), "85 м²");
  assert.equal(row(m, "kitchen_area"), "12 м²");
  assert.equal(row(m, "rooms"), "3 кімнати");
  assert.equal(row(m, "floor"), "5");
  assert.equal(m.price.text, "95 000 $");
  assert.equal(unitPrice(m), "1 118 $/м²");
  assert.deepEqual(quickFacts(m.rows).map((f) => f.text), ["85 м²", "3 кімн.", "5/9 пов."]);
});

t("службові параметри OLX не потрапляють, емодзі прибрано", () => {
  const m = normalizeOlx(API_FLAT);
  const labels = m.rows.map((r) => r.label);
  assert.ok(!labels.some((l) => /ріелтор|Приватна|Ціна/.test(l)), labels.join("|"));
  assert.equal(m.rows.find((r) => r.label === "Домашні улюбленці").value, "Так, котик");
  assert.equal(m.rows.find((r) => r.label === "Тип будинку").value, "Сталінка");
});

t("опис: без телефонів, посилань і @нікнеймів; роки й ціни лишаються", () => {
  const m = normalizeOlx(API_FLAT);
  assert.ok(!/067|t\.me|@owner/.test(m.description), m.description);
  assert.ok(/95 000 \$/.test(m.description) && /2001-2010/.test(m.description), m.description);
  assert.ok(!/Телефон/.test(m.description), m.description);
  assert.ok(/<br/.test(m.descriptionOriginal) === false);
});

t("фото: розмір з шаблону, без :443, без дублів", () => {
  const m = normalizeOlx(API_FLAT);
  assert.deepEqual(m.photos.map((p) => p.url), [
    "https://ireland.apollo.olxcdn.com/v1/files/aaa-UA/image;s=1600x1200",
    "https://ireland.apollo.olxcdn.com/v1/files/bbb-UA/image;s=900x1200",
  ]);
});

t("телефони в різних записах", () => {
  for (const s of ["0671234567", "067 123 45 67", "+380671234567", "+38 (067) 123-45-67", "(067)123-45-67", "38 067 123 45 67"]) {
    assert.equal(stripContacts("Дзвоніть " + s + " зранку").replace(/\s+/g, " "), "Дзвоніть зранку", s);
  }
  for (const s of ["1 050 000 грн", "кадастр 3220882200:03:005:1306", "площа 0,85 га", "2001-2010"]) {
    assert.equal(stripContacts(s), s, s);
  }
});

t("опис: залишки контактів і переноси <br>", () => {
  const m = normalizeOlx({ api: { description: "Рядок 1<br />\nРядок 2<br /><br />Телефонуйте: 067 123 45 67 (Viber)<br />Пишіть у Telegram @owner_kyiv<br />На 5 поверсі є ліфт.<br />Телевізор залишається." } });
  assert.equal(m.description, "Рядок 1\nРядок 2\n\nНа 5 поверсі є ліфт.\nТелевізор залишається.");
});

t("будинок зі стану сторінки (без API): тип з параметра, ділянка, ціна за м²", () => {
  const m = normalizeOlx({
    url: "u", main: {
      id: 5, title: "Будинок", description: "Опис", category: { id: 1602 },
      location: { cityName: "Лісники", districtName: "", regionName: "Київська область" },
      price: { displayValue: "250 000 $", regularPrice: { value: 250000, currencyCode: "USD" } },
      params: [
        { key: "property_type_houses", name: "Тип будинку", value: "Котедж", normalizedValue: "cottage" },
        { key: "total_area", name: "Загальна площа", value: "200 м²", normalizedValue: "200" },
        { key: "land_area", name: "Площа ділянки", value: "10", normalizedValue: "10" },
        { key: "total_floors", name: "Поверховість", value: "2", normalizedValue: "2" },
      ],
      photos: ["https://ireland.apollo.olxcdn.com:443/v1/files/zzz/image;s=1000x700"],
    },
  });
  assert.equal(m.kicker, "Продаж / Котедж");
  assert.equal(row(m, "address"), "Лісники, Київська область");
  assert.equal(row(m, "land_area"), "10 сот.");
  assert.ok(!m.rows.some((r) => r.label === "Тип будинку"), "тип будинку вже в «Тип об'єкта»");
  assert.equal(unitPrice(m), "1 250 $/м²");
  assert.deepEqual(quickFacts(m.rows).map((f) => f.text), ["200 м²", "2-поверх.", "10 сот."]);
  assert.equal(m.photos[0].url, "https://ireland.apollo.olxcdn.com/v1/files/zzz/image;s=1000x700");
});

t("оренда офісу: нежитлова, ціна / міс.", () => {
  const m = normalizeOlx({ api: { category: { id: 3690 }, location: { city: { name: "Київ" } },
    params: [{ key: "price", name: "Ціна", value: { value: 30000, currency: "UAH", label: "30 000 грн." } },
      { key: "total_area", name: "Загальна площа", value: { key: "60", label: "60 м²" } }] } });
  assert.equal(m.kicker, "Оренда / Офіс");
  assert.equal(row(m, "purpose"), "Нежитлова (комерційна)");
  assert.equal(m.price.text, "30 000 грн. / міс.");
  assert.equal(unitPrice(m), "500 грн/м²");
});

t("земля: призначення з типу землі, ціна за сотку", () => {
  const m = normalizeOlx({ api: { category: { id: 1608 }, params: [
    { key: "price", name: "Ціна", value: { value: 30000, currency: "USD", label: "30 000 $" } },
    { key: "property_type_land", name: "Тип нерухомості", value: { key: "2", label: "Земля житлової й суспільної забудови" } },
    { key: "land_area", name: "Площа ділянки", value: { key: "15", label: "15 сот." } }] } });
  assert.equal(m.kicker, "Продаж / Земельна ділянка");
  assert.equal(row(m, "purpose"), "Земля житлової й суспільної забудови");
  assert.equal(unitPrice(m), "2 000 $/сот.");
});

t("лише розмітка сторінки (DOM): параметри «Мітка: значення», хлібні крихти рос.", () => {
  const m = normalizeOlx({ dom: {
    title: "Сдам 1к", price: "12 000 грн.\nДоговірна", description: "Текст",
    params: ["Приватна особа", "Поверх: 3", "Поверховість: 16", "Загальна площа: 40 м²", "Кількість кімнат: 1 кімната", "Без комісії"],
    breadcrumbs: ["Главная", "Недвижимость", "Квартиры", "Долгосрочная аренда квартир"],
    location: ["Київ, Оболонський", "Київська область", "Опубліковано Сьогодні о 10:00"], photos: [] },
    ld: { product: { image: ["https://ireland.apollo.olxcdn.com/v1/files/q/image;s=1000x750"] }, breadcrumbs: [] } });
  assert.equal(m.kicker, "Оренда / Квартира");
  assert.equal(row(m, "address"), "Київ, Оболонський район");
  assert.equal(m.price.text, "12 000 грн. / міс.");
  assert.deepEqual(quickFacts(m.rows).map((f) => f.text), ["40 м²", "1 кімн.", "3/16 пов."]);
  assert.equal(m.photos.length, 1);
});

t("адреса: область не дублюється для обласних центрів", () => {
  assert.equal(formatAddress("Одеса", "Приморський", "Одеська область"), "Одеса, Приморський район");
  assert.equal(formatAddress("Кривий Ріг", "", "Дніпропетровська область"), "Кривий Ріг, Дніпропетровська область");
  assert.deepEqual(parsePrice("1 050 000 грн."), { value: 1050000, currency: "UAH" });
});

t("Telegraph: вузли, лише увімкнені фото, контакти ріелтора", () => {
  const m = normalizeOlx(API_FLAT);
  m.photos[1].on = false;
  const p = buildTelegraph(m, { name: "Олег", phone: "+380000000000" });
  assert.equal(p.title, "Продаж / Квартира, 85 м² — Київ, Печерський район");
  const imgs = JSON.stringify(p.content).match(/"img"/g) || [];
  assert.equal(imgs.length, 1);
  assert.ok(JSON.stringify(p.content).includes("1 118 $/м²"));
  assert.ok(JSON.stringify(p.content).includes("Олег · +380000000000"));
  assert.ok(JSON.stringify(p.content).length < 64000);
  m.showAgent = false;
  assert.ok(!JSON.stringify(buildTelegraph(m, { name: "Олег" }).content).includes("Олег"));
  assert.equal(presentationTitle({ kicker: "", rows: [] }), "Презентація об'єкта");
});

console.log(`\nУсього тестів: ${n}, усі пройшли.`);
