// Ловить рядок Authorization у запитах сторінки до www.olx.ua/api — це і є токен, який потрібен боту.
// Зберігаємо останній у chrome.storage.session (пам'ять браузера; на диск не пишеться).
chrome.webRequest.onSendHeaders.addListener(
  (d) => {
    const h = (d.requestHeaders || []).find((x) => x.name.toLowerCase() === "authorization");
    const m = h && /^Bearer\s+(eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+)$/.exec(h.value || "");
    if (m) chrome.storage.session.set({ olxToken: m[1], olxTokenAt: Date.now() });
  },
  { urls: ["https://www.olx.ua/api/*"] },
  ["requestHeaders", "extraHeaders"]
);
