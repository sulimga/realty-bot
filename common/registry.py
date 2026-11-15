"""
Реєстр «об'єкт CRM ↔ оголошення на сайтах» — основа для зміни цін, коли вони міняються в CRM.

Модель — граф (схема з фліпборду, 15.11.2026):
  синя вершина   — об'єкт CRM (таблиця objects), один на всіх користувачів;
  зелена вершина — користувач (Telegram ID; окремо не зберігається — у мультиботі це теки users/<id>);
  червона вершина — оголошення на сайті (таблиця listings): об'єкт + користувач + сайт + ID + акаунт.
Об'єкт «знає» своїх користувачів, а користувач — сайти та ID оголошень по кожному об'єкту: це одна таблиця
listings, яку читаємо в два боки (індекси за об'єктом і за користувачем). Дублювання немає, розійтись нічому.

Ключ об'єкта — з посилання, без запитів у мережу; посилання різних людей на той самий об'єкт (інший хвіст ?t=…)
дають той самий ключ:
  RealtSoft  https://crm-capital.realtsoft.net/estate-112633-<hash>.html?t=…  → crm-capital.realtsoft.net/112633
  LigaPro    http://120064.ligapro.ua/kvartiry_prodaga/ann-3451458.html?…    → 120064.ligapro.ua/3451458

Ціни: objects.price — остання відома ціна в CRM; listings.price — ціна, яку ми поставили на сайті.
Що оновлювати, визначається порівнянням нової ціни CRM з listings.price кожного оголошення (а не з objects.price),
тож свіжа публікація одного користувача не «ховає» зміну від інших.

Реєстр нічого не знає про сайти: board — назва з boards.json, listing_id — те, що повернула програма сайту.
Помилка реєстру не повинна ламати публікацію: диспетчер викликає record_run() в try.

Командний рядок (на сервері: cd /opt/realty-bot && .venv/bin/python common/registry.py …):
  stats [USER]          — скільки об'єктів і оголошень
  show <посилання|ключ> — об'єкт: хто й де його рекламує
  import                — підтягнути оголошення з journal.jsonl (робиться само при першому відкритті бази)
"""

import contextlib
import json
import os
import re
import sqlite3
import sys
import time
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("REALTY_DATA_DIR", os.path.join(ROOT, "data"))


def db_path():
    """Шлях до бази. REALTY_REGISTRY_DB — щоб у мультиботі всі користувачі мали ОДНУ спільну базу."""
    return os.environ.get("REALTY_REGISTRY_DB") or os.path.join(DATA, "registry.db")


def journal_path():
    return os.path.join(DATA, "journal.jsonl")


SCHEMA = """
CREATE TABLE IF NOT EXISTS objects(
  key        TEXT PRIMARY KEY,               -- crm-capital.realtsoft.net/112633
  source     TEXT,                           -- realtsoft | ligapro | other
  urls       TEXT NOT NULL DEFAULT '[]',     -- JSON: відомі публічні посилання (перше — основне для перевірки)
  article    TEXT,
  price      INTEGER,                        -- остання відома ціна в CRM
  currency   TEXT,                           -- USD | EUR | UAH
  mark       TEXT,                           -- мітка ?v= з презентації (для сторожа; ставить сторож)
  status     TEXT NOT NULL DEFAULT 'active', -- active | gone (зник з CRM)
  fails      INTEGER NOT NULL DEFAULT 0,     -- невдалих перевірок поспіль
  checked_at TEXT,
  changed_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listings(
  board      TEXT NOT NULL,                  -- domria | rieltor | olx | …
  listing_id TEXT NOT NULL,                  -- ID оголошення на сайті
  object_key TEXT NOT NULL REFERENCES objects(key),
  user_id    INTEGER,                        -- Telegram ID власника оголошення
  slot       INTEGER NOT NULL DEFAULT 1,     -- з якого входу (акаунта) викладено: 1 або 2
  via        TEXT,                           -- як викладено, якщо в сайту кілька шляхів (OLX: api | site)
  url        TEXT,
  draft      INTEGER NOT NULL DEFAULT 0,     -- чернетка / чекає оплати
  price      INTEGER,                        -- ціна, яку ми поставили на сайті (NULL — невідомо, напр. зі старого журналу)
  currency   TEXT,
  status     TEXT NOT NULL DEFAULT 'active', -- active | gone (зняте / видалене)
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(board, listing_id)
);
CREATE INDEX IF NOT EXISTS ix_listings_object ON listings(object_key, status);
CREATE INDEX IF NOT EXISTS ix_listings_user   ON listings(user_id, object_key);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

RS_RE = re.compile(r"^https?://([a-z0-9.-]+\.realtsoft\.net)/estate-(\d+)-", re.I)
LP_RE = re.compile(r"^https?://([a-z0-9.-]+\.ligapro\.ua)/.*?ann-(\d+)\.html", re.I)
CURRENCIES = (("$", "USD"), ("usd", "USD"), ("дол", "USD"), ("€", "EUR"), ("eur", "EUR"), ("євро", "EUR"),
              ("евро", "EUR"), ("₴", "UAH"), ("грн", "UAH"), ("uah", "UAH"))


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def object_key(url):
    """(ключ, джерело) з публічного посилання на об'єкт; (None, None) — не схоже на посилання."""
    u = (url or "").strip()
    m = RS_RE.match(u)
    if m:
        return f"{m.group(1).lower()}/{m.group(2)}", "realtsoft"
    m = LP_RE.match(u)
    if m:
        return f"{m.group(1).lower()}/{m.group(2)}", "ligapro"
    if re.match(r"^https?://", u, re.I):
        p = urlsplit(u)
        return f"{p.netloc.lower()}{p.path.rstrip('/')}", "other"
    return None, None


def parse_price(text):
    """«2 500 000 $» → (2500000, "USD"); «45 000 грн/міс» → (45000, "UAH"); невідомо → (None, None)."""
    if text is None or text == "":
        return None, None
    if isinstance(text, (int, float)):
        return int(text), None
    s = str(text).lower()
    m = re.search(r"\d[\d\s  .,]*", s)
    if not m:
        return None, None
    num = re.sub(r"[.,]\d{1,2}\s*$", "", m.group(0).strip())     # копійки/центи
    digits = re.sub(r"\D", "", num)
    cur = next((c for sym, c in CURRENCIES if sym in s), None)
    return (int(digits) if digits else None), cur


@contextlib.contextmanager
def connect(path=None, auto_import=True):
    """Відкрити базу (створює, якщо нема). При першому відкритті сама підтягує оголошення з journal.jsonl."""
    path = path or db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=15000")
        con.executescript(SCHEMA)
        if auto_import and not con.execute("SELECT 1 FROM meta WHERE k='journal_imported'").fetchone():
            import_journal(con)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def default_user():
    """Користувач за замовчуванням (тестовий бот / слот — один власник): OWNER_ID або DATA/owner.json."""
    env = os.environ.get("OWNER_ID", "").strip()
    if env.isdigit():
        return int(env)
    try:
        with open(os.path.join(DATA, "owner.json"), encoding="utf-8") as f:
            return int(json.load(f)["id"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _arg(argv, name):
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def upsert_object(con, url, article=None, price_text=None):
    """Синя вершина: знайти або створити об'єкт за посиланням. Повертає ключ або None."""
    key, source = object_key(url)
    if not key:
        return None
    amount, cur = parse_price(price_text)
    row = con.execute("SELECT urls FROM objects WHERE key=?", (key,)).fetchone()
    if row is None:
        con.execute("INSERT INTO objects(key, source, urls, article, price, currency, created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (key, source, json.dumps([url], ensure_ascii=False), article, amount, cur, now()))
        return key
    urls = json.loads(row["urls"] or "[]")
    if url not in urls:
        urls.append(url)
    sets = {"urls": json.dumps(urls, ensure_ascii=False), "status": "active", "fails": 0}
    if article:
        sets["article"] = article
    if amount is not None:                       # щойно прочитано з CRM — це найсвіжіша ціна
        sets.update(price=amount, currency=cur)
    con.execute(f"UPDATE objects SET {', '.join(k + '=?' for k in sets)} WHERE key=?", (*sets.values(), key))
    return key


def add_listing(con, key, board, listing_id, user_id, *, slot=1, via=None, url=None, draft=False,
                price=None, currency=None, ts=None):
    """Червона вершина: оголошення board/listing_id об'єкта key у користувача user_id."""
    ts = ts or now()
    con.execute("""
        INSERT INTO listings(board, listing_id, object_key, user_id, slot, via, url, draft, price, currency,
                             status, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?, 'active', ?, ?)
        ON CONFLICT(board, listing_id) DO UPDATE SET
          object_key=excluded.object_key, user_id=COALESCE(excluded.user_id, listings.user_id),
          slot=excluded.slot, via=COALESCE(excluded.via, listings.via), url=COALESCE(excluded.url, listings.url),
          draft=excluded.draft, price=COALESCE(excluded.price, listings.price),
          currency=COALESCE(excluded.currency, listings.currency), status='active', updated_at=excluded.updated_at
    """, (board, str(listing_id), key, user_id, int(slot or 1), via, url, int(bool(draft)), price, currency, ts, ts))


def _record(con, board, argv, result, user_id, ts=None, with_price=True):
    if not result or not result.get("ok") or not argv:
        return None
    cmd = argv[0]
    if cmd == "post":
        if "--dry-run" in argv or not result.get("id"):
            return None
        crm_url = _arg(argv, "--url")
        if not crm_url:
            return None
        price_text = result.get("crm_price") if with_price else None
        key = upsert_object(con, crm_url, article=result.get("article"), price_text=price_text)
        if not key:
            return None
        amount, cur = parse_price(price_text)
        add_listing(con, key, board, result["id"], user_id, slot=result.get("account_slot") or 1,
                    via=result.get("via"), url=result.get("url"), draft=result.get("draft"),
                    price=amount, currency=cur, ts=ts)
        return key
    if cmd == "techpassport":                # dom.ria: техпаспорт до чернетки → оголошення опубліковане
        rid = _arg(argv, "--realty-id") or result.get("id")
        if rid:
            con.execute("UPDATE listings SET draft=0, updated_at=? WHERE board=? AND listing_id=?",
                        (ts or now(), board, str(rid)))
    return None


def record_run(board, argv, result, user_id=None):
    """Викликає диспетчер після кожного запуску програми сайту. Повертає ключ об'єкта або None."""
    with connect() as con:
        return _record(con, board, list(argv or []), result, user_id if user_id else default_user())


def import_journal(con, user_id=None, path=None):
    """Оголошення, викладені до появи реєстру — з journal.jsonl (там є посилання CRM і ID оголошення).
    Ціни в журналі нема → price NULL; її поставить перша перевірка сторожа. Повертає кількість записів."""
    path = path or journal_path()
    user_id = user_id or default_user()
    n = 0
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                res = {"ok": e.get("ok"), "id": e.get("id"), "url": e.get("url"), "draft": e.get("draft")}
                if _record(con, e.get("board"), e.get("argv") or [], res, user_id, ts=e.get("ts"),
                           with_price=False):
                    n += 1
    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('journal_imported', ?)", (f"{now()} ({n})",))
    return n


# ------------------------------------------------------------------ читання графа в обидва боки
def object_users(con, key):
    """Об'єкт «знає», у кого він у рекламі."""
    return [r[0] for r in con.execute("SELECT DISTINCT user_id FROM listings WHERE object_key=? AND status='active' "
                                      "ORDER BY user_id", (key,))]


def user_object_listings(con, user_id, key):
    """Користувач «знає», на яких сайтах і з якими ID висить об'єкт."""
    return [dict(r) for r in con.execute("SELECT board, listing_id, slot, via, url, draft, price, currency "
                                         "FROM listings WHERE user_id=? AND object_key=? AND status='active' "
                                         "ORDER BY board", (user_id, key))]


def update_plan(con, key, new_price, new_currency=None):
    """Ціна об'єкта в CRM стала new_price → що міняти: {user_id: [оголошення]}.
    Не чіпаємо оголошення, де ціна вже така; оголошення з невідомою ціною (NULL) — окремо, у {"unknown": […]}."""
    plan, unknown = {}, []
    for r in con.execute("SELECT * FROM listings WHERE object_key=? AND status='active' ORDER BY user_id, board",
                         (key,)):
        d = dict(r)
        if d["price"] is None:
            unknown.append(d)
        elif d["price"] != new_price or (new_currency and d["currency"] and d["currency"] != new_currency):
            plan.setdefault(d["user_id"], []).append(d)
    return plan, unknown


def objects_to_check(con, limit=None):
    """Об'єкти, на яких висить хоч одне активне оголошення (лише їх перевіряє сторож), найдавніше перевірені першими."""
    q = ("SELECT o.* FROM objects o WHERE o.status='active' AND EXISTS "
         "(SELECT 1 FROM listings l WHERE l.object_key=o.key AND l.status='active') "
         "ORDER BY COALESCE(o.checked_at, '') ASC")
    if limit:
        q += f" LIMIT {int(limit)}"
    return [dict(r) for r in con.execute(q)]


def find_object(con, url_or_key):
    key = object_key(url_or_key)[0] if re.match(r"^https?://", url_or_key or "", re.I) else url_or_key
    r = con.execute("SELECT * FROM objects WHERE key=?", (key,)).fetchone()
    return dict(r) if r else None


def stats(con, user_id=None):
    w, p = ("WHERE status='active' AND user_id=?", (user_id,)) if user_id else ("WHERE status='active'", ())
    by_board = {r[0]: (r[1], r[2]) for r in con.execute(
        f"SELECT board, count(*), sum(draft) FROM listings {w} GROUP BY board ORDER BY board", p)}
    objs = con.execute(f"SELECT count(DISTINCT object_key) FROM listings {w}", p).fetchone()[0]
    no_price = con.execute(f"SELECT count(*) FROM listings {w} AND price IS NULL", p).fetchone()[0]
    shared = con.execute("SELECT count(*) FROM (SELECT object_key FROM listings WHERE status='active' "
                         "GROUP BY object_key HAVING count(DISTINCT user_id) > 1)").fetchone()[0]
    imp = con.execute("SELECT v FROM meta WHERE k='journal_imported'").fetchone()
    return {"objects": objs, "by_board": by_board, "no_price": no_price, "shared": shared,
            "journal_imported": imp[0] if imp else None}


def stats_text(user_id=None):
    with connect() as con:
        s = stats(con, user_id)
        recent = [dict(r) for r in con.execute(
            "SELECT o.article, o.price, o.currency, group_concat(l.board, ', ') AS boards FROM objects o "
            "JOIN listings l ON l.object_key=o.key AND l.status='active'" + (" AND l.user_id=?" if user_id else "") +
            " GROUP BY o.key ORDER BY max(l.created_at) DESC LIMIT 5", (user_id,) if user_id else ())]
    total = sum(n for n, _ in s["by_board"].values())
    lines = [f"Об'єктів: {s['objects']} · оголошень: {total}"]
    for b, (n, dr) in s["by_board"].items():
        lines.append(f"• {b}: {n}" + (f" (чернеток / чекають оплати: {dr})" if dr else ""))
    if s["no_price"]:
        lines.append(f"Без відомої ціни (зі старого журналу): {s['no_price']} — ціну поставить перша перевірка CRM")
    if recent:
        lines.append("\nОстанні:")
        for r in recent:
            sym = {"USD": "$", "EUR": "€", "UAH": "грн"}.get(r["currency"], r["currency"] or "")
            price = f"{r['price']:,}".replace(",", " ") + f" {sym}" if r["price"] else "ціна ?"
            lines.append(f"• {r['article'] or '—'} · {price.strip()} · {r['boards']}")
    if s["journal_imported"]:
        lines.append(f"\nІз журналу підтягнуто: {s['journal_imported']}")
    return "\n".join(lines)


def _main(argv):
    cmd = argv[0] if argv else "stats"
    if cmd == "stats":
        print(stats_text(int(argv[1]) if len(argv) > 1 else None))
    elif cmd == "show" and len(argv) > 1:
        with connect() as con:
            o = find_object(con, argv[1])
            if not o:
                print("Такого об'єкта в реєстрі нема")
                return 1
            print(json.dumps(o, ensure_ascii=False, indent=1))
            for u in object_users(con, o["key"]):
                print(f"користувач {u}:", json.dumps(user_object_listings(con, u, o["key"]), ensure_ascii=False))
    elif cmd == "import":
        with connect(auto_import=False) as con:
            print("підтягнуто з журналу:", import_journal(con))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
