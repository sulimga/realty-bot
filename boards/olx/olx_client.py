"""
Клієнт OLX (posting-services + www.olx.ua/api). Знято з HAR форми подачі оголошення.

Авторизація: веб-версія OLX шле `Authorization: Bearer <access_token>`, де access_token —
JWT із cookie `access_token` (той самий, що в браузері). Він живе недовго, тому
при 401 пробуємо оновити його запитом сторінки кабінету з cookie (сайт сам видає новий
access_token у Set-Cookie); якщо не вийшло — просимо оновити cookie.

Фото: POST /api/v1/apollo/token/ -> {"token"} ;
      POST https://ireland.apollo.olxcdn.com/v1/temp-files  (тіло — JPEG,
           Authorization: Bearer <apollo token>, Expires: завтра) -> {"data":{"filename"}}
Оголошення: POST https://posting-services.prd.01.eu-west-1.eu.olx.org/api/v2/offers
"""

import base64
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

WWW = "https://www.olx.ua"
POSTING = "https://posting-services.prd.01.eu-west-1.eu.olx.org"
APOLLO = "https://ireland.apollo.olxcdn.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


class OlxError(Exception):
    pass


class AuthError(OlxError):
    pass


def parse_cookie_string(raw):
    raw = (raw or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def jwt_exp(token):
    """Час закінчення JWT (unix) або None."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
    except Exception:  # noqa: BLE001
        return None


class OlxClient:
    def __init__(self, cookie_path, device_id, log_dir=None):
        self.cookie_path = cookie_path
        self.device_id = device_id
        self.log_dir = log_dir
        self.cookies = parse_cookie_string(open(cookie_path, encoding="utf-8").read()) if cookie_path else {}
        self.s = requests.Session()
        retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(502, 503, 504),
                      allowed_methods=frozenset(["GET"]))
        self.s.mount("https://", HTTPAdapter(max_retries=retry))
        self._apollo_token = None
        self._apollo_at = 0

    # ------------------------------------------------------------ службове
    @property
    def access_token(self):
        return self.cookies.get("access_token")

    def token_expires_in(self):
        exp = jwt_exp(self.access_token or "")
        return None if exp is None else exp - int(time.time())

    def _log(self, step, method, url, status, req=None, resp=None):
        if not self.log_dir:
            return
        os.makedirs(self.log_dir, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "step": step, "method": method,
               "url": url, "status": status, "req": req, "resp": resp}
        with open(os.path.join(self.log_dir, time.strftime("%Y%m%d") + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str)[:20000] + "\n")

    def _cookie_header(self):
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def _h_www(self, auth=True):
        h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "uk", "version": "v1.19",
             "x-client": "DESKTOP", "x-platform-type": "mobile-html5", "x-device-id": self.device_id,
             "Origin": WWW, "Referer": WWW + "/d/uk/adding/", "Cookie": self._cookie_header()}
        if auth and self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    def _h_posting(self, posting_id):
        return {"User-Agent": UA, "Accept": "*/*", "Content-Type": "application/json",
                "Authorization": f"Bearer {self.access_token}", "postingid": posting_id,
                "x-client": "DESKTOP", "x-platform": "d", "x-platform-type": "mobile-html5",
                "Origin": WWW, "Referer": WWW + "/"}

    @staticmethod
    def _body(r):
        try:
            return r.json()
        except ValueError:
            return r.text[:2000]

    def _save_cookies(self):
        with open(self.cookie_path, "w", encoding="utf-8") as f:
            f.write(self._cookie_header())
        try:
            os.chmod(self.cookie_path, 0o600)
        except OSError:
            pass

    def try_refresh(self):
        """Відкрити кабінет з cookie: якщо сайт видасть новий access_token — зберегти."""
        old = self.access_token
        try:
            r = self.s.get(WWW + "/uk/myaccount/", headers={"User-Agent": UA, "Cookie": self._cookie_header()},
                           timeout=40, allow_redirects=True)
        except requests.RequestException:
            return False
        got = {c.name: c.value for c in r.cookies} if r.cookies else {}
        for resp in list(r.history) + [r]:
            for c in resp.cookies:
                got[c.name] = c.value
        self._log("refresh", "GET", "/uk/myaccount/", r.status_code,
                  resp={"new_cookies": sorted(got.keys())})
        if got.get("access_token") and got["access_token"] != old:
            self.cookies.update(got)
            self._save_cookies()
            return True
        return False

    def _request(self, step, method, url, headers_fn, retry_auth=True, **kw):
        r = self.s.request(method, url, headers=headers_fn(), timeout=kw.pop("timeout", 60), **kw)
        if r.status_code == 401 and retry_auth and self.try_refresh():
            r = self.s.request(method, url, headers=headers_fn(), timeout=60, **kw)
        return r

    # ------------------------------------------------------------ перевірка
    def me(self):
        r = self._request("me", "GET", WWW + "/api/v1/users/me/", lambda: self._h_www())
        body = self._body(r)
        self._log("me", "GET", "/api/v1/users/me/", r.status_code, resp=body)
        if r.status_code in (401, 403):
            raise AuthError(f"OLX не впізнав користувача (HTTP {r.status_code})")
        if r.status_code >= 400:
            raise OlxError(f"users/me: HTTP {r.status_code} {str(body)[:300]}")
        return body.get("data", body) if isinstance(body, dict) else {}

    # ------------------------------------------------------------ гео
    def location_autocomplete(self, query):
        r = self.s.get(WWW + "/api/v1/geo-encoder/location-autocomplete/",
                       params={"query": query, "limit": 10, "scope": "posting"},
                       headers=self._h_www(auth=False), timeout=40)
        r.raise_for_status()
        return (r.json() or {}).get("data") or []

    def city(self, city_id):
        r = self.s.get(f"{WWW}/api/v1/geo-encoder/cities/{city_id}/", headers=self._h_www(auth=False), timeout=40)
        r.raise_for_status()
        return (r.json() or {}).get("data") or {}

    # ------------------------------------------------------------ фото
    def apollo_token(self, fresh=False):
        if self._apollo_token and not fresh and time.time() - self._apollo_at < 240:
            return self._apollo_token
        r = self._request("apollo_token", "POST", WWW + "/api/v1/apollo/token/",
                          lambda: {**self._h_www(), "Content-Type": "application/json"}, data="{}")
        body = self._body(r)
        self._log("apollo_token", "POST", "/api/v1/apollo/token/", r.status_code,
                  resp=("<token>" if r.ok else body))
        if r.status_code in (401, 403):
            raise AuthError(f"OLX не дав токен для фото (HTTP {r.status_code})")
        if not r.ok:
            raise OlxError(f"apollo/token: HTTP {r.status_code} {str(body)[:300]}")
        tok = None
        if isinstance(body, dict):
            tok = body.get("token") or (body.get("data") or {}).get("token")
        if not tok:
            raise OlxError(f"apollo/token: немає token у відповіді: {str(body)[:300]}")
        self._apollo_token, self._apollo_at = tok, time.time()
        return tok

    def upload_image(self, data, content_type="image/jpeg"):
        """-> filename (id файлу на apollo)."""
        for attempt in (1, 2):
            tok = self.apollo_token(fresh=attempt == 2)
            expires = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            r = self.s.post(APOLLO + "/v1/temp-files", data=data, timeout=120,
                            headers={"User-Agent": UA, "Authorization": f"Bearer {tok}",
                                     "Content-Type": content_type, "Expires": expires,
                                     "Origin": WWW, "Referer": WWW + "/"})
            body = self._body(r)
            self._log("temp_files", "POST", "/v1/temp-files", r.status_code, req={"bytes": len(data)}, resp=body)
            if r.status_code in (401, 403) and attempt == 1:
                continue
            if not r.ok:
                raise OlxError(f"фото: HTTP {r.status_code} {str(body)[:300]}")
            fn = None
            if isinstance(body, dict):
                d = body.get("data") if isinstance(body.get("data"), dict) else body
                fn = d.get("filename") or d.get("id") or d.get("file_id")
            if not fn:
                raise OlxError(f"фото: немає filename у відповіді: {str(body)[:300]}")
            return fn
        raise OlxError("фото: не вдалося авторизуватись на apollo")

    @staticmethod
    def image_url(filename):
        return f"{APOLLO}/v1/files/{filename}/image"

    # ------------------------------------------------------------ оголошення
    def create_offer(self, payload):
        posting_id = str(uuid.uuid4())
        url = POSTING + "/api/v2/offers"
        r = self._request("offer", "POST", url, lambda: self._h_posting(posting_id),
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=90)
        body = self._body(r)
        self._log("offer", "POST", url, r.status_code, req=payload, resp=body)
        if r.status_code in (401, 403):
            raise AuthError(f"OLX відхилив авторизацію (HTTP {r.status_code}): {str(body)[:300]}")
        return r.status_code, body
