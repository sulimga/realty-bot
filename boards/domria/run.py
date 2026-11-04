"""
dom.ria — точка входу для диспетчера (протокол: common/board_protocol.py).

    python run.py post --url <посилання CRM> [--techpassport файл/папка ...]
    python run.py techpassport --realty-id 35055093 файл/папка ...
    python run.py check

Сама логіка — у post_domria.py / mapping.py / domria_client.py (ті самі файли,
що працюють і з консолі: python post_domria.py post "<посилання>").
Дані (cookie, налаштування, логи, звіти) — у $REALTY_DATA_DIR/domria, $REALTY_DATA_DIR/secrets.
"""

import argparse
import json
import os
import re
import sys
import traceback
import types

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [HERE, os.path.join(ROOT, "crm"), os.path.join(ROOT, "common")]

import board_protocol as bp  # noqa: E402

bp.install_print_hook()

import domria_client  # noqa: E402
import post_domria as P  # noqa: E402

DATA = os.environ.get("REALTY_DATA_DIR", os.path.join(ROOT, "data"))
BOARD_DIR = os.path.join(DATA, "domria")
COOKIES = os.path.join(DATA, "secrets", "domria_cookies.txt")
os.makedirs(BOARD_DIR, exist_ok=True)

# усі файли програми — у теці даних, а не поруч з кодом
P.HERE = BOARD_DIR
P.POSTED_DIR = os.path.join(BOARD_DIR, "posted")
P.RUNS_DIR = os.path.join(BOARD_DIR, "runs")


class ProtoAsker:
    """Питання програми dom.ria -> у бот (замість консолі)."""
    enabled = True

    @staticmethod
    def _clean(prompt):
        return re.sub(r"\s*\(?Enter\s*—\s*([^)]*)\)?", r" (або «Пропустити» — \1)", prompt or "").strip()

    def text(self, prompt):
        return bp.ask_text(self._clean(prompt))

    def choose(self, options, prompt="ваш вибір", allow_text=False):
        return bp.ask_choice(self._clean(prompt), options, allow_text)


P.Asker = lambda enabled=True: ProtoAsker()

# запам'ятовуємо «красиве» посилання з відповіді saveRealty
_last = {"beautiful_url": None}
_orig_save = domria_client.DomRiaClient.save_realty


def _save_and_remember(self, payload, step="main_chars"):
    rid, body = _orig_save(self, payload, step)
    if isinstance(body, dict) and body.get("beautiful_url"):
        _last["beautiful_url"] = body["beautiful_url"]
    return rid, body


domria_client.DomRiaClient.save_realty = _save_and_remember


CONFIG_PATH = os.path.join(BOARD_DIR, "config.json")
ID_KEYS = ("userid", "user_id", "userId".lower())


def _find_user_id(obj):
    """Шукає в JSON відповіді dom.ria поле на кшталт userId/user_id з числом (ID акаунта)."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if str(k).lower().replace("-", "_") in ID_KEYS:
                    try:
                        if int(v) > 1000:
                            found.append(int(v))
                    except (TypeError, ValueError):
                        pass
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(obj)
    return found[0] if len(set(found)) == 1 else None


def _keys_outline(obj, depth=0):
    """Лише назви полів (без значень) — щоб без витоку даних побачити, як виглядає відповідь."""
    if isinstance(obj, dict) and depth < 2:
        return "{" + ", ".join(f"{k}: {_keys_outline(v, depth + 1)}" if isinstance(v, (dict, list)) else str(k)
                               for k in list(obj)[:15] for v in [obj[k]]) + "}"
    if isinstance(obj, list):
        return f"[{len(obj)}]"
    return "…"


def _save_user_id(uid):
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    cfg["user_id"] = int(uid)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def resolve_user_id(cfg):
    """
    ID акаунта dom.ria, від імені якого публікуємо (dom.ria записує оголошення на ЦЕЙ ID, а не на
    власника cookie). Порядок: з сесії (cookie) → з config.json цього користувача → питаємо один раз.
    Якщо ID із сесії і з налаштувань різні — зупиняємось: інакше оголошення піде на чужий акаунт.
    """
    configured = cfg.get("user_id")
    session_id, outline = None, ""
    try:
        bal = domria_client.DomRiaClient(P.load_cookies(cfg), 0, log_dir=os.path.join(BOARD_DIR, "logs")).check_auth()
        session_id = _find_user_id(bal)
        outline = _keys_outline(bal)
    except domria_client.AuthError:
        raise
    except Exception:  # noqa: BLE001 — визначення з сесії не обов'язкове
        pass
    if session_id:
        if configured and int(configured) != session_id:
            raise domria_client.DomRiaError(
                f"cookie належать акаунту dom.ria № {session_id}, а в налаштуваннях записано № {configured}. "
                f"Щоб не викласти на чужий акаунт — зупиняюсь. Якщо cookie правильні, видаліть user_id "
                f"з {CONFIG_PATH} (або скажіть адміністратору).")
        if not configured:
            _save_user_id(session_id)
            bp.info(f"dom.ria: акаунт № {session_id} (визначено з cookie, запам'ятав)")
        cfg["user_id"] = session_id
        return cfg
    if configured:
        return cfg
    if outline:
        print(f"[dom.ria] у відповіді балансу немає ID акаунта; поля: {outline}", file=sys.stderr)
    ans = bp.ask_text("dom.ria: номер (ID) вашого акаунта dom.ria — від його імені будуть оголошення. "
                      "Лише цифри. Він є в кабінеті dom.ria (профіль). Питаю один раз.")
    uid = re.sub(r"\D", "", ans or "")
    if not uid or int(uid) < 1000:
        raise domria_client.DomRiaError("без ID акаунта dom.ria не публікую — інакше оголошення може "
                                        "потрапити на чужий акаунт. Спробуйте ще раз і вкажіть ID.")
    _save_user_id(uid)
    cfg["user_id"] = int(uid)
    bp.info(f"dom.ria: запам'ятав акаунт № {uid}")
    return cfg


def load_cfg():
    if not os.path.exists(COOKIES) or not open(COOKIES, encoding="utf-8").read().strip():
        raise domria_client.DomRiaError("немає cookie. Додайте їх: кнопка «🍪 Cookies» → dom.ria")
    cfg = P.load_config()
    cfg["cookies_file"] = COOKIES
    return resolve_user_id(cfg)


def finish_error(e):
    if isinstance(e, domria_client.AuthError):
        bp.result(False, f"dom.ria: cookie застаріли ({e}). Оновіть їх: кнопка «🍪 Cookies» → dom.ria.")
    elif isinstance(e, requests.exceptions.RequestException):
        bp.result(False, f"dom.ria не відповідає ({type(e).__name__}). Спробуйте ще раз за кілька хвилин. "
                         f"Якщо чернетку вже створено — вона в кабінеті dom.ria, повторна спроба створить нову.")
    elif isinstance(e, (domria_client.DomRiaError, SystemExit)):
        bp.result(False, f"dom.ria: {e}")
    else:
        bp.info(traceback.format_exc()[-1500:])
        bp.result(False, f"dom.ria: непередбачена помилка {type(e).__name__}: {e}")


def cmd_post(a):
    cfg = load_cfg()
    args = types.SimpleNamespace(source=a.url, techpassport=a.techpassport or None, draft=False,
                                 dry_run=False, street=None, house=None, no_ask=False)
    report = {"mode": "publish" if a.techpassport else "draft", "via": "bot"}
    try:
        P._cmd_post(args, cfg, report)
    finally:
        try:
            P.write_run_report(report)
        except Exception:
            pass
    rid = report.get("realty_id")
    url = f"https://dom.ria.com/uk/{_last['beautiful_url']}" if _last["beautiful_url"] else None
    if a.techpassport:
        bp.result(True, f"dom.ria: опубліковано (№ {rid}), техпаспорт на перевірці", url=url, id=rid)
    else:
        bp.result(True, f"dom.ria: чернетку створено (№ {rid}). Опублікувати: додайте техпаспорт "
                        f"(кнопка «📎 Техпаспорт до чернетки») або в кабінеті dom.ria",
                  url="https://dom.ria.com/v/uk/mypage", id=rid, draft=True)


def cmd_techpassport(a):
    cfg = load_cfg()
    client = P.make_client(cfg)
    client.check_auth()
    P.add_techpassport(client, int(a.realty_id), a.files, publish=True)
    bp.result(True, f"dom.ria: техпаспорт додано, оголошення № {a.realty_id} опубліковано", id=a.realty_id)


def cmd_check(a):
    cfg = load_cfg()
    bal = P.make_client(cfg).check_auth()
    bp.result(True, f"dom.ria: cookie робочі (баланс: {bal})")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("post")
    p.add_argument("--url", required=True)
    p.add_argument("--techpassport", nargs="*", default=[])
    t = sub.add_parser("techpassport")
    t.add_argument("--realty-id", required=True)
    t.add_argument("files", nargs="+")
    sub.add_parser("check")
    a = ap.parse_args()
    try:
        {"post": cmd_post, "techpassport": cmd_techpassport, "check": cmd_check}[a.cmd](a)
    except BaseException as e:  # noqa: BLE001 — будь-яка помилка має дійти до бота як результат
        finish_error(e)


if __name__ == "__main__":
    main()
