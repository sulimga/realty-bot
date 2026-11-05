const st = document.getElementById("st");
const say = (t, cls) => { st.textContent = t; st.className = cls || ""; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function currentToken() {
  const { olxToken } = await chrome.storage.session.get("olxToken");
  return olxToken || null;
}

async function copyLogin() {
  const cookies = await chrome.cookies.getAll({ domain: "olx.ua" });
  const token = await currentToken();
  if (!cookies.length) return say("На olx.ua немає cookie. Увійдіть в акаунт OLX у цьому браузері.", "bad");
  const text = buildPayload(token, cookies);
  await navigator.clipboard.writeText(text);
  const left = token ? minutesLeft(token, Date.now() / 1000) : null;
  let msg = "Скопійовано (" + text.length + " символів).\nВставте боту: «🍪 Cookies» → OLX → «Тимчасовий токен / cookie».";
  if (!token) msg += "\n⚠️ Токен не зловлено — натисніть кнопку «Оновити сторінку…» і скопіюйте ще раз. Без токена спрацюють лише cookie.";
  else if (left !== null && left < 1) msg += "\n⚠️ Токен вже протух — натисніть «Оновити сторінку…».";
  else if (left !== null) msg += "\nТокен діє ще ~" + left + " хв.";
  if (text.length > 4000) msg += "\n⚠️ Більше 4000 символів — Telegram може розрізати повідомлення. Напишіть мені, зменшимо.";
  say(msg, token && (left === null || left >= 1) ? "ok" : "bad");
}

async function reloadAndCatch() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https:\/\/([\w-]+\.)?olx\.ua\//.test(tab.url || ""))
    return say("Відкрийте вкладку olx.ua (залогінені) і натисніть знову.", "bad");
  await chrome.storage.session.remove("olxToken");
  say("Оновлюю сторінку…");
  await chrome.tabs.reload(tab.id);
  for (let i = 0; i < 20; i++) {
    await sleep(500);
    if (await currentToken()) return copyLogin();
  }
  say("Токен не зловлено за 10 с. Відкрийте будь-яке оголошення або свій профіль на olx.ua і спробуйте ще.", "bad");
}

document.getElementById("copy").onclick = () => copyLogin().catch((e) => say("Помилка: " + e.message, "bad"));
document.getElementById("reload").onclick = () => reloadAndCatch().catch((e) => say("Помилка: " + e.message, "bad"));
