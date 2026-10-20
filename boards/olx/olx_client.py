"""
Клієнт OLX (posting-services + www.olx.ua/api). Знято з HAR форми подачі оголошення.

Авторизація: AWS Cognito. Браузер шле `Authorization: Bearer <JWT access token>` (див. OlxClient).

Фото: POST /api/v1/apollo/token/ -> {"token"} ;
      POST https://ireland.apollo.olxcdn.com/v1/temp-files  (тіло — JPEG,
           Authorization: Bearer <apollo token>, Expires: завтра) -> {"data":{"filename"}}
Оголошення: POST https://posting-services.prd.01.eu-west-1.eu.olx.org/api/v2/offers
"""

import base64
import json
import os
import re
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


JWT_RE = re.compile(r"eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+")


def parse_cookie_string(raw):
    """Cookie з тексту: «a=1; b=2», з/без «cookie:», рядок Authorization ігнорується."""
    out = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        low = line.lower()
        if not line or low.startswith("authorization") or low.startswith("bearer"):
            continue
        if low.startswith("cookie:"):
            line = line.split(":", 1)[1]
        for part in line.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() and " " not in k.strip():
                    out[k.strip()] = v.strip()
    return out


def find_jwts(raw):
    return JWT_RE.findall(raw or "")


def jwt_claims(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


def jwt_exp(token):
    """Час закінчення JWT (unix) або None."""
    exp = jwt_claims(token or "").get("exp")
    return int(exp) if exp else None


def _read(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return ""


class OlxClient:
    """
    Авторизація OLX — AWS Cognito: браузер шле `Authorization: Bearer <JWT>` (живе ~1 год).
    Токен беремо з будь-якого з двох файлів (що свіжіший):
      olx_auth.txt    — надісланий рядок Authorization (Bearer …)
      olx_cookies.txt — cookie (якщо в них є токен: access_token / a_access_token / рядок Bearer)
    Коли токен прострочений — пробуємо оновити (Cognito refresh token з cookie, або кабінет сайту).
    """
    TOKEN_COOKIES = ("access_token", "a_access_token", "accessToken")

    def __init__(self, cookie_path, device_id, log_dir=None, auth_path=None):
        self.cookie_path = cookie_path
        self.auth_path = auth_path
        self.device_id = device_id
        self.log_dir = log_dir
        raw_c, raw_a = _read(cookie_path), _read(auth_path)
        self.cookies = parse_cookie_string(raw_c)
        cands = find_jwts(raw_a) + find_jwts(raw_c) + [self.cookies[k] for k in self.TOKEN_COOKIES if k in self.cookies]
        cands = [t for t in cands if jwt_claims(t).get("token_use", "access") != "id"] or cands
        self.token = max(cands, key=lambda t: jwt_exp(t) or 0) if cands else None
        self.s = requests.Session()
        retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(502, 503, 504),
                      allowed_methods=frozenset(["GET"]))
        self.s.mount("https://", HTTPAdapter(max_retries=retry))
        self._apollo_token = None
        self._apollo_at = 0

    # ------------------------------------------------------------ службове
    @property
    def access_token(self):
        return self.token

    def token_expires_in(self):
        exp = jwt_exp(self.token or "")
        return None if exp is None else exp - int(time.time())

    def describe(self):
        """Без секретів: назви cookie і поля токена — для діагностики."""
        c = jwt_claims(self.token or "")
        return {"cookie_names": sorted(self.cookies.keys()),
                "token": {k: c.get(k) for k in ("iss", "client_id", "token_use", "scope", "exp")} if c else None,
                "expires_in": self.token_expires_in()}

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
             "Origin": WWW, "Referer": WWW + "/d/uk/adding/"}
        if self.cookies:
            h["Cookie"] = self._cookie_header()
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _h_posting(self, posting_id):
        return {"User-Agent": UA, "Accept": "*/*", "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}", "postingid": posting_id,
                "x-client": "DESKTOP", "x-platform": "d", "x-platform-type": "mobile-html5",
                "Origin": WWW, "Referer": WWW + "/"}

    @staticmethod
    def _body(r):
        try:
            return r.json()
        except ValueError:
            return r.text[:2000]

    def _save_token(self, token):
        self.token = token
        if self.auth_path:
            with open(self.auth_path, "w", encoding="utf-8") as f:
                f.write("Authorization: Bearer " + token)
            try:
                os.chmod(self.auth_path, 0o600)
            except OSError:
                pass

    def _refresh_cognito(self):
        c = jwt_claims(self.token or "")
        iss, client_id = c.get("iss") or "", c.get("client_id")
        refresh = next((v for k, v in self.cookies.items() if "refresh" in k.lower() and len(v) > 100), None)
        if "cognito-idp." not in iss or not client_id or not refresh:
            self._log("refresh_cognito", "-", "-", None,
                      resp={"skip": True, "iss": iss, "client_id": bool(client_id), "refresh_cookie": bool(refresh)})
            return False
        region = iss.split("cognito-idp.")[1].split(".")[0]
        r = self.s.post(f"https://cognito-idp.{region}.amazonaws.com/", timeout=40,
                        headers={"Content-Type": "application/x-amz-json-1.1",
                                 "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"},
                        data=json.dumps({"AuthFlow": "REFRESH_TOKEN_AUTH", "ClientId": client_id,
                                         "AuthParameters": {"REFRESH_TOKEN": refresh}}))
        body = self._body(r)
        tok = ((body or {}).get("AuthenticationResult") or {}).get("AccessToken") if isinstance(body, dict) else None
        self._log("refresh_cognito", "POST", "cognito InitiateAuth", r.status_code,
                  resp={"ok": bool(tok), "err": None if tok else str(body)[:300]})
        if tok:
            self._save_token(tok)
            return True
        return False

    def _refresh_site(self):
        if not self.cookies:
            return False
        try:
            r = self.s.get(WWW + "/uk/myaccount/", headers={"User-Agent": UA, "Cookie": self._cookie_header()},
                           timeout=40, allow_redirects=True)
        except requests.RequestException:
            return False
        got = {}
        for resp in list(r.history) + [r]:
            for c in resp.cookies:
                got[c.name] = c.value
        self._log("refresh_site", "GET", "/uk/myaccount/", r.status_code, resp={"new_cookies": sorted(got)})
        new = [got[k] for k in self.TOKEN_COOKIES if got.get(k)] + find_jwts(r.text[:200000])
        new = [t for t in new if t != self.token and (jwt_exp(t) or 0) > (jwt_exp(self.token or "") or 0)]
        if new:
            self._save_token(max(new, key=lambda t: jwt_exp(t) or 0))
            return True
        return False

    def try_refresh(self):
        try:
            return self._refresh_cognito() or self._refresh_site()
        except requests.RequestException:
            return False

    def ensure_token(self):
        if not self.token:
            raise AuthError("немає токена авторизації (рядок Authorization: Bearer …)")
        left = self.token_expires_in()
        if left is not None and left < 120 and not self.try_refresh():
            raise AuthError("токен авторизації прострочений, оновити автоматично не вдалося")

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
            hint = str(body)[:200].replace("\n", " ")
            raise AuthError(f"OLX не впізнав користувача (HTTP {r.status_code}, server={r.headers.get('server')}): {hint}")
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
