"""
Тести презентації на штучних даних, без мережі:  python boards/presentation/test_presentation.py
(форма даних — як у API OLX /api/v1/offers/<id>/, стані сторінки й розмітці; ті самі випадки, що в
extension/olx-presentation/test/normalize.test.js, плюс розбір HTML і збирання PDF).
"""

import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from normalize import (format_address, normalize_olx, parse_price, presentation_title, quick_facts,  # noqa: E402
                       strip_contacts, unit_price)
from olx_source import is_ad_url, parse_page  # noqa: E402
from telegraph import build_page  # noqa: E402

N = 0


def t(name):
    def deco(fn):
        global N
        fn()
        N += 1
        print("✓", name)
        return fn
    return deco


def row(m, k):
    return next((r["value"] for r in m["rows"] if r["key"] == k), None)


API_FLAT = {
    "url": "https://www.olx.ua/d/uk/obyavlenie/x-IDabc12.html", "id": "900000001", "notes": [],
    "api": {
        "id": 900000001, "title": "Продам 3к квартиру!!!",
        "description": "Гарна квартира<br />\nТелефон: +38 (000) 000-00-00<br /><br />Пишіть @owner_kyiv або https://t.me/x<br />Ціна 95 000 $, 2001-2010 р.",
        "category": {"id": 1758, "type": "real_estate"},
        "location": {"city": {"id": 268, "name": "Київ"}, "district": {"id": 15, "name": "Печерський"}, "region": {"id": 25, "name": "Київська область"}},
        "params": [
            {"key": "price", "name": "Ціна", "type": "price", "value": {"value": 95000, "currency": "USD", "label": "95 000 $"}},
            {"key": "private_business", "name": "Приватна особа / Бізнес", "type": "select", "value": {"key": "private", "label": "Приватна особа"}},
            {"key": "cooperate", "name": "Готовий співпрацювати з ріелторами", "type": "checkbox", "value": {"key": "1", "label": "Так"}},
            {"key": "property_type_appartments_sale", "name": "Тип будинку", "type": "select", "value": {"key": "2", "label": "Сталінка"}},
            {"key": "floor", "name": "Поверх", "type": "input", "value": {"key": "5", "label": "5"}},
            {"key": "total_floors", "name": "Поверховість", "type": "input", "value": {"key": "9", "label": "9"}},
            {"key": "total_area", "name": "Загальна площа", "type": "input", "value": {"key": "85", "label": "85 м²"}},
            {"key": "kitchen_area", "name": "Площа кухні", "type": "input", "value": {"key": "12", "label": "12 м²"}},
            {"key": "number_of_rooms_string", "name": "Кількість кімнат", "type": "select", "value": {"key": "trehkomnatnye", "label": "3 кімнати"}},
            {"key": "repair", "name": "Ремонт", "type": "select", "value": {"key": "2", "label": "Євроремонт"}},
            {"key": "pets", "name": "Домашні улюбленці", "type": "select", "value": {"key": "yes_cat", "label": "Так, котик 😸"}},
        ],
        "photos": [
            {"id": 1, "link": "https://ireland.apollo.olxcdn.com:443/v1/files/aaa-UA/image;s={width}x{height}", "width": 2000, "height": 1500},
            {"id": 2, "link": "https://ireland.apollo.olxcdn.com:443/v1/files/bbb-UA/image;s={width}x{height}", "width": 900, "height": 1200},
            {"id": 3, "link": "https://ireland.apollo.olxcdn.com:443/v1/files/aaa-UA/image;s={width}x{height}", "width": 2000, "height": 1500},
        ],
    },
}


@t("квартира з API: тип, призначення, адреса, площі, кімнати, поверх, ціна за м²")
def _():
    m = normalize_olx(API_FLAT)
    assert m["kicker"] == "Продаж / Квартира"
    assert row(m, "address") == "Київ, Печерський район"
    assert row(m, "purpose") == "Житлова"
    assert row(m, "total_area") == "85 м²" and row(m, "kitchen_area") == "12 м²"
    assert row(m, "rooms") == "3 кімнати" and row(m, "floor") == "5"
    assert m["price"]["text"] == "95 000 $"
    assert unit_price(m) == "1 118 $/м²", unit_price(m)
    assert quick_facts(m["rows"]) == ["85 м²", "3 кімн.", "5/9 пов."], quick_facts(m["rows"])


@t("службові параметри OLX не потрапляють, емодзі прибрано")
def _():
    m = normalize_olx(API_FLAT)
    labels = [r["label"] for r in m["rows"]]
    assert not any(x in "|".join(labels) for x in ("ріелтор", "Приватна", "Ціна")), labels
    assert next(r for r in m["rows"] if r["label"] == "Домашні улюбленці")["value"] == "Так, котик"
    assert next(r for r in m["rows"] if r["label"] == "Тип будинку")["value"] == "Сталінка"


@t("опис: без телефонів, посилань і @нікнеймів; роки й ціни лишаються")
def _():
    d = normalize_olx(API_FLAT)["description"]
    assert not any(x in d for x in ("000-00-00", "t.me", "@owner", "Телефон")), d
    assert "95 000 $" in d and "2001-2010" in d, d


@t("опис: залишки контактів і переноси <br>")
def _():
    m = normalize_olx({"api": {"description": "Рядок 1<br />\nРядок 2<br /><br />Телефонуйте: 000 000 00 00 (Viber)<br />"
                                              "Пишіть у Telegram @owner_kyiv<br />На 5 поверсі є ліфт.<br />Телевізор залишається."}})
    assert m["description"] == "Рядок 1\nРядок 2\n\nНа 5 поверсі є ліфт.\nТелевізор залишається.", repr(m["description"])


@t("фото: розмір з шаблону, без :443, без дублів")
def _():
    assert normalize_olx(API_FLAT)["photos"] == [
        "https://ireland.apollo.olxcdn.com/v1/files/aaa-UA/image;s=1600x1200",
        "https://ireland.apollo.olxcdn.com/v1/files/bbb-UA/image;s=900x1200",
    ]


@t("телефони в різних записах")
def _():
    for s in ["0000000000", "000 000 00 00", "+380000000000", "+38 (000) 000-00-00", "(000)000-00-00", "38 000 000 00 00"]:
        assert " ".join(strip_contacts("Дзвоніть " + s + " зранку").split()) == "Дзвоніть зранку", s
    for s in ["1 050 000 грн", "кадастр 3220882200:03:005:1306", "площа 0,85 га", "2001-2010"]:
        assert strip_contacts(s) == s, s


@t("будинок зі стану сторінки (без API): тип з параметра, ділянка, ціна за м²")
def _():
    m = normalize_olx({"url": "u", "main": {
        "id": 5, "title": "Будинок", "description": "Опис", "category": {"id": 1602},
        "location": {"cityName": "Лісники", "districtName": "", "regionName": "Київська область"},
        "price": {"displayValue": "250 000 $", "regularPrice": {"value": 250000, "currencyCode": "USD"}},
        "params": [
            {"key": "property_type_houses", "name": "Тип будинку", "value": "Котедж", "normalizedValue": "cottage"},
            {"key": "total_area", "name": "Загальна площа", "value": "200 м²", "normalizedValue": "200"},
            {"key": "land_area", "name": "Площа ділянки", "value": "10", "normalizedValue": "10"},
            {"key": "total_floors", "name": "Поверховість", "value": "2", "normalizedValue": "2"},
        ],
        "photos": ["https://ireland.apollo.olxcdn.com:443/v1/files/zzz/image;s=1000x700"]}})
    assert m["kicker"] == "Продаж / Котедж"
    assert row(m, "address") == "Лісники, Київська область"
    assert row(m, "land_area") == "10 сот."
    assert not any(r["label"] == "Тип будинку" for r in m["rows"])
    assert unit_price(m) == "1 250 $/м²"
    assert quick_facts(m["rows"]) == ["200 м²", "2-поверх.", "10 сот."], quick_facts(m["rows"])


@t("оренда офісу: нежитлова, ціна / міс.")
def _():
    m = normalize_olx({"api": {"category": {"id": 3690}, "location": {"city": {"name": "Київ"}}, "params": [
        {"key": "price", "name": "Ціна", "value": {"value": 30000, "currency": "UAH", "label": "30 000 грн."}},
        {"key": "total_area", "name": "Загальна площа", "value": {"key": "60", "label": "60 м²"}}]}})
    assert m["kicker"] == "Оренда / Офіс"
    assert row(m, "purpose") == "Нежитлова (комерційна)"
    assert m["price"]["text"] == "30 000 грн. / міс."
    assert unit_price(m) == "500 грн/м²"


@t("земля: призначення з типу землі, ціна за сотку")
def _():
    m = normalize_olx({"api": {"category": {"id": 1608}, "params": [
        {"key": "price", "name": "Ціна", "value": {"value": 30000, "currency": "USD", "label": "30 000 $"}},
        {"key": "property_type_land", "name": "Тип нерухомості", "value": {"key": "2", "label": "Земля житлової й суспільної забудови"}},
        {"key": "land_area", "name": "Площа ділянки", "value": {"key": "15", "label": "15 сот."}}]}})
    assert m["kicker"] == "Продаж / Земельна ділянка"
    assert row(m, "purpose") == "Земля житлової й суспільної забудови"
    assert unit_price(m) == "2 000 $/сот."


@t("адреса: область не дублюється для обласних центрів; ціна з тексту")
def _():
    assert format_address("Одеса", "Приморський", "Одеська область") == "Одеса, Приморський район"
    assert format_address("Кривий Ріг", "", "Дніпропетровська область") == "Кривий Ріг, Дніпропетровська область"
    assert parse_price("1 050 000 грн.") == (1050000.0, "UAH")


PAGE = """<!doctype html><html><head><meta property="og:title" content="Здам 1к квартиру">
<script type="application/ld+json">{"@type":"Product","name":"Здам 1к","image":["https://ireland.apollo.olxcdn.com:443/v1/files/q1/image;s=1000x750"],
"offers":{"price":12000,"priceCurrency":"UAH","areaServed":{"name":"Київ"}}}</script></head><body>
<ol data-testid="breadcrumbs"><li><a>Головна</a></li><li><a>Нерухомість</a></li><li><a>Квартири</a></li><li><a>Довгострокова оренда квартир</a></li></ol>
<div data-testid="image-galery-container"><img src="https://ireland.apollo.olxcdn.com:443/v1/files/q1/image;s=1000x750">
<img srcset="https://ireland.apollo.olxcdn.com/v1/files/q2/image;s=500x375 500w, https://ireland.apollo.olxcdn.com/v1/files/q2/image;s=1000x750 1000w"></div>
<div data-cy="ad_title"><h4>Здам 1к квартиру</h4></div>
<div data-testid="ad-price-container"><h3>12 000 грн.</h3><p>Договірна</p></div>
<div data-testid="ad-parameters-container"><p>Приватна особа</p><p>Поверх: 3</p><p>Поверховість: 16</p><p>Загальна площа: 40 м²</p>
<p>Кількість кімнат: 1 кімната</p><p>Ремонт: Косметичний ремонт</p></div>
<div data-cy="ad_description"><div>Затишна квартира.<br>Дзвоніть +380000000001</div></div>
<div data-testid="map-aside-section"><p>Київ, Оболонський</p><p>Київська область</p></div>
<div data-cy="ad-footer-bar-section"><span>ID: 900000002</span><span>Переглядів: 10</span></div>
<script>window.__PRERENDERED_STATE__= @@STATE@@;</script></body></html>"""


@t("розбір HTML без стану: ID з підвалу, параметри, фото з галереї, оренда")
def _():
    raw = parse_page(PAGE.replace("@@STATE@@", "null"), "https://www.olx.ua/d/uk/obyavlenie/zdam-IDAbC34.html")
    assert raw["id"] == "900000002", raw["id"]
    assert raw["main"] is None
    m = normalize_olx(raw)
    assert m["kicker"] == "Оренда / Квартира", m["kicker"]
    assert row(m, "address") == "Київ, Оболонський район", row(m, "address")
    assert m["price"]["text"] == "12 000 грн. / міс."
    assert quick_facts(m["rows"]) == ["40 м²", "1 кімн.", "3/16 пов."]
    assert len(m["photos"]) == 2 and m["photos"][1].endswith("s=1000x750"), m["photos"]
    assert "+380000000001" not in m["description"] and "Затишна квартира." in m["description"], m["description"]


@t("розбір HTML зі станом сторінки (рядок JSON у рядку JS); стан чужого оголошення відкидається")
def _():
    state = {"ad": {"ad": {"id": 900000003, "url": "https://www.olx.ua/d/uk/obyavlenie/zdam-IDAbC34.html", "title": "зі стану",
                           "category": {"id": 1760}, "user": {"name": "Продавець"}}}}
    page = PAGE.replace("@@STATE@@", json.dumps(json.dumps(state, ensure_ascii=False), ensure_ascii=False))
    raw = parse_page(page, "https://www.olx.ua/d/uk/obyavlenie/zdam-IDAbC34.html")
    assert raw["main"]["id"] == 900000003 and "user" not in raw["main"], raw["main"]
    raw2 = parse_page(page, "https://www.olx.ua/d/uk/obyavlenie/insha-IDzzz99.html")
    assert raw2["main"] is None and any("іншого оголошення" in n for n in raw2["notes"])
    assert is_ad_url("https://www.olx.ua/d/uk/obyavlenie/zdam-IDAbC34.html")
    assert is_ad_url("https://m.olx.ua/d/obyavlenie/zdam-IDAbC34.html?reason=x")
    assert not is_ad_url("https://crm-capital.realtsoft.net/estate-1.html")


@t("Telegraph: вузли сторінки, підпис ріелтора")
def _():
    m = normalize_olx(API_FLAT)
    p = build_page(m, {"name": "Олег", "phone": "+380000000000"})
    assert p["title"] == "Продаж / Квартира, 85 м² — Київ, Печерський район", p["title"]
    cj = json.dumps(p["content"], ensure_ascii=False)
    assert cj.count('"img"') == 2 and "1 118 $/м²" in cj and "Олег · +380000000000" in cj
    assert "Олег" not in json.dumps(build_page(m, {})["content"], ensure_ascii=False)
    assert presentation_title({"kicker": "", "rows": []}) == "Презентація об'єкта"


@t("PDF збирається: кирилиця, фото, кілька сторінок")
def _():
    from PIL import Image
    import render_pdf
    fitz = render_pdf.fitz
    imgs = []
    for i, (w, h) in enumerate([(1000, 750), (750, 1000), (1200, 800), (1000, 750), (900, 600)]):
        b = io.BytesIO()
        Image.new("RGB", (w, h), (40 * i, 120, 200 - 30 * i)).save(b, "WEBP" if i == 1 else "JPEG")
        imgs.append(b.getvalue())
    imgs.append(b"not an image")
    m = normalize_olx(API_FLAT)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "p.pdf")
        n = render_pdf.render(m, {"name": "Олег Іваненко", "phone": "+380 00 000 00 00"}, imgs, out)
        assert n == 5, n
        doc = fitz.open(out)
        text = "".join(pg.get_text() for pg in doc)
        for s in ["Продаж / Квартира", "95 000 $", "1 118 $/м²", "Київ, Печерський район", "ПАРАМЕТРИ", "Житлова",
                  "Євроремонт", "ДОДАТКОВА ІНФОРМАЦІЯ", "Гарна квартира", "Олег Іваненко"]:
            assert s in text, s
        assert sum(len(pg.get_images()) for pg in doc) == 5
        assert doc.metadata["title"].startswith("Продаж / Квартира")



# ---------------------------------------------------------------- dom.ria
from normalize_domria import normalize_domria  # noqa: E402
from domria_source import ad_id as dr_id, is_ad_url as dr_is_ad, parse_page as dr_parse, uk_url  # noqa: E402

DR_URL = "https://dom.ria.com/uk/realty-prodaja-kvartira-kiev-shevchenkovskiy-glubochitskaya-ulitsa-35131292.html"
DR_REALTY = {
    "realty_id": 35131292, "realty_type_id": 2, "realty_type_name_uk": "квартира", "advert_type_id": 1,
    "advert_type_name_uk": "продаж", "price": 240000, "currency_type_id": 1, "currency_type_uk": "$",
    "priceArr": {"1": "240 000", "2": "205 000", "3": "11 052 000"},
    "total_square_meters": 74.7, "living_square_meters": 34.1, "kitchen_square_meters": 19.1,
    "rooms_count": 2, "floor": 22, "floors_count": 23,
    "state_name_uk": "Київська", "city_name_uk": "Київ", "district_name_uk": "Шевченківський",
    "street_name_uk": "Глибочицька вулиця", "building_number_str": "73", "user_newbuild_name_uk": "Podil Plaza",
    "description_uk": "Видова 2-кімнатна квартира з хамамом.<br>Телефонуйте: 000 000 00 00",
    "characteristics_values": {"118": 108, "209": 2, "214": 74.7, "216": 34.1, "218": 19.1, "227": 22, "228": 23,
                               "234": 240000, "242": 239, "443": 435, "505": 3.2, "516": 506, "1437": 1436,
                               "2014": "0", "99999": 5},
    "photos": {"348220824": {"file": "dom/photo/34822/3482208/348220824/348220824.jpg", "ordering": 2},
               "348220822": {"file": "dom/photo/34822/3482208/348220822/348220822.jpg", "ordering": 1}},
    "user_id": 777, "user": {"name": "Продавець", "phone": "000"}, "agency_id": 5,
}
DR_PAGE = """<html><head><title>DIM.RIA</title><meta property="og:image" content="https://cdn.riastatic.com/photosnew/dom/photo/x__1xl.jpg">
<script type="application/ld+json">{"@type":"Apartment","address":{"streetAddress":"Глибочицька вулиця, 73","addressLocality":"Київ"},
"floorSize":{"value":74.7},"numberOfRooms":2,"offers":{"price":240000,"priceCurrency":"USD"},"description":"ld опис"}</script></head>
<body><h1>Продаж 2к квартири 74.7 кв. м на вул. Глибочицька 73</h1>
<img src="https://cdn.riastatic.com/photosnew/dom/photo/prodaja-kvartira-kiev-shevchenkovskiy-glubochitskaya-ulitsa__348220822xl.jpg">
<img src="https://cdn.riastatic.com/photosnew/dom/photo/prodaja-kvartira-kiev-shevchenkovskiy-glubochitskaya-ulitsa__348220824xl.jpg">
<img src="https://cdn.riastatic.com/photosnew/dom/photo/prodaja-dom-irpen-tsentr__399999999xl.jpg">
<script>window.__INITIAL_STATE__ = @@STATE@@;</script></body></html>"""


@t("dom.ria: посилання, ID, українська сторінка")
def _():
    assert dr_is_ad(DR_URL) and dr_id(DR_URL) == "35131292"
    ru = DR_URL.replace("/uk/", "/ru/") + "?utm=x"
    assert dr_is_ad(ru) and uk_url(ru) == DR_URL
    assert uk_url(DR_URL.replace("/uk/", "/")) == DR_URL
    assert not dr_is_ad("https://dom.ria.com/prodazha-kvartir/kiev/") and not dr_is_ad(API_FLAT["url"])


@t("dom.ria: квартира з JSON — тип, адреса з вулицею, площі, ЖК, характеристики з довідника, ціна за м²")
def _():
    raw = {"url": DR_URL, "id": "35131292", "api": DR_REALTY, "dom": {"slug": "prodaja-kvartira-kiev-shevchenkovskiy-glubochitskaya-ulitsa", "photos": []}}
    m = normalize_domria(raw)
    assert m["kicker"] == "Продаж / Квартира", m["kicker"]
    assert row(m, "address") == "Київ, Шевченківський район, Глибочицька вулиця, 73", row(m, "address")
    assert row(m, "purpose") == "Житлова" and row(m, "zk") == "Podil Plaza"
    assert (row(m, "total_area"), row(m, "living_area"), row(m, "kitchen_area")) == ("74.7 м²", "34.1 м²", "19.1 м²")
    assert (row(m, "rooms"), row(m, "floor"), row(m, "total_floors"), row(m, "walls")) == ("2", "22", "23", "цегла")
    labels = {r["label"]: r["value"] for r in m["rows"]}
    assert labels.get("Рік побудови") == "1990-2000" and labels.get("Стан ремонту") == "дизайнерський ремонт", labels
    assert labels.get("Висота стелі") == "3.2", labels
    assert not any(k in labels for k in ("Тип пропозиції", "Комісія", "Ціна")), labels
    assert m["price"]["text"] == "240 000 $" and unit_price(m) == "3 213 $/м²", (m["price"], unit_price(m))
    assert quick_facts(m["rows"]) == ["74.7 м²", "2 кімн.", "22/23 пов."], quick_facts(m["rows"])
    assert m["description"] == "Видова 2-кімнатна квартира з хамамом.", repr(m["description"])
    assert m["photos"] == ["https://cdn.riastatic.com/photosnew/dom/photo/prodaja-kvartira-kiev-shevchenkovskiy-glubochitskaya-ulitsa__348220822xl.jpg",
                           "https://cdn.riastatic.com/photosnew/dom/photo/prodaja-kvartira-kiev-shevchenkovskiy-glubochitskaya-ulitsa__348220824xl.jpg"]


@t("dom.ria: сторінка з вбудованим JSON — знаходить оголошення, фото лише свої, без даних продавця")
def _():
    from domria_source import strip_private
    page = DR_PAGE.replace("@@STATE@@", json.dumps({"page": {"realty": DR_REALTY, "similar": [{"realty_id": 1, "x": 2}]}}, ensure_ascii=False))
    res = dr_parse(page, DR_URL, "35131292")
    assert res["embedded"] and res["embedded"]["realty_id"] == 35131292
    assert [p["id"] for p in res["dom"]["photos"]] == ["348220822", "348220824"], res["dom"]["photos"]
    emb = strip_private(res["embedded"])
    assert not any(k in emb for k in ("user_id", "user", "agency_id")), list(emb)
    m = normalize_domria({"url": DR_URL, "id": "35131292", "embedded": emb, **{k: v for k, v in res.items() if k != "embedded"}})
    assert m["kicker"] == "Продаж / Квартира" and len(m["photos"]) == 2


@t("dom.ria: без JSON — запасне з JSON-LD і розмітки (і примітка)")
def _():
    res = dr_parse(DR_PAGE.replace("@@STATE@@", "null"), DR_URL, "35131292")
    assert res["embedded"] is None
    m = normalize_domria({"url": DR_URL, "id": "35131292", **res})
    assert m["kicker"] == "Продаж / Квартира", m["kicker"]
    assert row(m, "address") == "Київ, Глибочицька вулиця, 73" and row(m, "total_area") == "74.7 м²", m["rows"]
    assert m["price"]["text"] == "240 000 $" and len(m["photos"]) == 2 and m["description"] == "ld опис"
    assert any("JSON оголошення не знайдено" in n for n in m["notes"])


@t("dom.ria: ділянка (цільове призначення, сотки, ціна за сотку) і оренда квартири (/ міс.)")
def _():
    land = normalize_domria({"api": {"realty_id": 1, "realty_type_id": 25, "advert_type_id": 1, "price": 30000, "currency_type_id": 1,
                                     "city_name_uk": "Лісники", "state_name_uk": "Київська",
                                     "characteristics_values": {"2099": 2083, "1917": 15, "226": 223, "1663": "3220882200:03:005:1306"}}})
    assert land["kicker"] == "Продаж / Ділянка під житлову забудову", land["kicker"]
    assert row(land, "purpose") == "під житлову забудову" and row(land, "land_area") == "15 сот."
    assert row(land, "address") == "Лісники, Київська обл."
    assert unit_price(land) == "2 000 $/сот." and any(r["label"] == "Кадастровий номер" for r in land["rows"])
    rent = normalize_domria({"api": {"realty_id": 2, "realty_type_id": 2, "advert_type_id": 3, "price": 25000, "currency_type_id": 3,
                                     "city_name_uk": "Київ", "total_square_meters": 50}})
    assert rent["kicker"] == "Оренда / Квартира" and rent["price"]["text"] == "25 000 грн / міс."

print(f"\nУсього тестів: {N}, усі пройшли.")
