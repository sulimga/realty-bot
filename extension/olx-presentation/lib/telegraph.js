// Публікація презентації на telegra.ph (API: https://telegra.ph/api).
// Фото на сторінці — прямі посилання на CDN OLX (завантаження файлів у Telegraph вимкнене).

import { quickFacts, unitPrice, presentationTitle } from "./normalize.js";

const API = "https://api.telegra.ph";

const el = (tag, children, attrs) => {
  const n = { tag };
  if (attrs) n.attrs = attrs;
  if (children != null) n.children = Array.isArray(children) ? children : [children];
  return n;
};

function paragraphs(text) {
  const out = [];
  for (const block of String(text || "").split(/\n{2,}/)) {
    const lines = block.split("\n").map((l) => l.trim()).filter(Boolean);
    if (!lines.length) continue;
    const ch = [];
    lines.forEach((l, i) => {
      if (i) ch.push(el("br"));
      ch.push(l);
    });
    out.push(el("p", ch));
  }
  return out;
}

export function agentLine(agent) {
  if (!agent) return "";
  return [agent.name, agent.phone, agent.extra].map((s) => String(s || "").trim()).filter(Boolean).join(" · ");
}

// Модель → { title, author_name, content } для createPage / editPage.
export function buildTelegraph(model, agent) {
  const photos = (model.photos || []).filter((p) => p.on).map((p) => p.url);
  const rows = (model.rows || []).filter((r) => String(r.label || "").trim() && String(r.value || "").trim());
  const c = [];
  if (photos[0]) c.push(el("figure", [el("img", null, { src: photos[0] })]));
  if (model.price && model.price.show && model.price.text) {
    const up = unitPrice(model);
    c.push(el("h3", up ? `${model.price.text} (${up})` : model.price.text));
  }
  const facts = quickFacts(model.rows).map((f) => f.text).join(" · ");
  if (facts) c.push(el("p", [el("b", facts)]));
  if (rows.length) {
    c.push(el("h4", "Параметри"));
    c.push(el("ul", rows.map((r) => el("li", [el("b", `${r.label.trim()}: `), r.value.trim()]))));
  }
  if (String(model.description || "").trim()) {
    c.push(el("h4", "Опис"));
    c.push(...paragraphs(model.description));
  }
  if (photos.length > 1) {
    c.push(el("h4", "Фото"));
    for (const src of photos.slice(1)) c.push(el("figure", [el("img", null, { src })]));
  }
  const ag = model.showAgent ? agentLine(agent) : "";
  if (ag) {
    c.push(el("hr"));
    c.push(el("p", [el("i", "Ваш ріелтор: "), el("b", ag)]));
  }
  return {
    title: presentationTitle(model).slice(0, 256),
    author_name: String((model.showAgent && agent && agent.name) || "").slice(0, 128),
    content: c,
  };
}

async function call(method, params) {
  const body = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v != null && v !== "") body.set(k, typeof v === "string" ? v : JSON.stringify(v));
  const r = await fetch(`${API}/${method}`, { method: "POST", body });
  let j = null;
  try { j = await r.json(); } catch (e) { /* нижче */ }
  if (!j) throw new Error(`Telegraph: HTTP ${r.status}`);
  if (!j.ok) throw new Error(`Telegraph: ${j.error || "помилка"}`);
  return j.result;
}

// Створює (або оновлює, якщо сторінка вже є) і повертає { url, path }.
// store — { get(): Promise<token|null>, set(token) } — токен акаунта Telegraph у chrome.storage.
export async function publish(model, agent, store) {
  let token = await store.get();
  if (!token) {
    const acc = await call("createAccount", {
      short_name: "Презентації",
      author_name: String((agent && agent.name) || "").slice(0, 128),
    });
    token = acc.access_token;
    await store.set(token);
  }
  const page = buildTelegraph(model, agent);
  const json = JSON.stringify(page.content);
  if (json.length > 63000) throw new Error("Telegraph: завеликий текст (ліміт 64 КБ) — скоротіть опис.");
  const params = { access_token: token, title: page.title, author_name: page.author_name, content: json, return_content: "false" };
  let res = null;
  if (model.telegraph && model.telegraph.path) {
    try {
      res = await call(`editPage/${encodeURIComponent(model.telegraph.path)}`, params);
    } catch (e) {
      // сторінку створено іншим токеном (напр., після перевстановлення розширення) — робимо нову
      if (!/ACCESS_DENIED|PAGE_NOT_FOUND/i.test(e.message)) throw e;
    }
  }
  if (!res) res = await call("createPage", params);
  return { url: res.url, path: res.path };
}
