#!/usr/bin/env python3
"""Приймальна сторінка для OAuth-повернення (OLX: «Дозволити» → ця адреса).

Слухає ЛИШЕ 127.0.0.1 (назовні виставляє Caddy з HTTPS, deploy/setup-callback.sh).
GET /olx/callback?code=…&state=…  → пише файл <sha256(state)>.json у каталог обміну (OAUTH_SPOOL).
Бот-слот, який видавав state, сам забирає свій файл (за хешем) і міняє code на токени —
тут жодних секретів програми й жодних токенів нема. code не потрапляє в логи.
"""
import hashlib
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

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/olx/health":
            return self._send(200, "ok", "text/plain; charset=utf-8")
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
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print(f"oauth_callback: 127.0.0.1:{PORT}, обмін через {SPOOL}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
