"""
Протокол між диспетчером і програмою сайту (dom.ria, rieltor, olx, ...).

Програма сайту запускається окремим процесом і спілкується рядками JSON:

  stdout (сайт -> диспетчер), один JSON на рядок:
    {"type": "info",   "text": "..."}                         — повідомлення про хід роботи
    {"type": "ask",    "id": 1, "text": "...",
     "options": [[value, "підпис"], ...] | null,
     "allow_text": true|false}                                — питання користувачу
    {"type": "result", "ok": true|false, "text": "...",
     "url": "...", "id": "...", "draft": true|false}          — підсумок (останнє повідомлення)

  stdin (диспетчер -> сайт), відповідь на "ask":
    {"id": 1, "choice": 3}       — обрано варіант з номером 3 (з 0)
    {"id": 1, "text": "..."}     — введено текст
    {"id": 1}                    — пропущено

Будь-який інший вивід (print з глибин коду, traceback) у stdout не потрапляє:
print() перехоплюється і перетворюється на "info", а stderr іде в лог.

Програму сайту можна запускати і вручну з консолі: якщо stdin — термінал,
питання ставляться звичайним input().
"""

import builtins
import json
import sys

_real_stdout = sys.stdout
_ask_counter = 0
_pending_info = []
INTERACTIVE = sys.stdin is not None and sys.stdin.isatty()


def _emit(obj):
    _real_stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _real_stdout.flush()


def flush_info():
    """Відправити накопичені повідомлення одним блоком."""
    global _pending_info
    text = "\n".join(_pending_info).strip()
    _pending_info = []
    if text:
        _emit({"type": "info", "text": text})


def info(text):
    _pending_info.append(str(text))
    if len(_pending_info) >= 25:
        flush_info()


def _protocol_print(*args, sep=" ", end="\n", file=None, flush=False):
    if file not in (None, sys.stdout, _real_stdout):
        return builtins.__orig_print__(*args, sep=sep, end=end, file=file, flush=flush)
    text = sep.join(str(a) for a in args)
    if text.strip():
        info(text)


def install_print_hook():
    """print() у коді сайту -> повідомлення "info" (а не сміття в протоколі)."""
    if INTERACTIVE:
        return
    if not hasattr(builtins, "__orig_print__"):
        builtins.__orig_print__ = builtins.print
    builtins.print = _protocol_print


def ask_choice(text, options=None, allow_text=False):
    """
    Поставити питання. options — [(value, label), ...] або None (тільки вільна відповідь).
    Повертає ("opt", value) | ("text", рядок) | (None, None) — пропущено.
    value може бути будь-яким об'єктом: назовні передається лише номер варіанта.
    """
    global _ask_counter
    options = list(options or [])
    allow_text = bool(allow_text or not options)
    if INTERACTIVE:
        p = builtins.__dict__.get("__orig_print__", builtins.print)
        for i, (_, label) in enumerate(options, 1):
            p(f"   {i}. {label}")
        while True:
            v = input(f"→ {text}: ").strip()
            if not v:
                return None, None
            if options and v.isdigit() and 1 <= int(v) <= len(options):
                return "opt", options[int(v) - 1][0]
            if allow_text:
                return "text", v
            p(f"   Введіть число від 1 до {len(options)}")

    _ask_counter += 1
    context = "\n".join(_pending_info).strip()
    _pending_info.clear()
    full = (context + "\n\n" + text).strip() if context else text
    _emit({"type": "ask", "id": _ask_counter, "text": full,
           "options": [str(label) for _, label in options] or None,
           "allow_text": allow_text})
    line = sys.stdin.readline()
    if not line:
        return None, None
    try:
        msg = json.loads(line)
    except ValueError:
        return None, None
    if "choice" in msg and isinstance(msg["choice"], int) and 0 <= msg["choice"] < len(options):
        return "opt", options[msg["choice"]][0]
    if msg.get("text") not in (None, "") and allow_text:
        return "text", str(msg["text"])
    return None, None


def ask_text(text):
    """Вільна відповідь. Повертає рядок або None (пропущено)."""
    kind, val = ask_choice(text, None, True)
    return val if kind == "text" else None


def result(ok, text, url=None, id=None, draft=False, **extra):
    flush_info()
    _emit({"type": "result", "ok": bool(ok), "text": text, "url": url,
           "id": str(id) if id is not None else None, "draft": bool(draft), **extra})
