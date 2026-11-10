"""
Презентація для клієнта з оголошення OLX — точка входу для диспетчера (протокол: common/board_protocol.py).

    python run.py make --url <посилання на оголошення OLX> [--no-ask]

Що робить: читає оголошення (olx_source.py), складає презентацію як у CRM (normalize.py), питає в боті
ціну й підпис ріелтора, публікує сторінку на telegra.ph (telegraph.py) і збирає PDF (render_pdf.py).
Результат: {"url": посилання Telegraph, "files": [{"path", "name"}]} — бот надсилає PDF документом.
Це не «дошка» для публікації: у boards.json має "kind": "tool" (не видно у виборі сайтів і в «Вхід»).

Дані: $REALTY_DATA_DIR/presentation/{config.json (підпис), telegraph.json (токен), out/*.pdf, runs/*.json}
Те саме вміє розширення Chrome extension/olx-presentation (там ще правки на сторінці) — логіку тримати однаковою.
"""

import argparse
import glob
import json
import os
import re
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [HERE, os.path.join(ROOT, "common")]

import board_protocol as bp  # noqa: E402

bp.install_print_hook()

from normalize import CUR_SYMBOL, fmt_num, normalize_olx, parse_price, presentation_title, unit_price  # noqa: E402
import olx_source  # noqa: E402

DATA = os.environ.get("REALTY_DATA_DIR", os.path.join(ROOT, "data"))
DIR = os.path.join(DATA, "presentation")
CONFIG = os.path.join(DIR, "config.json")
KEEP_FILES = 30


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(DIR, exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CONFIG)


def prune(pattern, keep=KEEP_FILES):
    files = sorted(glob.glob(pattern))
    for p in files[:-keep]:
        try:
            os.remove(p)
        except OSError:
            pass


def parse_agent(text):
    """«Олег Іваненко, +380 00 000 00 00» -> {"name", "phone"}."""
    t = str(text or "").strip()
    m = re.search(r"\+?\d[\d\s()\-]{7,}\d", t)
    phone = m.group(0).strip() if m else ""
    name = (t.replace(phone, "") if phone else t).strip(" ,;·-–\t")
    return {"name": name, "phone": phone}


def agent_text(agent):
    return " · ".join(x for x in ((agent or {}).get("name"), (agent or {}).get("phone")) if x) or "без підпису"


def ask_agent():
    kind, v = bp.ask_choice("Підпис унизу презентації: ваше ім'я і телефон одним повідомленням "
                            "(напр. «Олег Іваненко, +380 00 000 00 00»). Запам'ятаю для наступних презентацій.",
                            [("none", "Без підпису")], allow_text=True)
    return parse_agent(v) if kind == "text" else {"name": "", "phone": ""}


def new_price(old_text, typed):
    """«99000» при старій ціні в $ -> «99 000 $»; якщо валюту вказано — як написали."""
    typed = typed.strip()
    val, cur = parse_price(typed)
    if val and not cur:
        _, old_cur = parse_price(old_text)
        if old_cur:
            return f"{fmt_num(val)} {CUR_SYMBOL.get(old_cur, old_cur)}"
    return typed


def review(model, agent, cfg):
    """Зведення + що змінити. Повертає agent (можливо, новий)."""
    while True:
        p = model["price"]
        up = unit_price(model)
        price_line = (f"{p['text']}" + (f" ({up})" if up else "")) if p["show"] and p["text"] else "не показувати"
        text = (f"{presentation_title(model)}\n"
                f"Ціна: {price_line}\n"
                f"Фото: {len(model['photos'])}\n"
                f"Підпис: {agent_text(agent)}")
        opts = [("go", "✅ Зробити презентацію"), ("price", "💲 Змінити ціну"),
                ("toggle", "🚫 Без ціни" if p["show"] else "💲 Показати ціну"), ("agent", "✍️ Змінити підпис")]
        kind, v = bp.ask_choice(text, opts)
        if kind is None or v == "go":
            return agent
        if v == "price":
            t = bp.ask_text(f"Ціна для презентації (зараз «{p['text'] or '—'}»). Напр.: 99 000 $")
            if t:
                p["text"] = new_price(p["text"], t)
                p["show"] = True
        elif v == "toggle":
            p["show"] = not p["show"]
        elif v == "agent":
            agent = ask_agent()
            cfg["agent"] = agent
            save_config(cfg)


def file_name(model):
    name = re.sub(r'[\\/:*?"<>|]+', " ", presentation_title(model)).replace("—", "-")
    return re.sub(r"\s+", " ", name).strip()[:100] + ".pdf"


def cmd_make(a):
    url = (a.url or "").strip()
    if not olx_source.is_ad_url(url):
        return bp.result(False, "Це не посилання на оголошення OLX. Потрібне посилання виду "
                                "https://www.olx.ua/d/uk/obyavlenie/…-ID….html")
    bp.info("Читаю оголошення OLX…")
    session = olx_source.new_session()
    try:
        raw = olx_source.fetch(url, session, log=bp.info)
    except Exception as e:  # noqa: BLE001
        return bp.result(False, f"Не вдалося прочитати оголошення: {e}")
    model = normalize_olx(raw)

    ts = time.strftime("%Y%m%d-%H%M%S")
    sid = str(model.get("source_id") or "page")
    os.makedirs(os.path.join(DIR, "runs"), exist_ok=True)
    run_path = os.path.join(DIR, "runs", f"{ts}_{sid}.json")
    with open(run_path, "w", encoding="utf-8") as f:
        json.dump({"raw": raw, "model": model}, f, ensure_ascii=False, indent=1)
    prune(os.path.join(DIR, "runs", "*.json"), 50)

    if len(model["rows"]) < 3 or not (model["photos"] or model["description"]):
        return bp.result(False, "OLX віддав замало даних (схоже, змінилась сторінка). Нічого не створено.\n"
                                f"Дані для діагностики: {run_path}" + (f"\nДеталі: {'; '.join(model['notes'])}" if model["notes"] else ""))

    cfg = load_config()
    agent = cfg.get("agent")
    if not a.no_ask:
        if agent is None:
            agent = ask_agent()
            cfg["agent"] = agent
            save_config(cfg)
        agent = review(model, agent, cfg)
    agent = agent or {}

    bp.info(f"Завантажую фото ({len(model['photos'])})…")
    import render_pdf  # тут, щоб помилка PyMuPDF не заважала Telegraph
    photos = render_pdf.download_photos(model["photos"], session, log=bp.info)

    tg_url, problems = None, []
    try:
        import telegraph
        tg_url = telegraph.publish(model, agent, DATA)
    except Exception as e:  # noqa: BLE001
        problems.append(f"Telegraph не вийшов: {e}")
        bp.info(problems[-1])

    files = []
    try:
        os.makedirs(os.path.join(DIR, "out"), exist_ok=True)
        pdf_path = os.path.join(DIR, "out", f"{ts}_{sid}.pdf")
        n = render_pdf.render(model, agent, photos, pdf_path)
        files.append({"path": pdf_path, "name": file_name(model)})
        bp.info(f"PDF: {n} фото, {os.path.getsize(pdf_path) // 1024} КБ")
        prune(os.path.join(DIR, "out", "*.pdf"))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        problems.append(f"PDF не вийшов: {type(e).__name__}: {e}")
        bp.info(problems[-1])

    if not tg_url and not files:
        return bp.result(False, "Презентацію не створено.\n" + "\n".join(problems))
    text = "Презентація: " + presentation_title(model)
    if tg_url:
        text += "\nTelegraph (посилання для клієнта):"
    if problems:
        text = "\n".join(problems) + "\n" + text
    bp.result(True, text, url=tg_url, files=files)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("make")
    p.add_argument("--url", required=True)
    p.add_argument("--no-ask", action="store_true", help="без питань: ціна як на OLX, підпис зі збереженого")
    a = ap.parse_args()
    try:
        {"make": cmd_make}[a.cmd](a)
    except BaseException as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        bp.result(False, f"Презентація: непередбачена помилка {type(e).__name__}: {e}")
    finally:
        bp.flush_info()


if __name__ == "__main__":
    main()
