#!/usr/bin/env python3
"""Приймальна сторінка для OAuth-повернення (OLX: «Дозволити» → ця адреса) і редактор тексту для бота.

Слухає ЛИШЕ 127.0.0.1 (назовні виставляє Caddy з HTTPS, deploy/setup-callback.sh).
GET /olx/callback?code=…&state=…  → пише файл <sha256(state)>.json у каталог обміну (OAUTH_SPOOL).
Бот-слот, який видавав state, сам забирає свій файл (за хешем) і міняє code на токени —
тут жодних секретів програми й жодних токенів нема. code не потрапляє в логи.

Редактор (Telegram Mini App, 10.11.2026): бот кладе <OAUTH_SPOOL>/edit/<token>.in.json {"text","title"} і дає
кнопку на https://<host>/edit/<token>. GET — сторінка з полем, де вже стоїть текст; POST {"text"} — пише
<token>.out.json, бот його забирає (і прибирає обидва файли). token — випадковий, знає лише чат, де кнопка.
GET /edit/health — бот перевіряє, що редактор є (стара версія служби відповідає 404).
"""
import hashlib
import html
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

SPOOL = os.environ.get("OAUTH_SPOOL", "/var/lib/realty-oauth")
PORT = int(os.environ.get("OAUTH_PORT", "8099"))
TTL = 3600                                    # файли старші за годину видаляємо
STATE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
CODE_RE = re.compile(r"^[A-Za-z0-9_\-./~+=]{4,2048}$")
ERR_RE = re.compile(r"^[A-Za-z0-9_ .:-]{1,200}$")
EDIT_DIR = os.path.join(SPOOL, "edit")
EDIT_PATH_RE = re.compile(r"^/edit/([A-Za-z0-9_-]{24,64})$")
EDIT_TTL = 3 * 3600                           # незабрані файли редактора видаляємо через 3 год
EDIT_MAX_CHARS = 20000                        # з запасом (OLX приймає до 9000)
EDIT_MAX_BODY = 256 * 1024

EDIT_PAGE = """<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>%%TITLE%%</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--tg-theme-bg-color,#fff);
  color:var(--tg-theme-text-color,#1d2433)}
.wrap{display:flex;flex-direction:column;gap:8px;padding:10px;height:var(--tg-viewport-stable-height,100vh)}
h1{font-size:14px;font-weight:600;margin:2px 2px 0;color:var(--tg-theme-hint-color,#6b7385)}
textarea{flex:1;width:100%;padding:10px 12px;font:inherit;border-radius:10px;resize:none;outline:none;
  border:1px solid var(--tg-theme-section-separator-color,#d5dae3);color:inherit;
  background:var(--tg-theme-secondary-bg-color,#f6f7f9)}
.row{display:flex;justify-content:space-between;gap:8px;font-size:13px;color:var(--tg-theme-hint-color,#6b7385);padding:0 2px}
button{font:600 16px system-ui,sans-serif;padding:12px;border:0;border-radius:10px;cursor:pointer;
  background:var(--tg-theme-button-color,#1f5fa8);color:var(--tg-theme-button-text-color,#fff)}
button:disabled{opacity:.6}
</style></head><body><div class="wrap">
<h1>%%TITLE%%</h1>
<textarea id="t" spellcheck="true">%%TEXT%%</textarea>
<div class="row"><span id="n"></span><span id="s"></span></div>
<button id="b">Зберегти</button>
</div><script>
const tg = window.Telegram && Telegram.WebApp && Telegram.WebApp.initData ? Telegram.WebApp : null;
const t = document.getElementById("t"), n = document.getElementById("n"), s = document.getElementById("s"),
      b = document.getElementById("b");
const count = () => { n.textContent = t.value.length + " символів"; };
t.addEventListener("input", count); count();
let busy = false;
async function save() {
  if (busy) return;
  busy = true; b.disabled = true; s.textContent = "Зберігаю…";
  if (tg) tg.MainButton.showProgress();
  try {
    const r = await fetch(location.pathname, { method: "POST", headers: { "Content-Type": "application/json" },
                                              body: JSON.stringify({ text: t.value }) });
    let j = {}; try { j = await r.json(); } catch (e) {}
    if (!r.ok || !j.ok) throw new Error(j.error || ("HTTP " + r.status));
    s.textContent = "✅ Збережено — повертайтесь у чат";
    if (tg) setTimeout(() => tg.close(), 500);
  } catch (e) {
    s.textContent = "❌ " + e.message; busy = false; b.disabled = false;
  } finally { if (tg) tg.MainButton.hideProgress(); }
}
b.addEventListener("click", save);
if (tg) {
  tg.ready(); tg.expand();
  tg.MainButton.setText("Зберегти"); tg.MainButton.onClick(save); tg.MainButton.show();
  b.style.display = "none";                       // у Telegram — нативна кнопка внизу (видно й над клавіатурою)
}
</script></body></html>"""

PAGE = """<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font:16px system-ui,sans-serif;background:#10141f;color:#e8ecf4;display:flex;
justify-content:center;padding:48px 16px}}.c{{max-width:420px;background:#1a2133;border-radius:16px;
padding:28px;text-align:center}}h1{{font-size:22px;margin:0 0 12px}}p{{color:#b8c0d4;line-height:1.5}}</style>
</head><body><div class="c"><h1>{title}</h1><p>{text}</p></div></body></html>"""


def key_for(state):
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def cleanup(now=None):
    now = now or time.time()
    try:
        for n in os.listdir(SPOOL):
            p = os.path.join(SPOOL, n)
            if n.endswith(".json") and now - os.path.getmtime(p) > TTL:
                os.remove(p)
    except OSError:
        pass


def edit_cleanup(now=None):
    now = now or time.time()
    try:
        for n in os.listdir(EDIT_DIR):
            p = os.path.join(EDIT_DIR, n)
            if now - os.path.getmtime(p) > EDIT_TTL:
                os.remove(p)
    except OSError:
        pass


def edit_load(token):
    try:
        with open(os.path.join(EDIT_DIR, token + ".in.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def edit_save(token, text):
    p = os.path.join(EDIT_DIR, token + ".out.json")
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"text": text, "ts": int(time.time())}, f, ensure_ascii=False)
    os.replace(tmp, p)


def store(state, code=None, error=None):
    os.makedirs(SPOOL, exist_ok=True)
    p = os.path.join(SPOOL, key_for(state) + ".json")
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"code": code, "error": error, "ts": int(time.time())}, f)
    os.replace(tmp, p)


class Handler(BaseHTTPRequestHandler):
    server_version = "cb"
    sys_version = ""

    def log_message(self, fmt, *args):         # без query: там code
        sys.stderr.write("%s %s\n" % (self.command, urlsplit(self.path).path))

    def _send(self, status, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _page(self, status, title, text):
        self._send(status, PAGE.format(title=title, text=text))

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_POST(self):
        m = EDIT_PATH_RE.match(urlsplit(self.path).path)
        if not m:
            return self._send(404, "not found", "text/plain; charset=utf-8")
        token = m.group(1)
        if edit_load(token) is None:
            return self._json(410, {"ok": False, "error": "посилання застаріло — поверніться в чат"})
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        if not 0 < size <= EDIT_MAX_BODY:
            return self._json(413, {"ok": False, "error": "завеликий текст"})
        try:
            text = json.loads(self.rfile.read(size).decode("utf-8")).get("text")
        except (ValueError, AttributeError, UnicodeDecodeError):
            text = None
        if not isinstance(text, str):
            return self._json(400, {"ok": False, "error": "немає тексту"})
        if len(text) > EDIT_MAX_CHARS:
            return self._json(413, {"ok": False, "error": f"задовгий текст (макс. {EDIT_MAX_CHARS} символів)"})
        edit_save(token, text)
        self._json(200, {"ok": True})

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path in ("/olx/health", "/edit/health"):
            return self._send(200, "ok", "text/plain; charset=utf-8")
        m = EDIT_PATH_RE.match(u.path)
        if m:
            edit_cleanup()
            d = edit_load(m.group(1))
            if d is None:
                return self._page(410, "Посилання застаріло",
                                  "Цей текст уже збережено або час вийшов. Поверніться в Telegram — бот попросить ще раз.")
            page = (EDIT_PAGE.replace("%%TITLE%%", html.escape(str(d.get("title") or "Редагування тексту")))
                    .replace("%%TEXT%%", html.escape(str(d.get("text") or ""))))
            return self._send(200, page)
        if u.path != "/olx/callback":
            return self._send(404, "not found", "text/plain; charset=utf-8")
        q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        state, code, err = q.get("state", ""), q.get("code", ""), q.get("error", "")
        if not STATE_RE.match(state):
            return self._page(400, "Не вдалося підключити", "Посилання недійсне. Поверніться в Telegram і почніть підключення ще раз.")
        cleanup()
        if err:
            store(state, error=err if ERR_RE.match(err) else "error")
            return self._page(200, "Доступ не надано",
                              "Ви не дозволили доступ. Якщо це помилка, поверніться в Telegram і почніть підключення ще раз.")
        if not CODE_RE.match(code):
            return self._page(400, "Не вдалося підключити", "OLX не передав код. Поверніться в Telegram і спробуйте ще раз.")
        store(state, code=code)
        self._page(200, "✅ OLX: дозвіл отримано",
                   "Поверніться в Telegram — бот завершить підключення за кілька секунд. Цю сторінку можна закрити.")


def main():
    os.makedirs(SPOOL, exist_ok=True)
    os.makedirs(EDIT_DIR, mode=0o700, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print(f"oauth_callback: 127.0.0.1:{PORT}, обмін через {SPOOL}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
