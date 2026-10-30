"""
Диспетчер: знає, які є сайти (boards.json), запускає програму потрібного сайту
окремим процесом, пересилає її повідомлення й питання і повертає результат.

Сам нічого не публікує і не знає, як влаштований жоден сайт.
Якщо програма сайту впала чи зависла — зупиняє її і повертає помилку;
інші сайти й бот від цього не страждають.
"""

import asyncio
import json
import os
import re
import signal
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("REALTY_DATA_DIR", os.path.join(ROOT, "data"))
REGISTRY = os.path.join(ROOT, "dispatcher", "boards.json")
ASK_TIMEOUT = 15 * 60          # скільки чекати відповіді на питання, сек
JOURNAL = os.path.join(DATA, "journal.jsonl")
FRAME_RE = re.compile(r'File "([^"]+)", line (\d+) in (\S+)')
NET_HINTS = ("ssl.py", "socket.py", "/urllib3/", "/http/client.py", "/requests/", "curl_cffi")


def _is_project_file(path):
    p = os.path.abspath(path)
    return p.startswith(ROOT + os.sep) and "/.venv/" not in p and "site-packages" not in p


def where_stuck(stack_text):
    """З дампу faulthandler (найсвіжіший виклик першим) — коротко: де зависла програма."""
    frames = FRAME_RE.findall(stack_text or "")
    if not frames:
        return ""
    own = [f"{os.path.basename(f)}:{ln} {fn}" for f, ln, fn in frames if _is_project_file(f)][:3]
    inner = frames[0][0]
    if not _is_project_file(inner) and any(h in inner for h in NET_HINTS):
        own.insert(0, "чекала відповіді сайту")
    return " ← ".join(own)


async def _dump_stack(proc, stderr_path, start_pos):
    """Попросити програму записати свій стек у stderr-лог (SIGUSR1 → faulthandler) і прочитати його."""
    if not hasattr(signal, "SIGUSR1"):
        return ""
    try:
        proc.send_signal(signal.SIGUSR1)
        await asyncio.sleep(1.5)
        with open(stderr_path, encoding="utf-8", errors="replace") as f:
            f.seek(start_pos)
            return where_stuck(f.read())
    except (ProcessLookupError, OSError):
        return ""


def boards(enabled_only=True):
    with open(REGISTRY, encoding="utf-8") as f:
        reg = json.load(f)
    return {k: v for k, v in reg.items() if v.get("enabled") or not enabled_only}


def journal(entry):
    os.makedirs(DATA, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def last_journal(n=10):
    if not os.path.exists(JOURNAL):
        return []
    with open(JOURNAL, encoding="utf-8") as f:
        lines = f.readlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


async def run(board_key, argv, on_info, on_ask):
    """
    Запускає програму сайту: python <dir>/<entry> <argv...>
      on_info(text)          — корутина, повідомлення про хід роботи
      on_ask(ask_dict) -> answer_dict  — корутина, питання; повертає {"choice": i} / {"text": s} / {}
    Повертає dict результату: {"ok", "text", "url", "id", "draft"}.
    """
    reg = boards(enabled_only=False)
    if board_key not in reg:
        return {"ok": False, "text": f"Невідомий сайт: {board_key}"}
    b = reg[board_key]
    workdir = os.path.join(ROOT, b["dir"])
    entry = os.path.join(workdir, b["entry"])
    if not os.path.exists(entry):
        return {"ok": False, "text": f"{b['title']}: програму ще не встановлено ({b['dir']}/{b['entry']})"}

    os.makedirs(os.path.join(DATA, "logs"), exist_ok=True)
    stderr_path = os.path.join(DATA, "logs", f"{board_key}.stderr.log")
    env = dict(os.environ, REALTY_DATA_DIR=DATA, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")

    with open(stderr_path, "a", encoding="utf-8") as errf:
        errf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(argv)}\n")
        errf.flush()
        start_pos = errf.tell()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, entry, *argv, cwd=workdir, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=errf,
            limit=4 * 1024 * 1024)

        result = None
        deadline = time.monotonic() + 60 * float(b.get("timeout_min", 25))

        async def read_loop():
            nonlocal result, deadline
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    await on_info(line.decode("utf-8", "replace").rstrip())
                    continue
                t = msg.get("type")
                if t == "info":
                    await on_info(msg.get("text", ""))
                elif t == "ask":
                    asked_at = time.monotonic()
                    try:
                        ans = await asyncio.wait_for(on_ask(msg), ASK_TIMEOUT)
                    except asyncio.TimeoutError:
                        ans = {}
                        await on_info("⏱ Відповіді не було — питання пропущено")
                    deadline += time.monotonic() - asked_at      # час на відповідь не рахуємо
                    reply = {"id": msg.get("id"), **(ans or {})}
                    proc.stdin.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                    await proc.stdin.drain()
                elif t == "result":
                    result = msg

        try:
            # питання не рахуються в таймаут: користувач міг відійти
            reader = asyncio.ensure_future(read_loop())
            while not reader.done():
                await asyncio.wait({reader}, timeout=5)
                if time.monotonic() > deadline and not reader.done():
                    stuck = await _dump_stack(proc, stderr_path, start_pos)
                    proc.kill()
                    reader.cancel()
                    mins = int(float(b.get("timeout_min", 25)))
                    result = {"ok": False, "text": f"{b['title']}: програма працювала задовго ({mins} хв) — зупинено"
                                                   + (f"\nЗависла на: {stuck}" if stuck else "")}
                    break
            await proc.wait()
        except asyncio.CancelledError:
            proc.kill()
            raise

    if result is None:
        tail = ""
        try:
            with open(stderr_path, encoding="utf-8") as f:
                tail = f.read()[-800:]
        except OSError:
            pass
        result = {"ok": False, "text": f"{b['title']}: програма завершилась без результату "
                                       f"(код {proc.returncode}).\n{tail}"}
    result.setdefault("text", "")
    journal({"board": board_key, "argv": argv, "ok": result.get("ok"), "text": result.get("text"),
             "id": result.get("id"), "url": result.get("url"), "draft": result.get("draft")})
    return result
