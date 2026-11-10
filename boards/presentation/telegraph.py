"""
Публікація презентації на telegra.ph (https://telegra.ph/api). Та сама структура сторінки, що в розширенні Chrome.
Токен акаунта Telegraph — $REALTY_DATA_DIR/presentation/telegraph.json (створюється сам, chmod 600).
Фото — прямі посилання на CDN OLX (завантаження файлів у Telegraph вимкнене).
"""

import json
import os
import re

import requests

from normalize import presentation_title, quick_facts, unit_price

API = "https://api.telegra.ph"


def _el(tag, children=None, attrs=None):
    n = {"tag": tag}
    if attrs:
        n["attrs"] = attrs
    if children is not None:
        n["children"] = children if isinstance(children, list) else [children]
    return n


def _paragraphs(text):
    out = []
    for block in re.split(r"\n{2,}", str(text or "")):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        ch = []
        for i, ln in enumerate(lines):
            if i:
                ch.append(_el("br"))
            ch.append(ln)
        out.append(_el("p", ch))
    return out


def agent_line(agent):
    agent = agent or {}
    return " · ".join(str(agent.get(k) or "").strip() for k in ("name", "phone", "extra") if str(agent.get(k) or "").strip())


def build_page(model, agent):
    photos = list(model.get("photos") or [])
    rows = [r for r in model.get("rows") or [] if str(r.get("label") or "").strip() and str(r.get("value") or "").strip()]
    c = []
    if photos:
        c.append(_el("figure", [_el("img", attrs={"src": photos[0]})]))
    price = model.get("price") or {}
    if price.get("show") and price.get("text"):
        up = unit_price(model)
        c.append(_el("h3", f"{price['text']} ({up})" if up else price["text"]))
    facts = " · ".join(quick_facts(model.get("rows")))
    if facts:
        c.append(_el("p", [_el("b", facts)]))
    if rows:
        c.append(_el("h4", "Параметри"))
        c.append(_el("ul", [_el("li", [_el("b", f"{r['label'].strip()}: "), r["value"].strip()]) for r in rows]))
    if str(model.get("description") or "").strip():
        c.append(_el("h4", "Опис"))
        c.extend(_paragraphs(model["description"]))
    if len(photos) > 1:
        c.append(_el("h4", "Фото"))
        c.extend(_el("figure", [_el("img", attrs={"src": u})]) for u in photos[1:])
    ag = agent_line(agent)
    if ag:
        c.append(_el("hr"))
        c.append(_el("p", [_el("i", "Ваш ріелтор: "), _el("b", ag)]))
    return {"title": presentation_title(model)[:256], "author_name": str((agent or {}).get("name") or "")[:128], "content": c}


def _call(method, params, session=None):
    s = session or requests
    r = s.post(f"{API}/{method}", data=params, timeout=40)
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError(f"Telegraph: HTTP {r.status_code}")
    if not j.get("ok"):
        raise RuntimeError(f"Telegraph: {j.get('error') or 'помилка'}")
    return j["result"]


def _token_path(data_dir):
    return os.path.join(data_dir, "presentation", "telegraph.json")


def get_token(data_dir, author_name="", session=None):
    p = _token_path(data_dir)
    try:
        with open(p, encoding="utf-8") as f:
            t = json.load(f).get("access_token")
        if t:
            return t
    except (OSError, ValueError):
        pass
    acc = _call("createAccount", {"short_name": "Презентації", "author_name": author_name[:128]}, session)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"access_token": acc["access_token"]}, f)
    os.chmod(p, 0o600)
    return acc["access_token"]


def publish(model, agent, data_dir, session=None):
    """Створює сторінку. Повертає URL."""
    page = build_page(model, agent)
    content = json.dumps(page["content"], ensure_ascii=False)
    if len(content.encode("utf-8")) > 63000:
        raise RuntimeError("Telegraph: завеликий текст (ліміт 64 КБ).")
    token = get_token(data_dir, page["author_name"], session)
    res = _call("createPage", {"access_token": token, "title": page["title"], "author_name": page["author_name"],
                               "content": content, "return_content": "false"}, session)
    return res["url"]
