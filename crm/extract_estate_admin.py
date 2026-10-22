"""
Окремий, незалежний скрипт: витягує дані об'єкта з АВТОРИЗОВАНОЇ (адмінської)
сторінки CRM — https://crm-capital.realtsoft.net/estate/<ID> — а не з публічної
презентації (якою займається extract_estate.py; цей файл його НЕ чіпає і ні на
що з нього не впливає).

Навіщо: на публічній презентації нема надійного поля "тип об'єкта" — є лише
заголовок (deal_title / .pdf-title), який заповнюється автоматично, але який
рієлтори іноді випадково міняють вручну, і тоді парсинг типу (земля/будинок/...)
ламається. На адмінській сторінці об'єкта є таблиця "Характеристики" з полями
"Категорія" (Житлова нерухомість / Комерційна нерухомість / Земельні ділянки) і
"Тип" (конкретний підтип, напр. "Земля під інд. будівництво") — ці поля в самій
CRM, судячи з усього, недоступні рієлтору для ручної зміни, тому надійніші за
заголовок.

Авторизація: лише через cookie-файл (так само, як cookies_file для dom.ria/
rieltor.ua/OLX) — жодних логінів/паролів цей скрипт не вводить і ніде не питає.
Cookie сюди вставляєш сам, окремим безпечним шляхом (НЕ в чат Claude) — поки що
просто поклади значення cookie одним рядком у файл crm_admin_cookie.txt поруч
із цим скриптом (цей файл - у .gitignore, у публічний репозиторій не потрапляє).

Використання:
    # за ID:
    python3 extract_estate_admin.py 112203

    # за повним посиланням:
    python3 extract_estate_admin.py "https://crm-capital.realtsoft.net/estate/112203"

    # з уже збереженої адмінської сторінки (Ctrl+S "Веб-сторінка, повністю"):
    python3 extract_estate_admin.py saved_page.html

    # як бібліотека:
    from extract_estate_admin import fetch_estate_admin_data, extract_estate_admin_data
    data = fetch_estate_admin_data(112203, cookie="...")
"""

import json
import os
import re
import sys

import requests
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(HERE, "crm_admin_cookie.txt")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# Ключі таблиці "Характеристики", які нас цікавлять найбільше.
KEY_CATEGORY = "Категорія"
KEY_TYPE = "Тип"
KEY_DEAL = "Тип угоди"


def load_cookie():
    if not os.path.exists(COOKIE_FILE):
        return None
    raw = open(COOKIE_FILE, encoding="utf-8").read().strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    return raw or None


def fetch_estate_admin_data(estate_id_or_url, cookie=None, timeout=15) -> dict:
    """Завантажує адмінську сторінку об'єкта за ID (або повним посиланням) і повертає розібрані дані.
    Потребує дійсний cookie залогіненої сесії CRM (НЕ той самий, що для dom.ria/rieltor/OLX)."""
    cookie = cookie or load_cookie()
    if not cookie:
        raise RuntimeError(
            f"Немає cookie для адмінки CRM. Поклади його одним рядком у {COOKIE_FILE} "
            f"(DevTools → Network → будь-який запит до crm-capital.realtsoft.net → Headers → "
            f"Request Headers → cookie). Увага: це ІНША авторизація, ніж для dom.ria/rieltor/OLX.")
    s = str(estate_id_or_url)
    url = s if s.startswith("http") else f"https://crm-capital.realtsoft.net/estate/{s}"
    headers = dict(HEADERS, Cookie=cookie)
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    if "login" in response.url or "Увійти" in response.text[:3000]:
        raise RuntimeError("Схоже, cookie не робочий (сторінка перекинула на логін) — онови його.")
    data = extract_estate_admin_data(response.text)
    data["source_url"] = url
    m = re.search(r"/estate/(\d+)", url)
    if m:
        data["estate_id"] = int(m.group(1))  # з URL, а не з першого посилання в тілі сторінки
    return data


def extract_estate_admin_data(html: str) -> dict:
    """Розбирає HTML адмінської сторінки об'єкта: усі таблиці 'Характеристики'/'Адреса'/
    'Службова інформація' (table.detail-view, та сама верстка, що й у публічній презентації,
    тільки тут полів набагато більше — включно з Категорією і Типом)."""
    soup = BeautifulSoup(html, "lxml")
    data = {}

    fields = {}
    for table in soup.select("table.detail-view"):
        for row in table.find_all("tr"):
            th, td = row.find("th"), row.find("td")
            if th and td:
                fields[th.get_text(strip=True)] = td.get_text(" ", strip=True)
    data["fields"] = fields

    data["category"] = fields.get(KEY_CATEGORY)
    data["type"] = fields.get(KEY_TYPE)
    data["deal"] = fields.get(KEY_DEAL)

    # best-effort: якщо немає URL (виклик напряму зі збереженого файлу) — перше посилання
    # /estate/<id> на сторінці найчастіше є у хлібних крихтах/заголовку; якщо знадобиться
    # точний ID, краще передавати його явно (fetch_estate_admin_data бере з URL/аргумента).
    m = re.search(r"/estate/(\d+)", html)
    if m:
        data["estate_id"] = int(m.group(1))
    m = re.search(r"ID:\s*(A?[\d.]+)", html)
    if m:
        data["article"] = m.group(1)

    return data


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Використання: python3 extract_estate_admin.py <ID|посилання|файл.html>")
        sys.exit(1)

    source = sys.argv[1]
    if source.startswith("http") or source.isdigit():
        result = fetch_estate_admin_data(source)
    else:
        with open(source, encoding="utf-8") as f:
            result = extract_estate_admin_data(f.read())

    print(json.dumps(result, ensure_ascii=False, indent=2))
