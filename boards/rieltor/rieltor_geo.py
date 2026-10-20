"""
Резолвить текстову адресу з CRM у ID-шники, які очікує форма створення
оголошення на rieltor.ua (regionId, cityId, districtId, streetId, houseId,
newhouseId, ptolemyHouseId) — або, якщо збігу немає, готує дані для
резервного варіанту (streetNameUnfound + координати).

Потребує авторизовану сесію rieltor.ua (cookie з браузера), бо довідники
геолокації доступні лише залогіненим користувачам кабінету.

Використання:
    from rieltor_geo import RieltorGeoClient, resolve_address

    client = RieltorGeoClient(cookie="ТВІЙ_COOKIE_РЯДОК")
    result = resolve_address(
        client,
        city_query="Київ",
        district_query="Печерський",
        street_query="Кловський узвіз",
        house_query="7",
        complex_query="Carnegie Tower",  # необов'язково
    )
    print(result)
"""

import re
import difflib
import requests

BASE = "https://rieltor.ua/api/geo"

_PAREN_RE = re.compile(r"\(.*?\)")
_PREFIX_RE = re.compile(
    r"^(вул\.|вулиця|просп\.|проспект|пров\.|провулок|пер\.|пл\.|площа|бульв\.|бульвар|наб\.|шосе|тупик)\s+",
    re.IGNORECASE,
)


def _normalize_street(name: str) -> str:
    name = _PAREN_RE.sub("", str(name) if name else "")
    name = _PREFIX_RE.sub("", name)
    return name.strip().lower()


def _pick_best_street(candidates: list, query: str | None):
    """Серед кандидатів (звужених по ЖК) обирає найбільш схожий на текст із CRM."""
    if not candidates:
        return None
    if not query:
        return candidates[0]
    nq = _normalize_street(query)
    return max(candidates, key=lambda c: difflib.SequenceMatcher(None, nq, _normalize_street(c["name"])).ratio())


class RieltorGeoClient:
    def __init__(self, cookie: str, timeout: int = 15):
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie,
        }

    def _get(self, path: str, params: dict) -> list:
        resp = requests.get(f"{BASE}/{path}/", params=params, headers=self.headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])

    def search_cities(self, query: str) -> list:
        return self._get("cities", {"query": query})

    def search_districts(self, city_id: int) -> list:
        return self._get("districts", {"cityId": city_id})

    def search_streets(self, query: str, city_id: int, district_id: int | None = None,
                        newhouse_id: int | None = None) -> list:
        params = {"query": query, "cityId": city_id}
        if district_id is not None:
            params["districtId"] = district_id
        if newhouse_id is not None:
            params["newhouseId"] = newhouse_id
        return self._get("streets", params)

    def search_houses(self, query: str, street_id: int, newhouse_id: int | None = None) -> list:
        params = {"query": query, "streetId": street_id}
        if newhouse_id is not None:
            params["newhouseId"] = newhouse_id
        return self._get("houses", params)

    def search_novostroyki(self, query: str, city_id: int) -> list:
        return self._get("novostroyki", {"query": query, "cityId": city_id})


def _best_match(candidates: list, key: str = "isMatched"):
    """Повертає перший елемент з isMatched=True, або перший елемент взагалі, або None."""
    if not candidates:
        return None
    for c in candidates:
        if c.get(key) is True:
            return c
    return candidates[0]


def _pick_house(houses: list, house_query: str | None):
    """
    Обирає будинок зі списку кандидатів на вулиці:
    - є houses, немає house_query (в CRM номер невідомий) -> беремо будь-який
      (перший), бо rieltor.ua вимагає хоч якийсь будинок для валідної адреси;
    - є house_query, є точний exactMatch -> беремо його;
    - є house_query, точного збігу нема -> беремо номер, найближчий за
      числовим значенням (напр. CRM каже "12", у довіднику є лише 10 і 14 ->
      береться 10 або 14, залежно що ближче).
    """
    if not houses:
        return None
    if not house_query:
        return houses[0]

    exact = next((h for h in houses if h.get("exactMatch")), None)
    if exact:
        return exact

    def _num(label):
        m = re.search(r"\d+", str(label) if label is not None else "")
        return int(m.group()) if m else None

    target = _num(house_query)
    if target is None:
        return houses[0]

    numbered = [(h, _num(h.get("name"))) for h in houses]
    numbered = [(h, n) for h, n in numbered if n is not None]
    if not numbered:
        return houses[0]

    best_house, _ = min(numbered, key=lambda hn: abs(hn[1] - target))
    return best_house


def resolve_address(
    client: RieltorGeoClient,
    city_query: str,
    street_query: str | None = None,
    house_query: str | None = None,
    district_query: str | None = None,
    complex_query: str | None = None,
) -> dict:
    """
    Повертає словник із ключами, готовими лягти прямо в payload /api/offers/:
    regionId, cityId, districtId, streetId, houseId, newhouseId, ptolemyHouseId,
    latitude, longitude — плюс "matched"/"method" для діагностики.

    Пріоритет:
    1) Якщо є назва ЖК і вона знаходиться в novostroyki — довіряємо ЖК, а не
       текстовій адресі з CRM (вона може бути неточною/застарілою). Вулицю й
       будинок при цьому шукаємо ЗВУЖЕНО, в межах цього ЖК (параметр
       newhouseId), бо у великих комплексах кілька корпусів можуть мати різні
       вулиці/номери — просте зіставлення по всьому місту дало б неправильний
       будинок. З короткого списку вулиць комплексу обираємо найбільш схожу
       на те, що написано в CRM (текстова схожість).
    2) Якщо ЖК не вказано або не знайдено — звичайний ланцюжок
       місто -> район -> вулиця -> будинок по всьому місту/району.
    3) Якщо й це не дало результату — повертаємо хоча б regionId/cityId, а
       виклик має підставити streetNameUnfound (вільний текст).

    ptolemyHouseId навмисно залишається None: у зафіксованій сесії він
    з'являвся через окрему прив'язку до позиції мітки на карті (інший
    провайдер геокодування), і надійного автоматичного способу його
    отримати поки не знайдено.
    """
    result = {
        "regionId": None, "cityId": None, "districtId": None,
        "streetId": None, "houseId": None, "newhouseId": None,
        "ptolemyHouseId": None, "latitude": None, "longitude": None,
        "matched": False, "method": None,
    }

    # --- Крок 1: місто (потрібен хоча б орієнтовний cityId для пошуку ЖК) ---
    cities = client.search_cities(city_query)
    if not cities:
        return result
    city = cities[0]
    result["cityId"] = int(city["cityId"])
    result["regionId"] = int(city["regionId"])
    result["latitude"] = float(city["lat"])
    result["longitude"] = float(city["lng"])
    has_districts = bool(city.get("hasDistricts"))

    # --- Крок 2: ЖК — головне джерело, якщо назва є і знайшлася ---
    if complex_query:
        complexes = client.search_novostroyki(complex_query, result["cityId"])
        if complexes:
            c = complexes[0]
            newhouse_id = int(c["novostroyId"])
            result["newhouseId"] = newhouse_id
            result["cityId"] = int(c["city"]["cityId"])
            result["regionId"] = int(c["region"]["id"])
            if c.get("district") and c["district"].get("id"):
                result["districtId"] = int(c["district"]["id"])
            result["latitude"] = float(c["lat"])
            result["longitude"] = float(c["lng"])

            # вулиці саме цього ЖК (може бути кілька корпусів на різних вулицях)
            streets_in_complex = client.search_streets(
                street_query or "", result["cityId"], result["districtId"], newhouse_id=newhouse_id
            )
            street = _pick_best_street(streets_in_complex, street_query)
            if street:
                street_id = int(street["streetId"])
                result["streetId"] = street_id
                result["latitude"] = float(street["lat"])
                result["longitude"] = float(street["lng"])

                houses = client.search_houses(house_query or "", street_id, newhouse_id=newhouse_id)
                house = _pick_house(houses, house_query)
                if house:
                    result["houseId"] = int(house["houseId"])
                    result["latitude"] = float(house["lat"])
                    result["longitude"] = float(house["lng"])
                    result["server_house_hints"] = {
                        k: house.get(k) for k in ("floorCnt", "wallType", "year", "ceilHeight", "houseProjectType")
                        if house.get(k) is not None
                    }
            if result["houseId"] is not None:
                # rieltor.ua приймає структуровану адресу з вулицею лише
                # разом із конкретним будинком — без houseId вважає невалідною
                result["matched"] = True
                result["method"] = "novostroyki_scoped"
                return result
            # немає houseId -> не лишаємо "напівструктуровану" адресу,
            # провалюємось до вільнотекстового варіанту нижче
            result["streetId"] = None
            result["newhouseId"] = None

    # --- Крок 3: район (звичайний шлях, без ЖК) ---
    district_id = None
    if has_districts and district_query:
        districts = client.search_districts(result["cityId"])
        match = next((d for d in districts if district_query.lower() in d["name"].lower()), None)
        if match:
            district_id = int(match["districtId"])
            result["districtId"] = district_id

    # --- Крок 4: вулиця + будинок по всьому місту/району ---
    if street_query:
        streets = client.search_streets(street_query, result["cityId"], district_id)
        street = _best_match(streets)
        if street and street.get("isMatched"):
            street_id = int(street["streetId"])
            result["streetId"] = street_id
            result["latitude"] = float(street["lat"])
            result["longitude"] = float(street["lng"])

            houses = client.search_houses(house_query or "", street_id)
            house = _pick_house(houses, house_query)
            if house:
                result["houseId"] = int(house["houseId"])
                result["latitude"] = float(house["lat"])
                result["longitude"] = float(house["lng"])
                result["server_house_hints"] = {
                    k: house.get(k) for k in ("floorCnt", "wallType", "year", "ceilHeight", "houseProjectType")
                    if house.get(k) is not None
                }
            if result["houseId"] is not None:
                result["matched"] = True
                result["method"] = "street_chain"
                return result
            # немає houseId -> не лишаємо "напівструктуровану" адресу
            result["streetId"] = None

    result["method"] = "city_only"
    return result


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Використання: python3 rieltor_geo.py <COOKIE> [місто] [вулиця] [будинок] [район] [жк]")
        sys.exit(1)

    cookie = sys.argv[1]
    client = RieltorGeoClient(cookie)
    result = resolve_address(
        client,
        city_query=sys.argv[2] if len(sys.argv) > 2 else "Київ",
        street_query=sys.argv[3] if len(sys.argv) > 3 else None,
        house_query=sys.argv[4] if len(sys.argv) > 4 else None,
        district_query=sys.argv[5] if len(sys.argv) > 5 else None,
        complex_query=sys.argv[6] if len(sys.argv) > 6 else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
