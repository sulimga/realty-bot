"""
PDF-презентація (A4) з моделі — як у розширенні Chrome / публічних презентаціях CRM:
заголовок «Продаж / Квартира», ціна й ціна за м², адреса, коротко (площа, кімнати, поверх), головне фото,
«Параметри», «Додаткова інформація», фото по два в ряд, підпис ріелтора.

Верстка вручну (PyMuPDF: текст, лінії, картинки за координатами). HTML-верстка PyMuPDF (Story) не годиться:
ширини колонок таблиць ігнорує, текст накладається (перевірено 10.11.2026).
Шрифт — вбудований у PyMuPDF Helvetica (Nimbus Sans, з кирилицею), вшивається в PDF.
"""

import io

try:
    import pymupdf as fitz
except ImportError:          # старі версії PyMuPDF
    import fitz
from PIL import Image, ImageOps

from normalize import presentation_title, quick_facts, unit_price

INK = (0.114, 0.141, 0.200)
MUTED = (0.42, 0.45, 0.52)
ACCENT = (0.122, 0.373, 0.659)
LINE = (0.886, 0.902, 0.929)
CHIP = (0.933, 0.953, 0.980)
M = 34                      # поле сторінки, pt (≈12 мм)
W, H = fitz.paper_size("a4")
X0, X1 = M, W - M
CW = X1 - X0                # ширина вмісту

REG = fitz.Font("helv")
BOLD = fitz.Font("hebo")


def wrap(text, font, size, width):
    """Розбиває текст на рядки по ширині (переноси рядків у тексті зберігаються)."""
    lines = []
    for para in str(text or "").split("\n"):
        cur = ""
        for word in para.split(" "):
            cand = f"{cur} {word}" if cur else word
            if font.text_length(cand, fontsize=size) <= width:
                cur = cand
                continue
            if cur:
                lines.append(cur)
            while font.text_length(word, fontsize=size) > width and len(word) > 1:
                k = len(word) - 1
                while k > 1 and font.text_length(word[:k], fontsize=size) > width:
                    k -= 1
                lines.append(word[:k])
                word = word[k:]
            cur = word
        lines.append(cur)
    return lines


class Pdf:
    def __init__(self):
        self.doc = fitz.open()
        self.page = None
        self.y = 0
        self.new_page()

    def new_page(self):
        self.page = self.doc.new_page(width=W, height=H)
        self.page.insert_font(fontname="R", fontbuffer=REG.buffer)
        self.page.insert_font(fontname="B", fontbuffer=BOLD.buffer)
        self.y = M

    def ensure(self, h):
        if self.y + h > H - M:
            self.new_page()

    def text(self, x, top, s, size, bold=False, color=INK):
        font = BOLD if bold else REG
        self.page.insert_text((x, top + size * font.ascender), s, fontname="B" if bold else "R", fontsize=size, color=color)

    def hline(self, y, color=LINE, width=0.6, dashes=None):
        self.page.draw_line((X0, y), (X1, y), color=color, width=width, dashes=dashes)

    def section(self, title, need=60):
        self.ensure(26 + need)
        self.y += 10
        self.text(X0, self.y, title, 9, bold=True, color=ACCENT)
        self.y += 13
        self.hline(self.y)
        self.y += 5

    def image(self, rect, data):
        self.page.insert_image(rect, stream=data, keep_proportion=False)


def _jpeg(data, w, h):
    """Байти фото -> JPEG рівно w×h (обрізання по центру), з урахуванням EXIF-повороту."""
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im).convert("RGB")
    im = ImageOps.fit(im, (w, h), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=80, optimize=True)
    return out.getvalue()


def download_photos(urls, session, log=print, limit=30):
    """Завантажує фото; повертає список байтів (ті, що не завантажились, пропускає)."""
    out = []
    for i, u in enumerate(urls[:limit], 1):
        try:
            r = session.get(u, timeout=40)
            if r.status_code == 200 and r.content:
                Image.open(io.BytesIO(r.content)).verify()
                out.append(r.content)
            else:
                log(f"фото {i}: HTTP {r.status_code} — пропускаю")
        except Exception as e:  # noqa: BLE001
            log(f"фото {i}: {e} — пропускаю")
    return out


def _header(p, model):
    price = model.get("price") or {}
    show = price.get("show") and price.get("text")
    up = unit_price(model) if show else ""
    right_w = 0.0
    if show:
        right_w = min(CW * 0.45, max(BOLD.text_length(price["text"], fontsize=18), REG.text_length(up, fontsize=9)))
    left_w = CW - right_w - (16 if right_w else 0)
    k_lines = wrap(model.get("kicker") or "", BOLD, 18, left_w)
    addr = next((r["value"] for r in model["rows"] if r["key"] == "address"), "")
    a_lines = wrap(addr, REG, 11, left_w) if addr else []
    top = p.y
    y = top
    for ln in k_lines:
        p.text(X0, y, ln, 18, bold=True)
        y += 22
    y += 2
    for ln in a_lines:
        p.text(X0, y, ln, 11, color=MUTED)
        y += 14
    ry = top
    if show:
        for ln in wrap(price["text"], BOLD, 18, right_w):
            p.text(X1 - BOLD.text_length(ln, fontsize=18), ry, ln, 18, bold=True, color=ACCENT)
            ry += 22
        if up:
            p.text(X1 - REG.text_length(up, fontsize=9), ry + 1, up, 9, color=MUTED)
            ry += 13
    p.y = max(y, ry) + 4
    p.hline(p.y, color=ACCENT, width=2)
    p.y += 9


def _chips(p, facts):
    if not facts:
        return
    x, h = X0, 20
    for f in facts:
        w = BOLD.text_length(f, fontsize=10.5) + 18
        if x + w > X1 and x > X0:
            x = X0
            p.y += h + 6
        p.page.draw_rect(fitz.Rect(x, p.y, x + w, p.y + h), color=None, fill=CHIP, radius=0.25)
        p.text(x + 9, p.y + 4.5, f, 10.5, bold=True)
        x += w + 7
    p.y += h + 10


def _rows(p, rows):
    kw = CW * 0.38
    vw = CW - kw - 8
    for r in rows:
        kl = wrap(r["label"], REG, 10, kw - 6)
        vl = wrap(r["value"], BOLD, 10, vw)
        h = max(len(kl), len(vl)) * 13 + 6
        p.ensure(h)
        for i, ln in enumerate(kl):
            p.text(X0, p.y + 3 + i * 13, ln, 10, color=MUTED)
        for i, ln in enumerate(vl):
            p.text(X0 + kw + 8, p.y + 3 + i * 13, ln, 10, bold=True)
        p.y += h
        p.hline(p.y, color=LINE, width=0.5, dashes="[1 2] 0")


def _paragraphs(p, text):
    for block in str(text).split("\n\n"):
        for ln in wrap(block.strip(), REG, 10, CW):
            p.ensure(14)
            p.text(X0, p.y, ln, 10)
            p.y += 14
        p.y += 5


def render(model, agent, photo_bytes, out_path):
    """Збирає PDF у out_path. photo_bytes — список байтів фото (перше — головне). Повертає кількість фото в PDF."""
    hero_h = round(CW * 0.6)
    cell_w = (CW - 10) / 2
    cell_h = round(cell_w * 0.72)
    imgs = []
    for data in photo_bytes:
        try:
            imgs.append(_jpeg(data, 1240, round(1240 * hero_h / CW)) if not imgs else _jpeg(data, 760, round(760 * cell_h / cell_w)))
        except Exception:  # noqa: BLE001 — битий файл просто пропускаємо
            continue

    p = Pdf()
    _header(p, model)
    _chips(p, quick_facts(model["rows"]))
    if imgs:
        p.ensure(hero_h)
        p.image(fitz.Rect(X0, p.y, X1, p.y + hero_h), imgs[0])
        p.y += hero_h + 4

    rows = [r for r in model["rows"] if str(r.get("label") or "").strip() and str(r.get("value") or "").strip()]
    if rows:
        p.section("ПАРАМЕТРИ")
        _rows(p, rows)
    desc = str(model.get("description") or "").strip()
    if desc:
        p.section("ДОДАТКОВА ІНФОРМАЦІЯ")
        _paragraphs(p, desc)
    if len(imgs) > 1:
        p.section("ФОТО", need=cell_h)
        rest = imgs[1:]
        for i in range(0, len(rest), 2):
            p.ensure(cell_h)
            for j, data in enumerate(rest[i:i + 2]):
                x = X0 + j * (cell_w + 10)
                p.image(fitz.Rect(x, p.y, x + cell_w, p.y + cell_h), data)
            p.y += cell_h + 10

    line = " · ".join(str((agent or {}).get(k) or "").strip() for k in ("name", "phone", "extra") if str((agent or {}).get(k) or "").strip())
    if line:
        p.ensure(40)
        p.y += 8
        p.hline(p.y, color=ACCENT, width=2)
        p.y += 8
        lbl = "Ваш ріелтор:  "
        p.text(X0, p.y + 1, lbl, 10, color=MUTED)
        x = X0 + REG.text_length(lbl, fontsize=10)
        for i, ln in enumerate(wrap(line, BOLD, 11.5, X1 - x)):
            p.text(x, p.y + i * 15, ln, 11.5, bold=True)

    p.doc.set_metadata({"title": presentation_title(model), "creator": "realty-bot"})
    # subset_fonts() НЕ викликати: у PyMuPDF 1.24 після нього текст зникає (шрифт і так ~33 КБ)
    p.doc.save(out_path, garbage=3, deflate=True)
    p.doc.close()
    return len(imgs)
