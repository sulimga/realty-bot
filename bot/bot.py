"""
Telegram-бот — тільки інтерфейс. Про сайти нічого не знає: усе через dispatcher.

Запуск:  TELEGRAM_TOKEN=... REALTY_DATA_DIR=/var/lib/realty-bot python bot/bot.py
Доступ має лише власник: перший, хто надішле /start (його id зберігається),
або id з OWNER_ID.
"""

import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "dispatcher"), os.path.join(ROOT, "crm")]
import dispatcher as D  # noqa: E402

DATA = D.DATA
SECRETS = os.path.join(DATA, "secrets")
OWNER_FILE = os.path.join(DATA, "owner.json")
BATCH_PAUSE = 5          # пауза між оголошеннями в пакеті, сек
STARTED_AT = time.time()

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

BTN_POST = "📤 Викласти оголошення"
BTN_TP = "📎 Техпаспорт до чернетки"
BTN_COOKIES = "🍪 Cookies"
BTN_REFRESH = "🔄 Оновити дати rieltor"
BTN_STATUS = "ℹ️ Статус"
MENU = ReplyKeyboardMarkup([[KeyboardButton(BTN_POST)],
                            [KeyboardButton(BTN_TP), KeyboardButton(BTN_COOKIES)],
                            [KeyboardButton(BTN_REFRESH), KeyboardButton(BTN_STATUS)]],
                           resize_keyboard=True)


# ------------------------------------------------------------------ власник
def owner_id():
    env = os.environ.get("OWNER_ID", "").strip()
    if env.isdigit():
        return int(env)
    try:
        with open(OWNER_FILE, encoding="utf-8") as f:
            return int(json.load(f)["id"])
    except (OSError, ValueError, KeyError):
        return None


def claim_owner(user):
    os.makedirs(DATA, exist_ok=True)
    with open(OWNER_FILE, "w", encoding="utf-8") as f:
        json.dump({"id": user.id, "name": user.full_name}, f, ensure_ascii=False)


def is_owner(update: Update):
    oid = owner_id()
    return oid is not None and update.effective_user and update.effective_user.id == oid


# ------------------------------------------------------------------ допоміжне
def esc(s):
    return html.escape(str(s or ""))


def cut(s, n=3500):
    s = str(s or "")
    return s if len(s) <= n else s[: n - 20] + "\n… (обрізано)"


def st(ctx):
    """Стан розмови (один власник — один стан)."""
    return ctx.application.bot_data.setdefault("state", {"mode": None})


def reset(ctx):
    s = st(ctx)
    tmp = s.get("tmpdir")
    if tmp and os.path.isdir(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    s.clear()
    s["mode"] = None


class LiveLog:
    """Одне повідомлення, що оновлюється останніми рядками (замість десятків повідомлень)."""

    def __init__(self, bot, chat_id, title, quiet=False):
        self.bot, self.chat_id, self.title, self.quiet = bot, chat_id, title, quiet
        self.lines, self.msg, self.last_edit = [], None, 0.0

    async def add(self, text):
        for line in str(text).splitlines():
            if line.strip():
                self.lines.append(line.strip())
        if time.monotonic() - self.last_edit > 2:
            await self.flush()

    async def flush(self):
        body = "\n".join(self.lines[-14:])
        text = f"{self.title}\n<pre>{esc(cut(body, 3300))}</pre>" if body else self.title
        try:
            if self.msg is None:
                self.msg = await self.bot.send_message(self.chat_id, text, parse_mode=ParseMode.HTML,
                                                       disable_notification=self.quiet)
            else:
                await self.msg.edit_text(text, parse_mode=ParseMode.HTML)
        except BadRequest:
            pass
        self.last_edit = time.monotonic()


async def ask_user(ctx, chat_id, msg):
    """Питання від програми сайту -> кнопки в Telegram. Чекає відповіді."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    options = msg.get("options") or []
    rows, row = [], []
    for i, label in enumerate(options):
        row.append(InlineKeyboardButton(cut(label, 40), callback_data=f"ans:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⏭ Пропустити", callback_data="ans:skip")])
    hint = "\n\n<i>Або напишіть відповідь повідомленням.</i>" if msg.get("allow_text") else ""
    sent = await ctx.bot.send_message(chat_id, f"❓ {esc(cut(msg.get('text'), 3000))}{hint}",
                                      parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    ctx.application.bot_data["pending_ask"] = {"future": fut, "options": options,
                                               "allow_text": msg.get("allow_text"), "message": sent}
    try:
        return await fut
    finally:
        ctx.application.bot_data.pop("pending_ask", None)


async def run_board(ctx, chat_id, board_key, argv, quiet=False):
    b = D.boards(enabled_only=False)[board_key]
    live = LiveLog(ctx.bot, chat_id, f"▶️ <b>{esc(b['title'])}</b>", quiet=quiet)
    await live.flush()

    async def on_info(text):
        await live.add(text)

    async def on_ask(msg):
        await live.flush()
        return await ask_user(ctx, chat_id, msg)

    try:
        res = await D.run(board_key, argv, on_info, on_ask)
    except Exception as e:  # noqa: BLE001
        log.exception("dispatcher")
        res = {"ok": False, "text": f"{b['title']}: помилка диспетчера {e}"}
    await live.flush()
    icon = "📝" if res.get("draft") else ("✅" if res.get("ok") else "❌")
    text = f"{icon} {esc(cut(res.get('text'), 3000))}"
    if res.get("url"):
        text += f"\n{esc(res['url'])}"
    await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                               disable_notification=quiet)
    return res


# ------------------------------------------------------------------ команди
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if owner_id() is None:
        claim_owner(user)
        await update.message.reply_text(f"Вітаю, {user.first_name}! Тепер бот слухається тільки вас.",
                                        reply_markup=MENU)
        return
    if not is_owner(update):
        await update.message.reply_text("Це приватний бот.")
        return
    reset(ctx)
    await update.message.reply_text("Що робимо?", reply_markup=MENU)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш Telegram id: {update.effective_user.id}")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    pa = ctx.application.bot_data.get("pending_ask")
    if pa and not pa["future"].done():
        pa["future"].set_result({})
    batch = ctx.application.bot_data.get("batch")
    reset(ctx)
    if batch:
        batch["stop"] = True
        return await update.message.reply_text(f"⏹ Пакет зупиниться після поточного оголошення "
                                               f"({batch['i']}/{batch['n']}).", reply_markup=MENU)
    await update.message.reply_text("Скасовано.", reply_markup=MENU)


async def cmd_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Оновити код з GitHub і перезапуститись."""
    if not is_owner(update):
        return
    if ctx.application.bot_data.get("busy"):
        await update.message.reply_text("Зараз іде публікація — оновлю після неї. Спробуйте за хвилину.")
        return
    script = os.path.join(ROOT, "deploy", "update.sh")
    await update.message.reply_text("⬇️ Оновлюю код з GitHub…")
    proc = await asyncio.create_subprocess_exec("bash", script, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace")
    await update.message.reply_text(f"<pre>{esc(cut(text, 3500))}</pre>", parse_mode=ParseMode.HTML)
    if proc.returncode == 0:
        await update.message.reply_text("🔁 Перезапускаюсь… Через ~10 сек надішліть /start")
        await asyncio.sleep(1)
        os._exit(0)          # systemd підніме бота вже з новим кодом
    else:
        await update.message.reply_text("❌ Оновлення не вдалося, працюю на старій версії.")


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    logs_dir = os.path.join(DATA, "logs")
    files = [os.path.join(logs_dir, f) for f in os.listdir(logs_dir)] if os.path.isdir(logs_dir) else []
    if os.path.exists(D.JOURNAL):
        files.append(D.JOURNAL)
    if not files:
        await update.message.reply_text("Логів поки немає.")
        return
    for p in files:
        if os.path.getsize(p) > 0:
            with open(p, "rb") as f:
                await update.message.reply_document(f, filename=os.path.basename(p))


async def show_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ver = subprocess.run(["git", "-C", ROOT, "log", "-1", "--format=%h %cd", "--date=format:%d.%m %H:%M"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        ver = "?"
    up = int(time.time() - STARTED_AT)
    lines = [f"Версія коду: {ver or '?'}", f"Працює: {up // 3600} год {up % 3600 // 60} хв", "",
             "Cookie:"]
    for key, b in D.boards().items():
        paths = [os.path.join(SECRETS, f"{key}_{n}.txt") for n in ("cookies", "auth")]
        mt = [os.path.getmtime(p) for p in paths if os.path.exists(p)]
        when = time.strftime("%d.%m %H:%M", time.localtime(max(mt))) if mt else "—"
        lines.append(f"  {b['title']}: {'оновлено ' + when if when != '—' else 'немає'}")
    batch = ctx.application.bot_data.get("batch")
    if batch:
        mins = int(time.time() - batch["started"]) // 60
        lines += ["", f"📦 Іде пакет rieltor: {batch['i']}/{batch['n']}, успішно {batch['ok']} ({mins} хв)"]
    j = D.last_journal(8)
    if j:
        lines += ["", "Останні дії:"]
        for e in reversed(j):
            icon = "📝" if e.get("draft") else ("✅" if e.get("ok") else "❌")
            lines.append(f"  {e['ts'][5:16]} {icon} {cut(e.get('text'), 90)}")
    await update.message.reply_text("\n".join(lines))


# ------------------------------------------------------------------ сценарій «викласти оголошення»
def crm_summary(url):
    from extract_estate import fetch_estate_data
    e = fetch_estate_data(url)
    p = e.get("parameters") or {}
    area = next((v for k, v in p.items() if "площа" in k.lower() or "площадь" in k.lower()), None)
    lines = [f"<b>{esc(e.get('deal_title'))}</b> {esc(e.get('article'))}",
             f"📍 {esc(e.get('address'))}",
             f"💵 {esc(e.get('price'))}" + (f"   📐 {esc(area)}" if area else ""),
             f"🖼 фото: {e.get('photos_count', 0)}"]
    return e, "\n".join(lines)


def boards_keyboard(selected):
    rows = []
    for key, b in D.boards().items():
        mark = "✅" if key in selected else "▫️"
        rows.append([InlineKeyboardButton(f"{mark} {b['title']}", callback_data=f"tg:{key}")])
    rows.append([InlineKeyboardButton("Далі ➡️", callback_data="boards_next"),
                 InlineKeyboardButton("✖️ Скасувати", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


async def start_post(update, ctx):
    reset(ctx)
    st(ctx).update({"mode": "choose_boards_first", "selected": []})
    await update.message.reply_text("Куди викласти?", reply_markup=boards_keyboard([]))


URL_RE = re.compile(r"https?://[^\s,;]+")


def extract_urls(text):
    return list(dict.fromkeys(u.rstrip(").]'\"") for u in URL_RE.findall(text or "")))


async def ask_links(chat_id, ctx):
    s = st(ctx)
    if s["selected"] == ["rieltor"]:
        s["mode"] = "await_urls_batch"
        return await ctx.bot.send_message(
            chat_id, "Надішліть посилання з CRM: одне, кілька (через кому або кожне з нового рядка) "
                     "або .txt файл зі списком — тоді бот викладатиме їх по черзі сам.")
    s["mode"] = "await_url"
    await ctx.bot.send_message(chat_id, "Надішліть посилання на презентацію об'єкта з CRM.")


async def got_urls_batch(chat_id, ctx, urls):
    s = st(ctx)
    if not urls:
        return await ctx.bot.send_message(chat_id, "Не знайшов жодного посилання. Надішліть ще раз або /cancel")
    if len(urls) == 1:
        s["mode"] = "await_url"
        return await got_url_chat(chat_id, ctx, urls[0])
    s.update({"mode": "confirm_batch", "batch_urls": urls})
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🚀 Запустити ({len(urls)})", callback_data="batch_go")],
                               [InlineKeyboardButton("✖️ Скасувати", callback_data="cancel")]])
    await ctx.bot.send_message(
        chat_id, f"Пакет для rieltor.ua: <b>{len(urls)}</b> оголошень, по черзі з паузою.\n"
                 "Питань під час пакета не буде: якщо чогось бракує (напр. площі ділянки), оголошення "
                 "пропускається і потрапить у підсумок. Зупинити — /cancel (після поточного оголошення).",
        parse_mode=ParseMode.HTML, reply_markup=kb)


async def run_batch(ctx, chat_id, urls):
    bd = ctx.application.bot_data
    bd["busy"] = True
    batch = bd["batch"] = {"n": len(urls), "i": 0, "ok": 0, "stop": False, "started": time.time()}
    failed, stopped_reason = [], None
    try:
        for i, url in enumerate(urls, 1):
            if batch["stop"]:
                stopped_reason = "зупинено командою /cancel"
                break
            batch["i"] = i
            await ctx.bot.send_message(chat_id, f"📦 {i}/{len(urls)}", disable_notification=True)
            res = await run_board(ctx, chat_id, "rieltor", ["post", "--url", url, "--no-ask"], quiet=True)
            if res.get("ok"):
                batch["ok"] += 1
            else:
                failed.append((url, res.get("text") or ""))
            if res.get("stop_batch"):
                stopped_reason = res.get("text")
                break
            if i < len(urls):
                await asyncio.sleep(BATCH_PAUSE)
    except Exception as e:  # noqa: BLE001
        log.exception("batch")
        stopped_reason = f"помилка бота: {e}"
    finally:
        bd["busy"] = False
        bd.pop("batch", None)
    lines = [f"📦 Пакет rieltor завершено: ✅ {batch['ok']} з {len(urls)}"]
    if stopped_reason:
        rest = urls[batch["i"]:]
        lines.append(f"⛔ Зупинено: {cut(stopped_reason, 300)}")
        if rest:
            lines.append(f"Не оброблено ({len(rest)}):\n" + "\n".join(rest))
    if failed:
        lines.append(f"❌ З помилкою ({len(failed)}):")
        lines += [f"• {u}\n  {cut(t, 160)}" for u, t in failed]
    await ctx.bot.send_message(chat_id, cut("\n".join(lines), 4000), disable_web_page_preview=True,
                               reply_markup=MENU)


async def got_url(update, ctx, url):
    return await got_url_chat(update.effective_chat.id, ctx, url)


async def got_url_chat(chat_id, ctx, url):
    s = st(ctx)
    msg = await ctx.bot.send_message(chat_id, "🔎 Читаю презентацію…")
    try:
        estate, summary = await asyncio.to_thread(crm_summary, url)
    except Exception as e:  # noqa: BLE001
        await msg.edit_text(f"❌ Не вдалося прочитати презентацію: {e}\nНадішліть інше посилання або /cancel")
        return
    pre = s.get("selected") or []
    s.update({"url": url, "summary": summary, "article": estate.get("article")})
    if pre:                                   # сайти вже обрано на першому кроці
        await msg.edit_text(summary, parse_mode=ParseMode.HTML)
        if any(D.boards()[k].get("needs_techpassport") for k in pre):
            return await ask_techpassport(chat_id, ctx)
        return await confirm_publish(chat_id, ctx)
    s.update({"mode": "choose_boards", "selected": []})
    await msg.edit_text(f"{summary}\n\nКуди викласти?", parse_mode=ParseMode.HTML,
                        reply_markup=boards_keyboard([]))


async def ask_techpassport(chat_id, ctx):
    s = st(ctx)
    s["mode"] = "await_files"
    s["files"] = []
    s["tmpdir"] = tempfile.mkdtemp(prefix="tp_", dir=os.path.join(DATA, "tmp") if os.path.isdir(os.path.join(DATA, "tmp")) else None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Готово ✔️", callback_data="files_done")],
                               [InlineKeyboardButton("Без техпаспорта (буде чернетка)", callback_data="files_skip")]])
    await ctx.bot.send_message(chat_id, "📎 Надішліть техпаспорт для dom.ria: PDF або фото сторінок "
                                        "(можна кілька). Потім натисніть «Готово».", reply_markup=kb)


async def confirm_publish(chat_id, ctx):
    s = st(ctx)
    s["mode"] = "confirm"
    reg = D.boards()
    names = ", ".join(reg[k]["title"] for k in s["selected"])
    tp = s.get("files") or []
    extra = ""
    if "domria" in s["selected"]:
        extra = f"\n📎 Техпаспорт: {len(tp)} файл(ів)" if tp else "\n📎 Без техпаспорта — на dom.ria буде чернетка"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Опублікувати", callback_data="publish")],
                               [InlineKeyboardButton("✖️ Скасувати", callback_data="cancel")]])
    await ctx.bot.send_message(chat_id, f"{s['summary']}\n\nКуди: <b>{esc(names)}</b>{extra}",
                               parse_mode=ParseMode.HTML, reply_markup=kb)


async def do_publish(chat_id, ctx):
    s = st(ctx)
    ctx.application.bot_data["busy"] = True
    try:
        reg = D.boards()
        for key in s["selected"]:
            argv = ["post", "--url", s["url"]]
            if reg[key].get("needs_techpassport") and s.get("files"):
                argv += ["--techpassport", *s["files"]]
            await run_board(ctx, chat_id, key, argv)
    finally:
        ctx.application.bot_data["busy"] = False
        reset(ctx)
    await ctx.bot.send_message(chat_id, "Готово. Що далі?", reply_markup=MENU)


# ------------------------------------------------------------------ техпаспорт до чернетки
def recent_drafts(n=8):
    posted = os.path.join(DATA, "domria", "posted")
    if not os.path.isdir(posted):
        return []
    items = []
    for f in os.listdir(posted):
        try:
            with open(os.path.join(posted, f), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if rec.get("status") in ("draft_complete", "draft_created"):
            items.append((os.path.getmtime(os.path.join(posted, f)), f[:-5], rec.get("article") or ""))
    return [(rid, art) for _, rid, art in sorted(items, reverse=True)[:n]]


async def start_tp(update, ctx):
    reset(ctx)
    s = st(ctx)
    s["mode"] = "await_tp_id"
    drafts = recent_drafts()
    kb = None
    if drafts:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"№ {rid} {art}", callback_data=f"tpid:{rid}")]
                                   for rid, art in drafts])
    await update.message.reply_text("До якої чернетки dom.ria? Оберіть або надішліть номер оголошення.",
                                    reply_markup=kb)


# ------------------------------------------------------------------ cookies
async def start_cookies(update, ctx):
    reset(ctx)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(b["title"], callback_data=f"ck:{k}")]
                               for k, b in D.boards().items()])
    await update.message.reply_text("Для якого сайту оновити cookie?", reply_markup=kb)


async def save_cookie(update, ctx, board_key, text):
    os.makedirs(SECRETS, exist_ok=True)
    os.chmod(SECRETS, 0o700)
    raw = text.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    # Рядок «Authorization: Bearer <JWT>» (OLX) зберігаємо окремо, щоб не затерти cookie і навпаки
    jwt = re.search(r"eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+", raw)
    pairs = len(re.findall(r"[\w.-]+=[^;\s]*", re.sub(r"eyJ[\w.-]+", "", raw)))
    saved = []
    if jwt and ("bearer" in raw.lower() or "authorization" in raw.lower() or pairs < 3):
        p = os.path.join(SECRETS, f"{board_key}_auth.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("Authorization: Bearer " + jwt.group(0))
        os.chmod(p, 0o600)
        saved.append("токен")
    if pairs >= 3:
        p = os.path.join(SECRETS, f"{board_key}_cookies.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(l for l in raw.splitlines() if "bearer" not in l.lower()
                              and not l.lower().startswith("authorization")))
        os.chmod(p, 0o600)
        saved.append("cookie")
    try:
        await update.message.delete()          # не тримаємо cookie в історії чату
    except BadRequest:
        pass
    reset(ctx)
    if not saved:
        return await ctx.bot.send_message(update.effective_chat.id, "Не схоже ні на cookie, ні на Authorization — нічого не зберіг.")
    await ctx.bot.send_message(update.effective_chat.id, f"🍪 Збережено ({' і '.join(saved)}), перевіряю…")
    await run_board(ctx, update.effective_chat.id, board_key, ["check"])


# ------------------------------------------------------------------ обробники
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text = (update.message.text or "").strip()
    s = st(ctx)
    pa = ctx.application.bot_data.get("pending_ask")

    if text == BTN_POST:
        if ctx.application.bot_data.get("batch"):
            b = ctx.application.bot_data["batch"]
            return await update.message.reply_text(f"Зараз іде пакет rieltor ({b['i']}/{b['n']}). "
                                                   "Дочекайтесь або зупиніть: /cancel")
        return await start_post(update, ctx)
    if text == BTN_TP:
        return await start_tp(update, ctx)
    if text == BTN_COOKIES:
        return await start_cookies(update, ctx)
    if text == BTN_STATUS:
        return await show_status(update, ctx)
    if text == BTN_REFRESH:
        if ctx.application.bot_data.get("busy"):
            return await update.message.reply_text("Зачекайте, іде інша дія.")
        return await run_board(ctx, update.effective_chat.id, "rieltor", ["refresh"])

    if pa and not pa["future"].done():
        if pa.get("allow_text"):
            pa["future"].set_result({"text": text})
            return
        return await update.message.reply_text("Оберіть варіант кнопкою вище або «Пропустити».")

    mode = s.get("mode")
    if mode == "await_urls_batch":
        return await got_urls_batch(update.effective_chat.id, ctx, extract_urls(text))
    if mode == "await_url":
        m = re.search(r"https?://\S+", text)
        if not m:
            return await update.message.reply_text("Це не схоже на посилання. Надішліть посилання з CRM.")
        return await got_url(update, ctx, m.group(0))
    if mode and mode.startswith("await_cookie:"):
        return await save_cookie(update, ctx, mode.split(":", 1)[1], text)
    if mode == "await_tp_id":
        if not text.isdigit():
            return await update.message.reply_text("Надішліть номер оголошення dom.ria (тільки цифри).")
        s["tp_realty_id"] = text
        return await ask_techpassport(update.effective_chat.id, ctx)

    if re.match(r"https?://", text):       # просто кинули посилання — одразу починаємо
        reset(ctx)
        st(ctx)["mode"] = "await_url"
        return await got_url(update, ctx, re.search(r"https?://\S+", text).group(0))
    await update.message.reply_text("Оберіть дію в меню 👇", reply_markup=MENU)


async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    s = st(ctx)
    msg = update.message
    if s.get("mode") == "await_urls_batch" and msg.document:
        if msg.document.file_size and msg.document.file_size > 2_000_000:
            return await msg.reply_text("Завеликий файл для списку посилань.")
        raw = bytes(await (await msg.document.get_file()).download_as_bytearray())
        for enc in ("utf-8-sig", "cp1251", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        return await got_urls_batch(update.effective_chat.id, ctx, extract_urls(text))
    if s.get("mode") != "await_files":
        return await update.message.reply_text("Файл зараз не очікується. Спершу оберіть дію в меню.")
    if msg.document:
        tg_file = await msg.document.get_file()
        name = msg.document.file_name or f"file_{len(s['files']) + 1}"
    else:
        tg_file = await msg.photo[-1].get_file()          # найбільший розмір
        name = f"photo_{len(s['files']) + 1:02d}.jpg"
    path = os.path.join(s["tmpdir"], f"{len(s['files']) + 1:02d}_{re.sub(r'[^\w.\-]', '_', name)}")
    await tg_file.download_to_drive(path)
    s["files"].append(path)
    await msg.reply_text(f"📎 Отримано файлів: {len(s['files'])}. Ще, або «Готово».",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Готово ✔️", callback_data="files_done")]]))


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(update):
        return await q.answer()
    await q.answer()
    data = q.data or ""
    s = st(ctx)
    chat_id = q.message.chat_id

    if data.startswith("ans:"):
        pa = ctx.application.bot_data.get("pending_ask")
        if not pa or pa["future"].done():
            return
        val = data[4:]
        if val == "skip":
            pa["future"].set_result({})
            chosen = "пропущено"
        else:
            pa["future"].set_result({"choice": int(val)})
            chosen = pa["options"][int(val)]
        try:
            await q.edit_message_reply_markup(None)
            await ctx.bot.send_message(chat_id, f"↪️ {chosen}")
        except BadRequest:
            pass
        return

    if data == "cancel":
        reset(ctx)
        try:
            await q.edit_message_reply_markup(None)
        except BadRequest:
            pass
        return await ctx.bot.send_message(chat_id, "Скасовано.", reply_markup=MENU)

    if data.startswith("tg:") and s.get("mode") in ("choose_boards", "choose_boards_first"):
        key = data[3:]
        sel = s["selected"]
        sel.remove(key) if key in sel else sel.append(key)
        try:
            await q.edit_message_reply_markup(boards_keyboard(sel))
        except BadRequest:
            pass
        return

    if data == "boards_next" and s.get("mode") == "choose_boards_first":
        if not s["selected"]:
            return await ctx.bot.send_message(chat_id, "Позначте хоча б один сайт.")
        await q.edit_message_reply_markup(None)
        names = ", ".join(D.boards()[k]["title"] for k in s["selected"])
        await ctx.bot.send_message(chat_id, f"Куди: <b>{esc(names)}</b>", parse_mode=ParseMode.HTML)
        return await ask_links(chat_id, ctx)

    if data == "batch_go" and s.get("mode") == "confirm_batch":
        if ctx.application.bot_data.get("busy"):
            return await ctx.bot.send_message(chat_id, "Зачекайте, іде інша дія.")
        await q.edit_message_reply_markup(None)
        urls = s["batch_urls"]
        reset(ctx)
        ctx.application.bot_data["busy"] = True          # одразу, щоб не запустити двічі
        ctx.application.create_task(run_batch(ctx, chat_id, urls))
        return await ctx.bot.send_message(chat_id, f"▶️ Пакет запущено: {len(urls)} оголошень. "
                                                   "Прогрес — «ℹ️ Статус».", reply_markup=MENU)

    if data == "boards_next" and s.get("mode") == "choose_boards":
        if not s["selected"]:
            return await ctx.bot.send_message(chat_id, "Позначте хоча б один сайт.")
        await q.edit_message_reply_markup(None)
        reg = D.boards()
        if any(reg[k].get("needs_techpassport") for k in s["selected"]):
            return await ask_techpassport(chat_id, ctx)
        return await confirm_publish(chat_id, ctx)

    if data in ("files_done", "files_skip") and s.get("mode") == "await_files":
        if data == "files_skip":
            s["files"] = []
        elif not s["files"]:
            return await ctx.bot.send_message(chat_id, "Ще не отримано жодного файлу. Надішліть техпаспорт "
                                                       "або натисніть «Без техпаспорта».")
        try:
            await q.edit_message_reply_markup(None)
        except BadRequest:
            pass
        if s.get("tp_realty_id"):                       # сценарій «техпаспорт до чернетки»
            if not s["files"]:
                reset(ctx)
                return await ctx.bot.send_message(chat_id, "Скасовано.", reply_markup=MENU)
            ctx.application.bot_data["busy"] = True
            try:
                await run_board(ctx, chat_id, "domria", ["techpassport", "--realty-id", s["tp_realty_id"], *s["files"]])
            finally:
                ctx.application.bot_data["busy"] = False
                reset(ctx)
            return
        return await confirm_publish(chat_id, ctx)

    if data == "publish" and s.get("mode") == "confirm":
        if ctx.application.bot_data.get("busy"):
            return await ctx.bot.send_message(chat_id, "Зачекайте, іде попередня публікація.")
        await q.edit_message_reply_markup(None)
        return await do_publish(chat_id, ctx)

    if data.startswith("tpid:"):
        s["tp_realty_id"] = data[5:]
        await q.edit_message_reply_markup(None)
        return await ask_techpassport(chat_id, ctx)

    if data.startswith("ck:"):
        key = data[3:]
        s["mode"] = f"await_cookie:{key}"
        await q.edit_message_reply_markup(None)
        return await ctx.bot.send_message(
            chat_id, D.boards()[key].get("cookie_hint") or (
                     f"Надішліть cookie для {D.boards()[key]['title']} одним повідомленням.\n"
                     "Де взяти: у Chrome відкрийте сайт (залогіненим) → F12 → Network → оновіть сторінку → "
                     "будь-який запит до сайту → Request Headers → cookie → скопіюйте значення.\n"
                     "Повідомлення з cookie я одразу видалю з чату."))


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception("Помилка в обробнику", exc_info=ctx.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await ctx.bot.send_message(update.effective_chat.id, f"⚠️ Помилка бота: {ctx.error}")
    except Exception:  # noqa: BLE001
        pass


def main():
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        sys.exit("Немає TELEGRAM_TOKEN (див. /etc/realty-bot.env)")
    os.makedirs(os.path.join(DATA, "tmp"), exist_ok=True)
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("status", show_status))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("Бот запущено. Дані: %s", DATA)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
