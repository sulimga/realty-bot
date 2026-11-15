"""
Тест реєстру «об'єкт CRM ↔ оголошення» без мережі:  python3 common/test_registry.py
Сценарій — фліпборд 15.11.2026: об'єкт 1 у user1 (rieltor) і user2 (OLX, rieltor, dom.ria);
об'єкт 2 у user2 (OLX) і user3 (OLX, rieltor); user4 без цих об'єктів.
Публікації йдуть через справжній диспетчер (dispatcher.run) з підставною програмою сайту.
Також: акаунти (два входи на сайт), очищення реєстру, старий журнал не підтягується сам, перехід старої бази на нову схему.
"""

import asyncio
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="registry_test_")
os.environ["REALTY_DATA_DIR"] = TMP
os.environ.pop("REALTY_REGISTRY_DB", None)
os.environ["OWNER_ID"] = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "dispatcher")]
import dispatcher as D  # noqa: E402

R = D.registry()
FAILS = []


def check(cond, what):
    print(("✅ " if cond else "❌ ") + what)
    if not cond:
        FAILS.append(what)


# --- ключ об'єкта і ціна з тексту CRM
U1 = "https://crm-capital.realtsoft.net/estate-100001-0123456789abcdef0123456789abcdef.html?t=zTEST1"
U1_COLLEAGUE = "https://crm-capital.realtsoft.net/estate-100001-0123456789abcdef0123456789abcdef.html?t=zTEST2"
U2 = "https://crm-capital.realtsoft.net/estate-100002-fedcba9876543210fedcba9876543210.html?t=zTEST3"
LP = "http://120064.ligapro.ua/kvartiry_prodaga/ann-1000003.html?agents_conts_id=1"
check(R.object_key(U1) == ("crm-capital.realtsoft.net/100001", "realtsoft"), "ключ RealtSoft")
check(R.object_key(U1_COLLEAGUE)[0] == R.object_key(U1)[0], "посилання колеги (інший ?t=) → той самий об'єкт")
check(R.object_key(LP) == ("120064.ligapro.ua/1000003", "ligapro"), "ключ LigaPro")
for txt, exp in (("2 500 000 $", (2500000, "USD")), ("4 500 $", (4500, "USD")), ("45 000 грн", (45000, "UAH")),
                 ("120 000 €", (120000, "EUR")), ("226 000 $", (226000, "USD")), (None, (None, None))):
    check(R.parse_price(txt) == exp, f"ціна «{txt}» → {exp}")

# --- старий журнал (до реєстру): сам НЕ підтягується (рішення 15.11.2026), лише вручну
with open(os.path.join(TMP, "journal.jsonl"), "w", encoding="utf-8") as f:
    for e in (
        {"ts": "2026-11-01 10:00:00", "board": "rieltor", "argv": ["post", "--url", LP, "--no-ask"], "ok": True,
         "id": "OLD-1", "url": None, "draft": False},
        {"ts": "2026-11-01 10:05:00", "board": "domria", "argv": ["post", "--url", LP], "ok": True,
         "id": "OLD-2", "url": "https://dom.ria.com/v/uk/mypage", "draft": True},
        {"ts": "2026-11-01 10:06:00", "board": "domria", "argv": ["techpassport", "--realty-id", "OLD-2", "a.pdf"],
         "ok": True, "id": "OLD-2", "draft": False},
        {"ts": "2026-11-01 10:07:00", "board": "olx", "argv": ["post", "--url", LP], "ok": False, "id": None},
        {"ts": "2026-11-01 10:08:00", "board": "presentation", "argv": ["make", "--url", "https://olx.ua/x"],
         "ok": True, "id": None},
        {"ts": "2026-11-01 10:09:00", "board": "rieltor", "argv": ["keepalive"], "ok": True}):
        f.write(json.dumps(e, ensure_ascii=False) + "\n")

# --- підставна програма сайту: друкує result з тим, що їй передали в --id/--price
FAKE_DIR = os.path.join(TMP, "fakeboard")
os.makedirs(FAKE_DIR)
with open(os.path.join(FAKE_DIR, "run.py"), "w", encoding="utf-8") as f:
    f.write("import json, sys\n"
            "a = sys.argv[1:]\n"
            "g = lambda n: a[a.index(n) + 1] if n in a else None\n"
            "res = {'type': 'result', 'ok': g('--fail') is None, 'text': 'ok', 'id': g('--id'), 'url': None,\n"
            "       'draft': '--draft' in a, 'crm_price': g('--price'), 'article': g('--art'), 'via': g('--via'),\n"
            "       'account': g('--acc'), 'account_slot': int(g('--slot') or 1)}\n"
            "print(json.dumps(res, ensure_ascii=False))\n")
boards = {k: {"title": k, "dir": FAKE_DIR, "entry": "run.py", "enabled": True, "timeout_min": 1}
          for k in ("olx", "rieltor", "domria")}
D.REGISTRY = os.path.join(TMP, "boards.json")
with open(D.REGISTRY, "w", encoding="utf-8") as f:
    json.dump(boards, f)


async def _noop(*_):
    return {}


async def post(board, user, url, lid, price="225 000 $", *extra):
    return await D.run(board, ["post", "--url", url, "--id", lid, "--price", price, "--art", "#A00001", *extra],
                       _noop, _noop, user=user)


async def main():
    await post("rieltor", 1, U1, "R-1")
    await post("olx", 2, U1_COLLEAGUE, "O-2", "225 000 $", "--via", "api")
    await post("rieltor", 2, U1_COLLEAGUE, "R-2", "225 000 $", "--acc", "acc-A", "--slot", "1")
    await post("domria", 2, U1_COLLEAGUE, "D-2", "225 000 $", "--draft", "--acc", "777001")
    await post("olx", 2, U2, "O-3", "2 500 000 $")
    await post("olx", 3, U2, "O-4", "2 500 000 $")
    await post("rieltor", 3, U2, "R-4", "2 500 000 $")
    await post("rieltor", 2, U2, "R-5", "2 500 000 $", "--acc", "acc-B", "--slot", "2")   # другий акаunt user2
    await post("rieltor", 4, U2, "X-dry", "2 500 000 $", "--dry-run")     # перевірка без відправки — не пишемо
    await post("rieltor", 4, U2, "X-fail", "2 500 000 $", "--fail", "1")  # невдала публікація — не пишемо
    await D.run("domria", ["techpassport", "--realty-id", "D-2", "--id", "D-2"], _noop, _noop, user=2)

asyncio.run(main())

K1, K2 = R.object_key(U1)[0], R.object_key(U2)[0]
with R.connect() as con:
    check(R.object_users(con, K1) == [1, 2], "об'єкт 1 знає своїх: user1, user2")
    check(R.object_users(con, K2) == [2, 3], "об'єкт 2 знає своїх: user2, user3")
    u2 = {(x["board"], x["listing_id"]) for x in R.user_object_listings(con, 2, K1)}
    check(u2 == {("olx", "O-2"), ("rieltor", "R-2"), ("domria", "D-2")}, "user2 знає свої оголошення по об'єкту 1")
    check([x["listing_id"] for x in R.user_object_listings(con, 2, K2)] == ["O-3", "R-5"],
          "user2 по об'єкту 2 — OLX і rieltor з другого акаунта")
    o1 = R.find_object(con, U1_COLLEAGUE)
    check(o1 and len(json.loads(o1["urls"])) == 2, "об'єкт 1 пам'ятає обидва посилання (моє й колеги)")
    check(con.execute("SELECT draft FROM listings WHERE listing_id='D-2'").fetchone()[0] == 0,
          "dom.ria: після техпаспорта чернетка → опубліковане")
    check(con.execute("SELECT via FROM listings WHERE listing_id='O-2'").fetchone()[0] == "api", "OLX: через API")
    check(con.execute("SELECT count(*) FROM listings WHERE user_id=4").fetchone()[0] == 0,
          "user4: dry-run і невдала публікація не записані")

    plan, unknown = R.update_plan(con, K1, 220000, "USD")
    check({u: sorted(x["board"] for x in v) for u, v in plan.items()} == {1: ["rieltor"],
                                                                          2: ["domria", "olx", "rieltor"]},
          "ціна об'єкта 1 змінилась → user1: rieltor; user2: OLX, rieltor, dom.ria")
    plan, _ = R.update_plan(con, K2, 2500000, "USD")
    check(plan == {}, "ціна та сама → нічого не міняємо")
    con.execute("UPDATE listings SET price=2400000 WHERE listing_id='O-4'")
    plan, _ = R.update_plan(con, K2, 2400000, "USD")
    check({u: sorted(x["listing_id"] for x in v) for u, v in plan.items()} == {2: ["O-3", "R-5"], 3: ["R-4"]},
          "де ціна вже нова (O-4) — запиту не буде")
    accs = {x["listing_id"]: (x["account"], x["slot"]) for x in R.user_object_listings(con, 2, K1)
            + R.user_object_listings(con, 2, K2)}
    check(accs["R-2"] == ("acc-A", 1) and accs["R-5"] == ("acc-B", 2) and accs["D-2"] == ("777001", 1),
          "два акаунти rieltor у user2 записані окремо (акаунт + слот), dom.ria — з ID акаунта")
    check(R.object_users(con, R.object_key(LP)[0]) == [], "старий журнал сам не підтягнувся")
    owners = dict(con.execute("SELECT listing_id, user_id FROM listings").fetchall())
    print("   підтягнуто вручну:", R.import_journal(con))
    check(all(con.execute("SELECT user_id FROM listings WHERE listing_id=?", (k,)).fetchone()[0] == v
              for k, v in owners.items()), "ручний імпорт журналу не перезаписав власників відомих оголошень")

    kl = R.object_key(LP)[0]
    imported = {r[0]: r[1] for r in con.execute("SELECT listing_id, draft FROM listings WHERE object_key=?", (kl,))}
    check(imported == {"OLD-1": 0, "OLD-2": 0}, "старий журнал: 2 оголошення, dom.ria вже опубліковане")
    check(con.execute("SELECT user_id FROM listings WHERE listing_id='OLD-1'").fetchone()[0] == 1,
          "старі записи — на власника бота")
    _, unknown = R.update_plan(con, kl, 50000)
    check(len(unknown) == 2, "у старих записах ціни нема → окремо («невідомо»), не міняємо наосліп")
    check([o["key"] for o in R.objects_to_check(con)].count(K1) == 1 and len(R.objects_to_check(con)) == 3,
          "сторожу перевіряти 3 об'єкти (не 9 оголошень)")

print("\n" + R.stats_text(2))
check("акаунт acc-A: 1" in R.stats_text(2) and "акаунт acc-B: 1" in R.stats_text(2), "/registry показує розбивку по акаунтах")

before = R.count_all()
n = R.reset_all()
check(n == before == 10 and R.count_all() == 0 and "Поки порожньо" in R.stats_text(), f"очищення реєстру (було {n})")
check("Із журналу" not in R.stats_text(), "після очищення — без рядка про журнал")

# --- база, створена до появи колонки account, відкривається й отримує колонку
import sqlite3  # noqa: E402
old_db = os.path.join(TMP, "old.db")
c = sqlite3.connect(old_db)
c.executescript(R.SCHEMA.replace("  account    TEXT,", "").replace(
    "  account    TEXT,                           -- ID акаунта на сайті (dom.ria user_id тощо) — надійніше за слот\n", ""))
c.close()
with R.connect(old_db) as con:
    cols = {r[1] for r in con.execute("PRAGMA table_info(listings)")}
check("account" in cols, "стара база: колонку account додано")
print("\n" + ("УСЕ ДОБРЕ" if not FAILS else f"ПОМИЛОК: {len(FAILS)}"))
sys.exit(1 if FAILS else 0)
