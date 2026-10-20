"""
Оновлення дати актуальності всіх активних оголошень на rieltor.ua.

Проходить по черзі всі твої активні оголошення (status=10) і для кожного
викликає "оновити дату" (та сама дія, що й кнопка в кабінеті) — підтверджено
на реальному HAR:
    GET https://rieltor.ua/api/offers/item-action/?id={ID}&action=refresh

Використання (перший тестовий запуск):
    python3 rieltor_refresh_dates.py

Скрипт сам запитає cookie один раз на початку (DevTools -> Network ->
будь-який запит до rieltor.ua -> Headers -> Request Headers -> cookie).
"""

import sys
import time
import requests

SITE_BASE_URL = "https://rieltor.ua"
HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "accept": "application/json, text/plain, */*",
}

DELAY_BETWEEN_SECONDS = 2  # пауза між оголошеннями, щоб не бомбардувати сервер запитами поспіль


def fetch_offers(cookie: str) -> list:
    """Повертає повний список активних оголошень (усі сторінки)."""
    headers = dict(HEADERS_BASE)
    headers["cookie"] = cookie

    all_items = []
    page = 1
    while True:
        resp = requests.get(
            f"{SITE_BASE_URL}/api/offers/list/",
            params={"page": page, "limit": 100, "status": 10, "mode": 10},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "OK":
            raise RuntimeError(f"Неочікувана відповідь API: {payload}")

        data = payload["data"]
        all_items.extend(data["items"])

        pagination = data.get("pagination", {})
        page_count = pagination.get("pageCount", 1)
        print(f"  Завантажено сторінку {page} з {page_count} ({len(data['items'])} оголошень на ній).")

        if page >= page_count:
            break
        page += 1

    return all_items


def refresh_offer(offer_id: int, cookie: str) -> dict:
    """Оновлює дату актуальності одного оголошення."""
    headers = dict(HEADERS_BASE)
    headers["cookie"] = cookie

    resp = requests.get(
        f"{SITE_BASE_URL}/api/offers/item-action/",
        params={"id": offer_id, "action": "refresh"},
        headers=headers,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def format_offer_line(offer: dict) -> str:
    return f"{offer.get('id')} {offer.get('address', '')} ({offer.get('price', '')})"


def run_once(cookie: str) -> None:
    print("Завантажую список активних оголошень...")
    offers = fetch_offers(cookie)
    total = len(offers)
    print(f"Знайдено {total} активних оголошень.\n")

    ok_count = 0
    fail_count = 0

    for i, offer in enumerate(offers, start=1):
        offer_id = offer["id"]
        print(f"[{i}/{total}] {format_offer_line(offer)}")
        try:
            result = refresh_offer(offer_id, cookie)
            if result.get("status") == "OK":
                print(f"  -> дату оновлено")
                ok_count += 1
            else:
                print(f"  -> ПОМИЛКА від сервера: {result}")
                fail_count += 1
        except Exception as e:  # noqa: BLE001
            print(f"  -> ПОМИЛКА запиту: {e}")
            fail_count += 1

        if i < total:
            time.sleep(DELAY_BETWEEN_SECONDS)

    print(f"\nГотово. Успішно: {ok_count}, з помилкою: {fail_count}, разом: {total}.")


if __name__ == "__main__":
    cookie = input("Встав cookie з rieltor.ua (один раз): ").strip()
    if not cookie:
        print("Cookie не введено, зупиняюсь.")
        sys.exit(1)

    try:
        run_once(cookie)
    except Exception as e:  # noqa: BLE001
        print(f"Критична помилка: {e}")
        sys.exit(1)
