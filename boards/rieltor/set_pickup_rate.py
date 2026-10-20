"""
Змінює ставку (кількість монеток за клік) на вже опублікованому оголошенні
rieltor.ua.

Використання:
    python3 set_pickup_rate.py <ID_оголошення> <ставка> <COOKIE>

Приклад:
    python3 set_pickup_rate.py 13038544 12 "ТУТ_COOKIE"
"""

import sys
import json
import requests

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
}


def set_pickup_rate(offer_id: int, rate: int, cookie: str,
                     autoupdate: int = 0, autoupdate_time: str = "09:00:00",
                     bird_premium: int = 0, lun_top: int = 0, quickly: int = 0) -> requests.Response:
    """
    Ставить нову ставку (rate) для вже існуючого оголошення.
    autoupdate/birdPremium/lunTop/quickly — додаткові платні опції
    просування, за замовчуванням вимкнені (0), як і в наших звичайних
    публікаціях.
    """
    headers = dict(HEADERS_BASE)
    headers["Cookie"] = cookie
    payload = {
        "rate": rate,
        "autoupdate": autoupdate,
        "autoupdateTime": autoupdate_time,
        "birdPremium": bird_premium,
        "lunTop": lun_top,
        "quickly": quickly,
        "itemType": None,
        "operType": None,
        "cityId": None,
    }
    url = f"https://rieltor.ua/api/offers/pickup-item/{offer_id}/"
    return requests.put(url, headers=headers, json=payload, timeout=20)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Використання: python3 set_pickup_rate.py <ID_оголошення> <ставка> <COOKIE>")
        sys.exit(1)

    offer_id = int(sys.argv[1])
    rate = int(sys.argv[2])
    cookie = sys.argv[3]

    resp = set_pickup_rate(offer_id, rate, cookie)
    print("Статус:", resp.status_code)
    print("Відповідь:", resp.text[:500])
