// Сторінка презентації: показ, редагування, PDF (друк браузера) і Telegraph.

import { normalizeOlx, quickFacts, unitPrice, addressOf, presentationTitle, stripContacts, tidyText } from "./lib/normalize.js";
import { publish } from "./lib/telegraph.js";

const $ = (id) => document.getElementById(id);
const KEY = decodeURIComponent(location.hash.slice(1));
let entry = null;
let model = null;
let agent = { name: "", phone: "", extra: "" };

const ICONS = {
  area: '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 16l8-8M12 8h4v4M8 12v4h4"/></svg>',
  rooms: '<svg viewBox="0 0 24 24"><path d="M5 21V4a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v17M3 21h18"/><circle cx="15" cy="12" r="1"/></svg>',
  floor: '<svg viewBox="0 0 24 24"><path d="M3 20h5v-5h5v-5h5V5h3"/></svg>',
  land: '<svg viewBox="0 0 24 24"><path d="M3 20l5-12 4 7 3-4 6 9z"/></svg>',
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function status(html, cls) {
  const s = $("status");
  s.hidden = !html;
  s.className = "status " + (cls || "");
  s.innerHTML = html || "";
}

let saveTimer = null;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    entry.model = model;
    chrome.storage.local.set({ [KEY]: entry }).catch((e) => status("Не зберіг правки: " + esc(e.message), "bad"));
  }, 300);
}

// ---------- показ ----------

function renderDerived() {
  $("addrLine").textContent = addressOf(model);
  const facts = quickFacts(model.rows);
  $("quick").innerHTML = facts.map((f) => `<span class="chip">${ICONS[f.icon] || ""}${esc(f.text)}</span>`).join("");
  const showPrice = model.price.show && model.price.text.trim();
  $("priceBox").style.display = model.price.show ? "" : "none";
  $("unitPrice").textContent = showPrice ? unitPrice(model) : "";
  document.title = presentationTitle(model);
}

function renderRows() {
  $("params").innerHTML = model.rows.map((r, i) => `
    <tr data-i="${i}" class="${String(r.value).trim() && String(r.label).trim() ? "" : "empty"}">
      <th contenteditable="plaintext-only" data-f="label" data-ph="Назва">${esc(r.label)}</th>
      <td contenteditable="plaintext-only" data-f="value" data-ph="Значення">${esc(r.value)}</td>
      <td class="x no-print"><button title="Прибрати рядок">✕</button></td>
    </tr>`).join("");
}

function renderPhotos() {
  const ph = model.photos;
  const heroIdx = ph.findIndex((p) => p.on);
  $("hero").style.display = heroIdx >= 0 ? "" : "none";
  if (heroIdx >= 0 && $("heroImg").getAttribute("src") !== ph[heroIdx].url) $("heroImg").src = ph[heroIdx].url;
  $("gallery").innerHTML = ph.map((p, i) => `
    <div class="photo ${p.on ? "" : "off"} ${i === heroIdx ? "hero-ph" : ""}" data-i="${i}">
      <img src="${esc(p.url)}" alt="" referrerpolicy="no-referrer">
      <span class="ph-num">${i + 1}</span>
      <div class="ph-tools">
        ${i === heroIdx ? "" : `<button data-act="hero" title="Зробити головним">★</button>`}
        <button data-act="toggle" title="${p.on ? "Прибрати" : "Повернути"}">${p.on ? "✕" : "↺"}</button>
      </div>
    </div>`).join("");
  const onCount = ph.filter((p) => p.on).length;
  $("photoSec").classList.toggle("no-photos", onCount < 2);
  $("photoSec").querySelector(".block-title").innerHTML = `Фото <span class="cnt no-print">${onCount} з ${ph.length}</span>`;
}

function renderAgent() {
  const line = [agent.name, agent.phone, agent.extra].map((s) => String(s || "").trim()).filter(Boolean);
  $("agentBox").innerHTML = model.showAgent && line.length
    ? `<span class="lbl">Ваш ріелтор:</span> <span class="who">${esc(line.join(" · "))}</span>` : "";
}

function renderSrc() {
  const parts = [model.source];
  if (model.sourceId) parts.push("ID " + model.sourceId);
  $("src").innerHTML = esc(parts.join(" · ")) + (model.sourceUrl ? ` · <a href="${esc(model.sourceUrl)}" target="_blank">оголошення</a>` : "");
  $("tgBtn").textContent = model.telegraph ? "✈ Оновити Telegraph" : "✈ Telegraph";
}

function renderAll() {
  $("sheet").hidden = false;
  $("kicker").textContent = model.kicker;
  $("kicker").dataset.ph = "Продаж / Квартира";
  $("price").textContent = model.price.text;
  $("price").dataset.ph = "Ціна";
  $("desc").textContent = model.description;
  $("desc").dataset.ph = "Опис об'єкта";
  $("descSec").classList.toggle("no-desc", !model.description.trim());
  $("showPrice").checked = model.price.show;
  $("cleanContacts").checked = model.cleanContacts;
  $("showAgent").checked = model.showAgent;
  renderRows();
  renderPhotos();
  renderAgent();
  renderSrc();
  renderDerived();
  renderWarnings();
}

function renderWarnings() {
  const w = [];
  if (!model.photos.length) w.push("Фото не знайдено.");
  if (!model.description) w.push("Опис не знайдено.");
  if (model.rows.length < 3) w.push("Мало параметрів.");
  if (w.length) {
    w.push("Схоже, OLX змінив сторінку. Натисніть 🐞 і надішліть файл Claude — підлаштуємо зчитування.");
    if (model.notes && model.notes.length) w.push("Деталі: " + model.notes.join("; "));
  }
  $("warn").hidden = !w.length;
  $("warn").textContent = w.join("\n");
}

function showError(text) {
  $("warn").hidden = false;
  $("warn").className = "warn bad no-print";
  $("warn").textContent = text;
  for (const id of ["pdfBtn", "tgBtn", "resetBtn"]) $(id).disabled = true;
}

// ---------- правки ----------

function bindEditing() {
  $("kicker").addEventListener("input", () => { model.kicker = $("kicker").innerText.trim(); renderDerived(); save(); });
  $("price").addEventListener("input", () => { model.price.text = $("price").innerText.trim(); renderDerived(); save(); });
  $("desc").addEventListener("input", () => {
    model.description = $("desc").innerText.replace(/\n$/, "");
    model.descEdited = true;
    $("descSec").classList.toggle("no-desc", !model.description.trim());
    save();
  });

  $("params").addEventListener("input", (e) => {
    const cell = e.target.closest("[data-f]");
    const tr = e.target.closest("tr");
    if (!cell || !tr) return;
    const r = model.rows[+tr.dataset.i];
    r[cell.dataset.f] = cell.innerText.trim();
    tr.classList.toggle("empty", !(r.label.trim() && r.value.trim()));
    renderDerived();
    save();
  });
  // Enter у клітинці — не новий рядок, а вихід з редагування
  $("params").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.closest("[data-f]")) { e.preventDefault(); e.target.blur(); }
  });
  for (const id of ["kicker", "price"]) {
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); e.target.blur(); } });
  }
  $("params").addEventListener("click", (e) => {
    if (!e.target.closest("td.x button")) return;
    model.rows.splice(+e.target.closest("tr").dataset.i, 1);
    renderRows();
    renderDerived();
    save();
  });
  $("addRow").addEventListener("click", () => {
    model.rows.push({ key: "", label: "", value: "" });
    renderRows();
    $("params").lastElementChild.querySelector("th").focus();
    save();
  });

  $("gallery").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const i = +b.closest(".photo").dataset.i;
    if (b.dataset.act === "toggle") model.photos[i].on = !model.photos[i].on;
    if (b.dataset.act === "hero") {
      const [p] = model.photos.splice(i, 1);
      p.on = true;
      model.photos.unshift(p);
    }
    renderPhotos();
    save();
  });

  $("showPrice").addEventListener("change", (e) => { model.price.show = e.target.checked; renderDerived(); save(); });
  $("showAgent").addEventListener("change", (e) => {
    model.showAgent = e.target.checked;
    if (model.showAgent && !agent.name && !agent.phone) $("agentPanel").hidden = false;
    renderAgent();
    save();
  });
  $("cleanContacts").addEventListener("change", (e) => {
    if (model.descEdited && !confirm("Опис буде взято з OLX заново — ваші правки в описі пропадуть. Продовжити?")) {
      e.target.checked = !e.target.checked;
      return;
    }
    model.cleanContacts = e.target.checked;
    const d = model.descriptionOriginal || "";
    model.description = model.cleanContacts ? tidyText(stripContacts(d)) : d;
    model.descEdited = false;
    $("desc").textContent = model.description;
    $("descSec").classList.toggle("no-desc", !model.description.trim());
    save();
  });
}

function bindActions() {
  $("agentBtn").addEventListener("click", () => {
    $("agentPanel").hidden = !$("agentPanel").hidden;
  });
  $("agSave").addEventListener("click", async () => {
    agent = { name: $("agName").value.trim(), phone: $("agPhone").value.trim(), extra: $("agExtra").value.trim() };
    await chrome.storage.local.set({ agent });
    $("agentPanel").hidden = true;
    if (model) renderAgent();
  });

  $("resetBtn").addEventListener("click", () => {
    if (!confirm("Скинути всі правки і взяти дані з OLX заново?")) return;
    const tg = model.telegraph;
    model = normalizeOlx(entry.raw);
    model.telegraph = tg;
    renderAll();
    save();
  });

  $("debugBtn").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify({ key: KEY, entry: { ...entry, model } }, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `olx-${(model && model.sourceId) || "page"}-dani.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  });

  $("pdfBtn").addEventListener("click", async () => {
    document.activeElement && document.activeElement.blur();
    $("pdfBtn").disabled = true;
    status("Чекаю, поки завантажаться фото…");
    await waitImages(20000);
    status("");
    $("pdfBtn").disabled = false;
    // назва файлу PDF = заголовок сторінки
    document.title = presentationTitle(model).replace(/[\\/:*?"<>|]+/g, " ").slice(0, 120);
    window.print();
  });

  $("tgBtn").addEventListener("click", async () => {
    document.activeElement && document.activeElement.blur();
    if (!model.photos.some((p) => p.on) && !confirm("У презентації немає фото. Все одно публікувати?")) return;
    $("tgBtn").disabled = true;
    status(model.telegraph ? "Оновлюю сторінку в Telegraph…" : "Публікую в Telegraph…");
    try {
      const store = {
        get: async () => (await chrome.storage.local.get("tgToken")).tgToken || null,
        set: (t) => chrome.storage.local.set({ tgToken: t }),
      };
      const res = await publish(model, agent, store);
      model.telegraph = res;
      save();
      renderSrc();
      let copied = true;
      try { await navigator.clipboard.writeText(res.url); } catch (e) { copied = false; }
      status(`Готово: <a href="${esc(res.url)}" target="_blank">${esc(res.url)}</a>${copied ? " — посилання скопійовано, можна вставляти клієнту." : ""}
        <button id="copyTg">Копіювати</button>`, "ok");
      $("copyTg").onclick = () => navigator.clipboard.writeText(res.url).then(() => ($("copyTg").textContent = "Скопійовано ✓"));
    } catch (e) {
      status("Не вдалося: " + esc(e.message || e), "bad");
    } finally {
      $("tgBtn").disabled = false;
    }
  });
}

function waitImages(timeoutMs) {
  const imgs = [...document.querySelectorAll(".sheet img")].filter((i) => i.offsetParent !== null || i.id === "heroImg");
  const all = Promise.all(imgs.map((i) => (i.complete ? null : new Promise((r) => { i.onload = i.onerror = r; }))));
  return Promise.race([all, new Promise((r) => setTimeout(r, timeoutMs))]);
}

// ---------- старт ----------

async function init() {
  bindActions();
  const st = await chrome.storage.local.get([KEY, "agent"]);
  if (st.agent) agent = { ...agent, ...st.agent };
  $("agName").value = agent.name;
  $("agPhone").value = agent.phone;
  $("agExtra").value = agent.extra;
  entry = st[KEY];
  if (!entry) return showError("Дані цієї презентації не знайдено (розширення тримає лише 30 останніх). Відкрийте оголошення на OLX і натисніть на іконку розширення ще раз.");
  if (entry.error === "not_ad") return showError("Це не сторінка оголошення OLX.\nВідкрийте оголошення (адреса виду olx.ua/d/uk/obyavlenie/…) і натисніть на іконку розширення ще раз.");
  if (entry.error) return showError("Не вдалося прочитати сторінку оголошення" + (entry.message ? `: ${entry.message}` : "") + ".\nОновіть сторінку OLX (F5) і спробуйте ще раз.");
  model = entry.model || normalizeOlx(entry.raw);
  if (!entry.model) save();
  bindEditing();
  renderAll();
}

init().catch((e) => showError("Помилка: " + (e && e.message ? e.message : e)));
