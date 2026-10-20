"""
Витягує структуровані дані об'єкта нерухомості з HTML-презентації RealtSoft CRM
(публічне посилання виду https://crm-capital.realtsoft.net/estate-XXXXX-....html)

Використання:
    # напряму за посиланням:
    python3 extract_estate.py "https://crm-capital.realtsoft.net/estate-109743-....html?t=z81b9S"

    # або з уже збереженого HTML-файлу:
    python3 extract_estate.py "presentation.html"

    # як бібліотека:
    from extract_estate import fetch_estate_data, extract_estate_data
    data = fetch_estate_data("https://crm-capital.realtsoft.net/estate-....html")
"""

import re
import json
import requests
from bs4 import BeautifulSoup

# Сайт віддає HTML одразу при звичайному GET-запиті (сервер-рендеринг),
# тож сесія/cookie не потрібні — досить представитись як звичайний браузер.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_estate_data(url: str, timeout: int = 15) -> dict:
    """Завантажує презентацію за посиланням і повертає розібрані дані."""
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    data = extract_estate_data(response.text)
    data["source_url"] = url
    return data


def extract_estate_data(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    data = {}

    # --- estate_id та координати (сидять у JS: FrontView.init({...})) ---
    script_text = soup.find(string=re.compile(r"FrontView\.init"))
    if script_text:
        m = re.search(r'"estate_id":\s*(\d+)', script_text)
        if m:
            data["estate_id"] = int(m.group(1))
        m_lat = re.search(r'"map_lat":\s*([\d.]+)', script_text)
        m_lng = re.search(r'"map_lng":\s*([\d.]+)', script_text)
        if m_lat and m_lng:
            data["lat"] = float(m_lat.group(1))
            data["lng"] = float(m_lng.group(1))

    # --- ціна ---
    price_block = soup.select_one(".pdf-price")
    if price_block:
        strong = price_block.find("strong")
        span = price_block.find("span")
        data["price"] = strong.get_text(strip=True) if strong else None
        data["price_per_m2"] = span.get_text(strip=True) if span else None

    # --- тип угоди / об'єкта (напр. "Продаж / Будинок") ---
    title = soup.select_one(".pdf-title")
    if title:
        data["deal_title"] = " ".join(title.get_text(separator=" ", strip=True).split())

    # --- адреса ---
    address = soup.select_one(".pdf-address")
    if address:
        data["address"] = " ".join(address.get_text(strip=True).split())

    # --- швидкі факти під адресою (площа/кімнати/поверх/ділянка) ---
    quick_facts = {}
    for span in soup.select(".pdf-detail span"):
        img = span.find("img")
        label = img.get("title") if img else None
        value = span.get_text(strip=True)
        if label:
            quick_facts[label] = value
    data["quick_facts"] = quick_facts

    # --- номер оголошення (артикул) ---
    article = soup.select_one(".pdf-article")
    if article:
        data["article"] = article.get_text(strip=True)

    # --- таблиця "ПАРАМЕТРИ" ---
    parameters = {}
    for table in soup.select("table.detail-view"):
        for row in table.find_all("tr"):
            th, td = row.find("th"), row.find("td")
            if th and td:
                parameters[th.get_text(strip=True)] = td.get_text(strip=True)
    data["parameters"] = parameters

    # --- опис (блок "ДОДАТКОВА ІНФОРМАЦІЯ") ---
    desc_div = None
    for h3 in soup.find_all("h3", class_="block-title"):
        if "ДОДАТКОВА ІНФОРМАЦІЯ" in h3.get_text() or "ДОПОЛНИТЕЛЬНАЯ ИНФОРМАЦИЯ" in h3.get_text():
            desc_div = h3.find_next_sibling("div")
            break
    if desc_div:
        # опис буває версткою через <br><br> МІЖ рядками одного div,
        # або версткою через окремі <p>абзац</p> без розділювачів між ними —
        # обидва варіанти зводимо до переносів рядків.
        for br in desc_div.find_all("br"):
            br.replace_with("\n")
        for p in desc_div.find_all("p"):
            p.insert_before("\n")
            p.unwrap()
        text = desc_div.get_text().strip()
        # прибираємо зайві порожні рядки (кожен <br><br> дає потрійний \n)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        text = text.strip()
        # артикул (#A33410 або A33410) часто продубльований в кінці опису
        if "article" in data:
            article_variants = (data["article"], data["article"].lstrip("#"))
            for variant in article_variants:
                if text.endswith(variant):
                    text = text[: -len(variant)].rstrip()
                    break
        data["description"] = text

    # --- фото (повнорозмірні, з водяним знаком) ---
    photos = []
    images_block = soup.select_one("#estate-images")
    if images_block:
        for a in images_block.select("a.fancybox"):
            href = a.get("href")
            if href:
                photos.append(href)
    data["photos"] = photos
    data["photos_count"] = len(photos)

    return data


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Використання: python3 extract_estate.py <URL або шлях до .html файлу>")
        sys.exit(1)

    source = sys.argv[1]
    if source.startswith("http://") or source.startswith("https://"):
        result = fetch_estate_data(source)
    else:
        with open(source, encoding="utf-8") as f:
            result = extract_estate_data(f.read())

    print(json.dumps(result, ensure_ascii=False, indent=2))
