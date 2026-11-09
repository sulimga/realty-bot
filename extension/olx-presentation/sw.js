// Клік по іконці розширення на сторінці оголошення → зчитати дані → відкрити презентацію в новій вкладці.

import { SOURCES } from "./sources/index.js";

const KEEP = 30; // скільки останніх презентацій тримати в пам'яті розширення

async function prune() {
  const all = await chrome.storage.local.get(null);
  const keys = Object.keys(all).filter((k) => k.startsWith("pres_")).sort();
  if (keys.length > KEEP) await chrome.storage.local.remove(keys.slice(0, keys.length - KEEP));
}

async function makePresentation(tab) {
  const key = "pres_" + Date.now();
  const src = SOURCES.find((s) => s.match(tab.url));
  let entry;
  if (!src) {
    entry = { error: "not_ad", url: tab.url || "" };
  } else {
    chrome.action.setBadgeText({ tabId: tab.id, text: "…" }).catch(() => {});
    try {
      let main = null;
      try {
        const [r] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "MAIN", func: src.readMain });
        main = r && r.result;
      } catch (e) {
        main = { err: "MAIN: " + ((e && e.message) || e) };
      }
      const [r2] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: src.extract, args: [main] });
      entry = { source: src.id, raw: r2 && r2.result };
      if (!entry.raw) entry = { error: "empty", url: tab.url };
    } catch (e) {
      entry = { error: "inject", message: String((e && e.message) || e), url: tab.url };
    }
    chrome.action.setBadgeText({ tabId: tab.id, text: "" }).catch(() => {});
  }
  entry.createdAt = Date.now();
  await chrome.storage.local.set({ [key]: entry });
  await prune();
  await chrome.tabs.create({ url: chrome.runtime.getURL("presentation.html#" + key), index: tab.index + 1, openerTabId: tab.id });
}

chrome.action.onClicked.addListener(makePresentation);
// Для перевірки без кліку (консоль service worker або автотест): makePresentation(tab)
globalThis.makePresentation = makePresentation;
