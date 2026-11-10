"""
Офіційний OLX Partner API (developer.olx.ua) — вхід «Дозволити» без паролів і cookie.

Схема (OAuth 2.0 authorization_code):
  1. Програма генерує state, дає користувачу посилання  https://www.olx.ua/oauth/authorize/?client_id=…&response_type=code
     &scope=v2 read write&state=…&redirect_uri=https://<host>.sslip.io/olx/callback
  2. Користувач входить на сайті OLX і тисне «Дозволити» → OLX веде на нашу приймальну адресу (служба realty-callback,
     common/oauth_callback.py), яка кладе код у $OAUTH_SPOOL/<sha256(state)>.json.
  3. Програма знаходить файл за своїм state, міняє код на токени: POST https://www.olx.ua/api/open/oauth/token (JSON).
     access ≈ 24 год, refresh ≈ 1 міс (новий видається раз на добу; без використання місяць — протухає).
  4. Запити до API: https://www.olx.ua/api/partner/… із заголовками Authorization: Bearer … і Version: 2.0
     (Content-Type — лише для POST/PUT: на GET він дає 400).

Спільне для всіх користувачів бота (лежить на сервері, НЕ в git):  $OAUTH_SPOOL/olx_app.json {client_id, client_secret},
$OAUTH_SPOOL/host.txt (адреса приймальної сторінки). Токени конкретного користувача: <DATA>/secrets/olx_api.json.

⚠️ Адреси authorize/token/API взято з документації OLX (PL) та чужої робочої інтеграції; для olx.ua їх ще не звіряли
з живою відповіддю — перша перевірка робиться командою `api-probe`.
"""
import fcntl
import hashlib
import json
import os
import time
import urllib.parse

from olx_client import NET_ERRORS, new_session  # noqa: F401  (NET_ERRORS реекспортуємо для run.py)

HOST = os.environ.get("OLX_API_HOST", "https://www.olx.ua")
AUTHORIZE_URL = HOST + "/oauth/authorize/"
TOKEN_URL = HOST + "/api/open/oauth/token"
API = HOST + "/api/partner"
SCOPE = "v2 read write"
SKEW = 300                 # с: оновлюємо токен, якщо лишилось менше


class OlxApiError(Exception):
    pass


def shared_dir():
    return os.environ.get("OAUTH_SPOOL", "/var/lib/realty-oauth")


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def save_app(client_id, client_secret, directory=None):
    """Спільні дані програми OLX (client_id / client_secret) — для всіх користувачів бота."""
    _write_json(os.path.join(directory or shared_dir(), "olx_app.json"),
                {"client_id": client_id.strip(), "client_secret": client_secret.strip()})


class OfficialClient:
    def __init__(self, data_dir, directory=None, session=None):
        self.dir = directory or shared_dir()
        self.token_path = os.path.join(data_dir, "secrets", "olx_api.json")
        self.s = session or new_session()
        self.tokens = _read_json(self.token_path) or {}

    # ---------------------------------------------------------------- налаштування
    def app(self):
        d = _read_json(os.path.join(self.dir, "olx_app.json")) or {}
        if not d.get("client_id") or not d.get("client_secret"):
            raise OlxApiError("програма OLX ще не налаштована на сервері (адмін: команда /olxapp)")
        return d["client_id"], d["client_secret"]

    def redirect_uri(self):
        try:
            with open(os.path.join(self.dir, "host.txt"), encoding="utf-8") as f:
                host = f.read().strip()
        except OSError:
            host = ""
        if not host:
            raise OlxApiError("приймальна адреса не налаштована на сервері (адмін: команда /callback)")
        return f"https://{host}/olx/callback"

    def authorize_url(self, state):
        cid, _ = self.app()
        q = {"client_id": cid, "response_type": "code", "scope": SCOPE, "state": state,
             "redirect_uri": self.redirect_uri()}
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)

    # ---------------------------------------------------------------- код від приймальної сторінки
    def wait_code(self, state, timeout=600, poll=2.0, sleep=time.sleep, now=time.time):
        path = os.path.join(self.dir, hashlib.sha256(state.encode("utf-8")).hexdigest() + ".json")
        end = now() + timeout
        while True:
            d = _read_json(path)
            if d:
                try:
                    os.remove(path)
                except OSError:
                    pass
                if d.get("error"):
                    raise OlxApiError(f"доступ не надано ({d['error']})")
                if d.get("code"):
                    return d["code"]
            if now() >= end:
                raise OlxApiError("не дочекався дозволу від OLX (10 хв). Почніть підключення ще раз")
            sleep(poll)

    # ---------------------------------------------------------------- токени
    def _save(self, d, old_refresh=None):
        t = {"access_token": d["access_token"],
             "refresh_token": d.get("refresh_token") or old_refresh,
             "expires_at": int(time.time()) + int(d.get("expires_in") or 3600),
             "scope": d.get("scope"), "saved_at": int(time.time())}
        _write_json(self.token_path, t)
        self.tokens = t

    def _token_call(self, grant):
        cid, secret = self.app()
        body = {"client_id": cid, "client_secret": secret, **grant}
        os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
        with open(self.token_path + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if grant.get("grant_type") == "refresh_token":
                    cur = _read_json(self.token_path) or {}      # інший процес міг уже оновити — беремо найновіший
                    if not cur.get("refresh_token"):
                        raise OlxApiError("офіційне підключення ще не зроблено")
                    body["refresh_token"] = cur["refresh_token"]
                r = self.s.post(TOKEN_URL, json=body, timeout=40, headers={"Accept": "application/json"})
                try:
                    d = r.json()
                except ValueError:
                    d = {}
                if r.status_code != 200 or not isinstance(d, dict) or not d.get("access_token"):
                    err = (d.get("error_description") or d.get("error") or d.get("message")) if isinstance(d, dict) else None
                    raise OlxApiError(f"OLX не видав токен (HTTP {r.status_code}"
                                      f"{', ' + str(err)[:200] if err else ''})")
                self._save(d, old_refresh=body.get("refresh_token"))
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def exchange_code(self, code):
        self._token_call({"grant_type": "authorization_code", "code": code, "scope": SCOPE,
                          "redirect_uri": self.redirect_uri()})

    def refresh(self):
        if not (self.tokens or _read_json(self.token_path) or {}).get("refresh_token"):
            raise OlxApiError("офіційне підключення ще не зроблено")
        self._token_call({"grant_type": "refresh_token"})

    def connected(self):
        return bool((_read_json(self.token_path) or {}).get("refresh_token"))

    def expires_in(self):
        return int((self.tokens or {}).get("expires_at", 0) - time.time())

    def ensure_token(self):
        self.tokens = _read_json(self.token_path) or {}
        if not self.tokens.get("access_token"):
            raise OlxApiError("офіційне підключення ще не зроблено")
        if self.expires_in() < SKEW:
            self.refresh()

    # ---------------------------------------------------------------- запити
    def request(self, method, path, params=None, body=None):
        """→ (status, розібране тіло або текст). Один повтор після оновлення токена при 401."""
        self.ensure_token()
        for attempt in (1, 2):
            h = {"Authorization": "Bearer " + self.tokens["access_token"], "Version": "2.0", "Accept": "application/json"}
            kw = {}
            if method in ("POST", "PUT"):
                h["Content-Type"] = "application/json"
                kw["json"] = body
            r = self.s.request(method, API + path, params=params, headers=h, timeout=40, **kw)
            if r.status_code == 401 and attempt == 1:
                self.refresh()
                continue
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, (r.text or "")[:500]

    def get(self, path, params=None):
        return self.request("GET", path, params=params)
