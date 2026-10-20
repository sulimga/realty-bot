"""
Клієнт для dom.ria.com (DIM.RIA) — ті самі запити, що робить форма
подачі оголошення в браузері (відновлено з HAR від 19.10.2026).

Авторизація — cookie сесії з браузера (файл cookies.txt, див. README).
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://dom.ria.com"
PHOTO_UPLOAD_URL = "https://9.riastatic.com/upload/v4/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
LANG_UK = 4
PARTNER_ID = 5          # так шле форма add_form_version 3
TECHPASS_FROM_ID = 31   # fromId для техпаспорта на кроці оплати


class DomRiaError(Exception):
    pass


class AuthError(DomRiaError):
    pass


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def find_ids(obj, key_hint=("id",), min_value=1000):
    """Рекурсивно шукає числові ID у відповіді (ключі, що містять 'id')."""
    found = []

    def walk(o, key=""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        else:
            if any(h in key.lower() for h in key_hint):
                try:
                    n = int(str(o))
                    if n >= min_value:
                        found.append((key, n))
                except (TypeError, ValueError):
                    pass

    walk(obj)
    return found


class DomRiaClient:
    def __init__(self, cookie_string: str, user_id: int, log_dir: str = "logs",
                 verbose: bool = True):
        self.cookie = cookie_string.strip()
        self.user_id = int(user_id)
        self.log_dir = log_dir
        self.verbose = verbose
        os.makedirs(log_dir, exist_ok=True)
        self.s = requests.Session()
        # Повтор ЛИШЕ для читання (GET): dom.ria інколи відповідає повільно.
        # POST (створення/зміна оголошення) не повторюємо — щоб не було дублів.
        retry = Retry(total=3, connect=3, read=3, backoff_factor=2,
                      status_forcelist=(502, 503, 504), allowed_methods=frozenset(["GET"]),
                      raise_on_status=False)
        self.s.mount("https://", HTTPAdapter(max_retries=retry))
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "uk-UA,uk;q=0.9",
        })

    # ------------------------------------------------------------ службове
    def _h(self, referer_path="/v2/new_design/uk/add_form/", extra=None, auth=True):
        h = {"Origin": BASE, "Referer": BASE + referer_path}
        if auth and self.cookie:
            h["Cookie"] = self.cookie
        if extra:
            h.update(extra)
        return h

    def _log(self, name, req=None, resp=None):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(self.log_dir, f"{ts}_{name}.json")
        body = None
        if resp is not None:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text[:20000]
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "request": req,
                "status": getattr(resp, "status_code", None),
                "response": body,
            }, f, ensure_ascii=False, indent=2)
        return body

    def _json(self, resp, name, req=None):
        body = self._log(name, req, resp)
        if resp.status_code in (401, 403):
            raise AuthError(f"{name}: HTTP {resp.status_code} — схоже, cookie застаріли")
        if isinstance(body, dict) and "not allowed" in str(body.get("msg", "")).lower():
            raise AuthError(f"{name}: {body['msg']} — cookie застаріли або не з того акаунта")
        if resp.status_code >= 400:
            raise DomRiaError(f"{name}: HTTP {resp.status_code}: {str(body)[:500]}")
        return body

    # ------------------------------------------------------------ авторизація
    def check_auth(self):
        r = self.s.get(BASE + "/v1/api/user/balance", headers=self._h(), timeout=40)
        return self._json(r, "check_auth")

    # ------------------------------------------------------------ гео (публічне)
    def geo_by_point(self, lat, lng):
        r = self.s.get(BASE + "/node/geo/getGeoByPoint", params={
            "latitude": lat, "longitude": lng, "langId": LANG_UK,
            "withStreetData": 1, "withBuildingData": 1, "withNewbuild": 1, "withMetro": 1,
        }, headers=self._h(auth=False), timeout=40)
        return self._json(r, "geo_by_point")

    def point_by_geo(self, city_id, street_id, building_num, address_text=""):
        r = self.s.get(BASE + "/node/geo/getPointByGeo", params={
            "cityId": city_id, "streetId": street_id, "buildingNum": building_num,
            "address": address_text,
        }, headers=self._h(auth=False), timeout=40)
        return self._json(r, "point_by_geo")

    def autocomplete_city(self, text):
        r = self.s.get(BASE + "/node/api/autocompleteV3", params={
            "lang_id": LANG_UK, "onlyCity": 1, "fromAddForm": 1, "text": text,
        }, headers=self._h(auth=False), timeout=40)
        return self._json(r, "autocomplete_city")

    def autocomplete_street(self, city_id, text):
        r = self.s.get(BASE + "/node/api/autocompleteV3", params={
            "type": "street", "fromAddForm": 1, "city_id": city_id,
            "lang_id": LANG_UK, "text": text,
        }, headers=self._h(auth=False), timeout=40)
        return self._json(r, "autocomplete_street")

    def building_by_location(self, state_id, city_id, street_id, building_no):
        r = self.s.get(BASE + "/v1/api/building/by-location", params={
            "city_id": city_id, "state_id": state_id, "street_id": street_id,
            "building_no": building_no, "newAddForm": 1, "building_entity_id": "",
        }, headers=self._h(), timeout=40)
        return self._json(r, "building_by_location")

    def get_realty(self, realty_id):
        r = self.s.get(f"{BASE}/node/getCouchRealtyById/{realty_id}/",
                       headers=self._h(), timeout=40)
        return self._json(r, "get_realty")

    # ------------------------------------------------------------ оголошення
    def save_realty(self, payload: dict, step="main_chars"):
        payload = dict(payload)
        payload["end_time"] = _now_iso()
        r = self.s.post(BASE + "/node/saveRealty", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        headers=self._h(f"/v2/new_design/uk/add_form/{step}?onSite=0",
                                        {"Content-Type": "application/json",
                                         "partnerLogId": str(PARTNER_ID)}),
                        timeout=40)
        body = self._json(r, f"save_realty_{step}", payload)
        rid = None
        if isinstance(body, dict):
            rid = body.get("realty_id")
            if not rid and isinstance(body.get("data"), dict):
                rid = body["data"].get("realty_id")
        if not rid:
            rid = payload.get("realty_id")
        if not rid:
            raise DomRiaError(f"saveRealty не повернув realty_id: {str(body)[:500]}")
        return int(rid), body

    # ------------------------------------------------------------ фото
    def upload_photo(self, realty_id, image_bytes, content_type="image/jpeg"):
        files = {
            "name": (None, "undefined"),
            "realtyId": (None, str(realty_id)),
            "area": (None, "dom/photo"),
            "isUpload": (None, "1"),
            "file0": ("blob", image_bytes, content_type),
        }
        # Форма шле це на інший домен без cookie (axios без withCredentials)
        r = self.s.post(PHOTO_UPLOAD_URL, files=files,
                        headers=self._h("/v2/new_design/uk/add_form/photo?onSite=0", auth=False),
                        timeout=90)
        body = self._json(r, "upload_photo", {"realtyId": realty_id, "bytes": len(image_bytes)})
        ids = [n for k, n in find_ids(body, min_value=10_000_000) if "realty" not in k.lower()]
        if not ids:
            raise DomRiaError(f"Не знайшов ID фото у відповіді: {str(body)[:500]}")
        return str(ids[0]), body

    def save_photos_ordering(self, realty_id, photo_ids):
        payload = {"realtyId": int(realty_id), "ordering": [str(p) for p in photo_ids]}
        r = self.s.post(BASE + "/node/savePhotosOrdering/", json=payload,
                        headers=self._h("/v2/new_design/uk/add_form/main_chars?onSite=0"),
                        timeout=30)
        return self._json(r, "save_photos_ordering", payload)

    # ------------------------------------------------------------ техпаспорт
    def upload_techpassport_file(self, realty_id, filename, data, content_type):
        files = {
            "attachment": (filename, data, content_type),
            "entity_type": (None, "techpassport"),
            "realty_id": (None, str(realty_id)),
            "fromId": (None, str(TECHPASS_FROM_ID)),
        }
        r = self.s.post(BASE + "/uploadfile/", files=files,
                        headers=self._h("/v2/new_design/uk/add_form/payment?onSite=0"),
                        timeout=120)
        body = self._json(r, "upload_techpassport", {"realty_id": realty_id, "file": filename})
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                pass
        file_ids = [n for k, n in find_ids(body, min_value=1) if "realty" not in k.lower()]
        names = re.findall(r'[0-9a-f]{16,}\.[a-zA-Z0-9]{2,5}', json.dumps(body))
        if not file_ids or not names:
            raise DomRiaError(
                "Не зміг розібрати відповідь завантаження техпаспорта "
                f"(file_ids={file_ids}, names={names}): {str(body)[:500]}")
        return str(file_ids[0]), names[0], body

    def create_techpassport_order(self, realty_id, realty_type_parent_id, geo, file_ids, documents,
                                  comment=""):
        payload = {
            "userId": str(self.user_id),
            "realtyId": int(realty_id),
            "realtyTypeParentId": int(realty_type_parent_id),
            "geoData": {
                "state_id": geo["state_id"], "city_id": geo["city_id"],
                "street_id": geo.get("street_id", 0),
                "building_number": str(geo.get("building_number_str", "") or ""),
                "flat_number": str(geo.get("flat_number", "") or ""),
            },
            "fileIds": [str(x) for x in file_ids],
            "fromId": TECHPASS_FROM_ID,
            "typeInspection": "tech_passport_verified",
            "documents": list(documents),
            "userComment": comment,
        }
        r = self.s.post(BASE + "/v1/api/inspections/sendRealtyDataAndCreateInspectionOrder/",
                        json=payload,
                        headers=self._h("/v2/new_design/uk/add_form/payment?onSite=0"),
                        timeout=40)
        body = self._json(r, "techpassport_order", payload)
        if not (isinstance(body, dict) and body.get("status") is True):
            raise DomRiaError(f"dom.ria не прийняла техпаспорт на перевірку: {str(body)[:300]}")
        return body

    def publish_free_by_techpassport(self, realty_id):
        payload = {
            "buy_from": "new_payment_step", "autoRepublish": 0,
            "publish_free_by_inspection": 7, "realty_id": int(realty_id),
            "userIdByPublish": str(self.user_id), "free_publicate": 7, "langId": LANG_UK,
        }
        r = self.s.post(BASE + "/node/api/publishByVideoInspection/", json=payload,
                        headers=self._h("/v2/new_design/uk/add_form/payment?onSite=0"),
                        timeout=40)
        body = self._json(r, "publish", payload)
        # реальна відповідь: {"status": true, ...} або {"status": false, "data": {"missingCharacteristics": true}}
        if not (isinstance(body, dict) and body.get("status") is True):
            data = (body or {}).get("data") or {} if isinstance(body, dict) else {}
            if data.get("missingCharacteristics"):
                raise DomRiaError(f"dom.ria НЕ опублікувала {realty_id}: не заповнені обов'язкові характеристики. "
                                  f"Заповніть їх у кабінеті й опублікуйте там (техпаспорт уже відправлено)")
            raise DomRiaError(f"dom.ria НЕ опублікувала {realty_id}: {str(body)[:300]}")
        return body

    def check_main_characteristics(self, realty_id):
        """Той самий запит, що робить форма на кроці оплати: чи всі обов'язкові поля заповнені."""
        payload = {"realtyId": int(realty_id), "langId": LANG_UK}
        r = self.s.post(BASE + "/node/checkIfRealtyHasMainCharacteristics", json=payload,
                        headers=self._h("/v2/new_design/uk/add_form/payment?onSite=0"), timeout=30)
        return self._json(r, "check_main_characteristics", payload)

    def publication_price(self, realty_id):
        r = self.s.get(f"{BASE}/v1/api/payments/get-publication-price/{realty_id}",
                       headers=self._h("/v2/new_design/uk/add_form/payment?onSite=0"), timeout=40)
        return self._json(r, "publication_price")
