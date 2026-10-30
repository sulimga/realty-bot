"""
Завантажує список фото (URL з CRM) на rieltor.ua і повертає список imageId,
готовий підставити прямо в rentaImages1 основного payload.

Використання:
    from rieltor_photos import upload_photos
    ids = upload_photos(crm_data["photos"], cookie)
"""

import io
import time
import requests
from PIL import Image

UPLOAD_URL = "https://rieltor.ua/api/offers/upload-photo/"
MIN_WIDTH = 1200  # запас вище за поріг відхилення (999px точно відхиляється)

# Ліміт rieltor «image_upload_rate_limit»: чекаємо RATE_LIMIT_WAIT і пробуємо знову, всього RATE_LIMIT_ATTEMPTS спроб
# (3 очікування = 15 хв). Сума ОБОВ'ЯЗКОВО менша за таймаут програми в диспетчері (boards.json, 25 хв) —
# інакше диспетчер вбиває програму і замість «ліміт фото, пакет зупинено» видно лише «працювала задовго».
RATE_LIMIT_WAIT = 300
RATE_LIMIT_ATTEMPTS = 4


class RateLimitExhausted(Exception):
    """Ліміт завантаження фото не знято навіть після всіх спроб очікування —
    сигнал зупинити весь пакетний процес, а не пропускати й продовжувати."""
    pass

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def upscale_if_needed(image_bytes: bytes, min_width: int = MIN_WIDTH) -> bytes:
    """Якщо фото вужче за min_width — розтягує його (LANCZOS) до потрібної
    ширини, зберігаючи пропорції. Це НЕ додає деталізації, лише інтерполює
    пікселі, щоб пройти перевірку мінімального розміру rieltor.ua."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.width >= min_width:
        return image_bytes
    scale = min_width / img.width
    new_size = (min_width, round(img.height * scale))
    resized = img.convert("RGB").resize(new_size, Image.LANCZOS)
    out = io.BytesIO()
    resized.save(out, format="JPEG", quality=90)
    return out.getvalue()


def upload_photos(photo_urls: list, cookie: str, delay_seconds: float = 0.5) -> list:
    """
    Завантажує кожне фото по черзі. Повертає список imageId (int) у тому
    самому порядку, що й вхідні URL. Якщо якесь фото не вдалось завантажити —
    воно пропускається (з повідомленням у консоль), решта продовжують іти.
    delay_seconds — пауза між фото, щоб не бомбардувати сервер запитами поспіль.

    При помилці "image_upload_rate_limit" (забагато завантажень поспіль,
    типово трапляється в пакетному режимі — багато оголошень підряд) —
    чекає 5 хв і повторює (див. RATE_LIMIT_*); не допомогло — RateLimitExhausted (пакет зупиняється).
    """
    headers = dict(HEADERS_BASE)
    headers["Cookie"] = cookie
    image_ids = []

    for i, url in enumerate(photo_urls, start=1):
        try:
            img_resp = requests.get(url, headers=HEADERS_BASE, timeout=20)
            img_resp.raise_for_status()
            original_bytes = img_resp.content
            img_bytes = upscale_if_needed(original_bytes)
            was_upscaled = img_bytes != original_bytes

            files = {"file": (f"photo_{i}.jpg", img_bytes, "image/jpeg")}

            data = None
            for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
                upload_resp = requests.post(UPLOAD_URL, headers=headers, files=files, timeout=30)
                upload_resp.raise_for_status()
                data = upload_resp.json()
                if data.get("error") != "image_upload_rate_limit":
                    break
                if attempt == RATE_LIMIT_ATTEMPTS:
                    waited = (RATE_LIMIT_ATTEMPTS - 1) * RATE_LIMIT_WAIT // 60
                    raise RateLimitExhausted(
                        f"ліміт завантаження фото на акаунт (фото {i}/{len(photo_urls)}) не знявся "
                        f"навіть за {waited} хв очікування — оголошення не створено, пакет зупинено. "
                        f"Спробуйте через кілька годин або завтра.")
                print(f"  [{i}/{len(photo_urls)}] rieltor: забагато фото поспіль — чекаю "
                      f"{RATE_LIMIT_WAIT // 60} хв і пробую ще раз ({attempt}/{RATE_LIMIT_ATTEMPTS - 1})…")
                time.sleep(RATE_LIMIT_WAIT)

            if "data" not in data:
                print(f"  [{i}/{len(photo_urls)}] ПОМИЛКА від сервера: {data}")
                continue
            image_id = data["data"]["imageId"]
            image_ids.append(image_id)
            tag = " (збільшено до мінімального розміру)" if was_upscaled else ""
            print(f"  [{i}/{len(photo_urls)}] OK -> imageId {image_id}{tag}")
        except RateLimitExhausted:
            raise  # свідомо не ловимо тут -- має зупинити весь процес, а не лише це фото
        except Exception as e:
            print(f"  [{i}/{len(photo_urls)}] ПОМИЛКА, фото пропущено: {e}")

        if delay_seconds:
            time.sleep(delay_seconds)

    return image_ids


if __name__ == "__main__":
    import sys
    import json
    from extract_estate import fetch_estate_data, extract_estate_data

    if len(sys.argv) < 3:
        print("Використання: python3 rieltor_photos.py <посилання CRM або .html файл> <COOKIE rieltor.ua>")
        sys.exit(1)

    source, cookie = sys.argv[1], sys.argv[2]
    if source.startswith("http://") or source.startswith("https://"):
        crm = fetch_estate_data(source)
    else:
        with open(source, encoding="utf-8") as f:
            crm = extract_estate_data(f.read())

    print(f"Знайдено {len(crm.get('photos', []))} фото, завантажую...")
    ids = upload_photos(crm.get("photos", []), cookie)
    print()
    print("Готовий масив для rentaImages1:")
    print(json.dumps(ids, ensure_ascii=False))
