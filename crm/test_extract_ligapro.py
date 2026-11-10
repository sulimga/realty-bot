"""
Тести парсера LigaPro без мережі: знімки сторінок у tests/ligapro/ (контакти вирізано).
Перевіряє і сам розбір, і те, що результат правильно розуміють програми сайтів
(dom.ria, rieltor.ua, OLX) — тип угоди/об'єкта, місто, район, вулиця, будинок, площі.

    python3 crm/test_extract_ligapro.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from extract_ligapro import (  # noqa: E402
    decode_html, extract_ligapro_data, is_ligapro_url, parse_address, address_to_crm,
)

BASE = "http://120064.ligapro.ua"
CASES = {
    "flat_sale": "/kvartiry_prodaga/ann-3451458.html?agents_conts_id=1",
    "house_sale": "/doma_dachi_prodaga/ann-472253-0.html",
    "commercial_rent": "/kommerch_arenda/ann-131629-0.html",
    "land_sale": "/zem_uchastki_prodaga/ann-168514-0.html",
    "office_sale": "/officy_prodaga/ann-12583-0.html",
    "garage_sale": "/garagy_prodaga/ann-6371-0.html",
}


def load(name):
    with open(os.path.join(HERE, "tests", "ligapro", name + ".html"), encoding="utf-8") as f:
        return extract_ligapro_data(f.read(), url=BASE + CASES[name])


def test_urls():
    assert is_ligapro_url("http://120064.ligapro.ua/kvartiry_prodaga/ann-3451458.html?agents_conts_id=1")
    assert is_ligapro_url("https://ligapro.ua/x")
    assert not is_ligapro_url("https://crm-capital.realtsoft.net/estate-1.html?t=x")
    assert not is_ligapro_url("https://evil.com/?u=ligapro.ua")


def test_decode():
    html = "<html><head><meta http-equiv='Content-Type' content='text/html; charset=windows-1251'></head>Київ</html>"
    assert "Київ" in decode_html(html.encode("cp1251"), "text/html; charset=windows-1251")
    assert "Київ" in decode_html(html.encode("cp1251"))                 # з мета-тегу
    assert "Київ" in decode_html("<p>Київ</p>".encode("utf-8"))        # без кодування — utf-8
    assert "Київ" in decode_html("<p>Київ</p>".encode("cp1251"))       # без кодування — cp1251


def test_addresses():
    cases = {
        "Зарічна вул., 2к.1, Київ, Дарницький, м. Славутич":
            "Київ, Дарницький район, вул. Зарічна, 2",
        "Адреса: Садова, Київ, Дарницький, Осокорки, м. Осокорки":
            "Київ, Дарницький район, Садова",
        "Київ, Дніпровський, м. Лівобережна": "Київ, Дніпровський район",
        "Сергія Колоса вул. (Леніна), 105, Київ, Солом'янський, Жуляни":
            "Київ, Солом'янський район, вул. Сергія Колоса, 105",
        "Миколи Міхновського бульв. (Дружби Народів), 2, Київ, Голосіївський, м. Либідська":
            "Київ, Голосіївський район, бульв. Миколи Міхновського, 2",
        "Євгена Сверстюка вул. (Марини Раскової), б.3, Київ, Дніпровський":
            "Київ, Дніпровський район, вул. Євгена Сверстюка, 3",
        "Глибочицька вул., 13/7, Київ, Шевченківський, Лук'янівка, м. Лук'янівська":
            "Київ, Шевченківський район, вул. Глибочицька, 13/7",
        "Академіка Заболотного вул., 15г, Київ, Голосіївський":
            "Київ, Голосіївський район, вул. Академіка Заболотного, 15г",
        "Дніпровська набережна, 19, Київ, Дніпровський": "Київ, Дніпровський район, Дніпровська набережна, 19",
        "Олени Теліги вул., 49, Київ": "Київ, вул. Олени Теліги, 49",
        "Відрадний просп., Київ, Солом'янський, Відрадний": "Київ, Солом'янський район, просп. Відрадний",
        "Землянський пров., Київ, Печерський, Печерськ, м. Печерська": "Київ, Печерський район, пров. Землянський",
        # область (зразків з сайту ще нема — припущення формату)
        "Шевченка вул., 5, Гатне, Києво-Святошинський": "Гатне, Києво-Святошинський район, вул. Шевченка, 5",
        "Гатне, Києво-Святошинський": "Гатне, Києво-Святошинський район",
    }
    for raw, want in cases.items():
        got = address_to_crm(parse_address(raw))
        assert got == want, f"{raw!r}: {got!r} != {want!r}"


def test_flat():
    d = load("flat_sale")
    assert d["estate_id"] is None                       # не RealtSoft — адмінку не чіпати
    assert d["deal_title"] == "Продаж / Квартира"
    assert d["address"] == "Київ, Дарницький район, вул. Зарічна, 2", d["address"]
    assert d["article"] == "213-451-458" and d["ligapro_id"] == "213451458"
    assert d["price"] == "129 000 $"
    p = d["parameters"]
    assert p["Кількість кімнат"] == "2" and p["Поверх"] == "2" and p["Поверховість"] == "26"
    assert (p["Площа загальна,м²"], p["Площа житлова,м²"], p["Площа кухні,м²"]) == ("72", "32", "25")
    assert p["Комплекс"] == "Славутич"
    assert d["photos_count"] == 4
    assert d["photos"][2].endswith("/90c29_00475b19612767b01c52c257b1cdf27b.jpg")   # data-image, не мініатюра
    assert d["description"].startswith("Продаж 3-кімнатної") and "\nДарницький район" in d["description"]
    assert "72 м²" in d["description"] and "<br" not in d["description"]
    assert "lat" not in d and "lng" not in d             # координати не беремо


def test_house():
    d = load("house_sale")
    assert d["deal_title"] == "Продаж / Дуплекс"
    assert d["address"] == "Київ, Дарницький район, Садова"
    assert d["price"] == "8 178 804 грн"
    p = d["parameters"]
    assert p == {"Поверховість": "1", "Кількість спалень": "2", "Площа загальна,м²": "70",
                 "Площа ділянки,соток": "5", "Кількість кімнат": "3"}, p   # кімнати — з кінця опису


def test_commercial_rent():
    d = load("commercial_rent")
    assert d["deal_title"] == "Оренда / Торгова площа"
    assert d["address"] == "Київ, вул. Олени Теліги, 49"
    assert d["price"] == "44 000 грн"
    assert d["parameters"]["Комунальні"] == "окремо"
    assert d["parameters"]["Площа загальна,м²"] == "37"


def test_land_office_garage():
    land = load("land_sale")
    assert land["deal_title"] == "Продаж / Земельна ділянка"
    assert land["parameters"]["Площа ділянки,соток"] == "6"
    assert "Площа загальна,м²" not in land["parameters"]
    office = load("office_sale")
    assert office["deal_title"] == "Продаж / Офіс"
    assert office["address"] == "Київ, Подільський район, вул. Воздвиженська, 31а"
    assert office["parameters"]["Кількість приміщень"] == "18"
    garage = load("garage_sale")
    assert garage["deal_title"] == "Продаж / Паркомісце"
    assert garage["address"] == "Київ, Печерський район, вул. Іоанна Павла ІІ, 6/1"
    assert garage["photos"][1].endswith("/d0d6d0d97e82b604005ba91c543980b9.jpg")   # без data-image — з мініатюри


def test_section_from_gallery():
    """Розділу в url нема (напр. збережений файл) — тип з шляху фото."""
    with open(os.path.join(HERE, "tests", "ligapro", "flat_sale.html"), encoding="utf-8") as f:
        d = extract_ligapro_data(f.read(), url="")
    assert d["deal_title"] == "Продаж / Квартира"


def test_boards_understand_it():
    """Результат читають програми сайтів так само, як дані RealtSoft."""
    sys.path[:0] = [os.path.join(ROOT, "boards", "domria"), os.path.join(ROOT, "boards", "olx"),
                    os.path.join(ROOT, "boards", "rieltor")]
    import post_domria as PD
    import mapping as DM                                  # dom.ria
    sys.modules.pop("mapping")
    sys.path.remove(os.path.join(ROOT, "boards", "domria"))
    import importlib
    OM = importlib.import_module("mapping")                # OLX
    import crm_to_rieltor_payload as RP

    flat = load("flat_sale")
    assert PD.split_address(flat["address"]) == ("Київ", "вул. Зарічна", "2")
    assert DM.detect_deal(flat["deal_title"]) == (1, 2)
    assert DM.parse_price(flat["price"]) == (129000, "$")
    assert RP.parse_deal_title(flat["deal_title"]) == (1, 1)
    a = RP.parse_address(flat["address"])
    assert (a["city"], a["district"], a["street"], a["house"]) == ("Київ", "Дарницький", "вул. Зарічна", "2")
    assert OM.city_and_district(flat["address"]) == ("Київ", "Дарницький")
    assert OM.street(flat["address"]) == "вул. Зарічна"
    det = OM.detect(flat)
    assert det["oper"] and det["family"] and not det["unsupported"], det

    house = load("house_sale")
    assert DM.detect_deal(house["deal_title"]) == (1, 8)               # дуплекс
    assert RP.parse_deal_title(house["deal_title"]) == (1, 3)
    assert PD.split_address(house["address"])[0] == "Київ"
    assert OM.detect(house)["family"] and not OM.detect(house)["unsupported"]

    comm = load("commercial_rent")
    assert DM.detect_deal(comm["deal_title"]) == (3, 14)               # оренда, магазин/торгова
    assert RP.parse_deal_title(comm["deal_title"]) == (2, 4)
    assert DM.parse_price(comm["price"]) == (44000, "грн")
    d = OM.detect(comm)
    assert d["family"] and not d["unsupported"], d
    assert OM.commercial_category(d["kind"], d["oper"]) == 3692          # OLX: оренда торгових

    land = load("land_sale")
    assert RP.parse_deal_title(land["deal_title"]) == (1, 5)
    assert DM.detect_deal(land["deal_title"])[1] == 25
    assert OM.detect(land)["unsupported"] == "земля"

    office = load("office_sale")
    assert DM.detect_deal(office["deal_title"]) == (1, 11)
    assert RP.parse_deal_title(office["deal_title"]) == (1, 4)

    garage = load("garage_sale")
    assert RP.parse_deal_title(garage["deal_title"]) == (1, 6)
    assert DM.detect_deal(garage["deal_title"])[1] == 34


def test_dispatch():
    """fetch_estate_data віддає LigaPro-посилання новому парсеру (без мережі — підміняємо requests)."""
    import extract_estate
    import extract_ligapro
    seen = {}

    class R:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=windows-1251"}

        def __init__(self, url):
            seen["url"] = url
            with open(os.path.join(HERE, "tests", "ligapro", "flat_sale.html"), encoding="utf-8") as f:
                self.content = f.read().encode("cp1251", errors="replace")

        def raise_for_status(self):
            pass

    orig = extract_ligapro.requests.get
    extract_ligapro.requests.get = lambda url, **kw: R(url)
    try:
        d = extract_estate.fetch_estate_data(BASE + CASES["flat_sale"])
    finally:
        extract_ligapro.requests.get = orig
    assert seen["url"].endswith("ann-3451458.html?agents_conts_id=1")
    assert d["source"] == "ligapro" and d["source_url"] == BASE + CASES["flat_sale"]
    assert d["address"] == "Київ, Дарницький район, вул. Зарічна, 2"


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"✓ {name}")
    print(f"OK: {n} тестів")
