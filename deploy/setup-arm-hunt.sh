#!/usr/bin/env bash
# Служба realty-armhunt: тестовий сервер сам шукає вільну ARM-машину Oracle (deploy/arm_hunt.py), поки не створить.
# Викликається командою бота /armhunt (root через `sudo -n`). Повторний запуск безпечний.
#   setup-arm-hunt.sh [start]  — поставити OCI SDK, перевірити права, запустити службу
#   setup-arm-hunt.sh status   — стан пошуку
#   setup-arm-hunt.sh stop     — зупинити й вимкнути службу
# Права в Oracle (ставляться ОДИН раз у Cloud Shell, не тут): динамічна група realty-arm-hunter (саме ця машина)
# і політика realty-arm-hunter (створювати машини, без видалення). Після створення машини — видалити обидві.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV=/opt/realty-armhunt
STATE=/var/lib/realty-armhunt
UNIT=/etc/systemd/system/realty-armhunt.service
ENVF="${ARMHUNT_ENV:-/etc/realty-bot.env}"
RUN_AS="$(stat -c %U "$ROOT")"

status() {
  local act; act="$(systemctl is-active realty-armhunt 2>/dev/null)"
  echo "Служба: ${act:-немає}"
  if [ -f "$STATE/state.json" ]; then
    python3 - "$STATE/state.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"Шукає з {s.get('started', '?')}: циклів {s.get('cycles', 0)}, спроб {s.get('attempts', 0)}")
print(f"Останнє: {s.get('last', '?')} — {s.get('last_result', '?')}")
if s.get("last_error"):
    print(f"Остання помилка ({s.get('last_error_at', '?')}): {s['last_error'][:200]}")
if s.get("done"):
    print(f"✅ Створено {s['done']}: {s.get('shape', '?')}, IP {s.get('ip', '?')}")
PY
  fi
  journalctl -u realty-armhunt -n 4 --no-pager -o cat 2>/dev/null | sed 's/^/  │ /'
}

case "${1:-start}" in
  status) status; exit 0 ;;
  stop)   systemctl disable --now realty-armhunt 2>/dev/null; echo "⏹ Пошук зупинено."; status; exit 0 ;;
  start)  ;;
  *) echo "Невідома дія: $1 (start | status | stop)"; exit 1 ;;
esac

[ "$(id -u)" = 0 ] || { echo "Потрібен root"; exit 1; }
[ -f "$STATE/done" ] && { echo "✅ Машину вже створено — пошук не потрібен."; status; exit 0; }

if ! "$VENV/bin/python" -c 'import oci' 2>/dev/null; then
  echo "▶ Ставлю OCI SDK (на цій машині 2–5 хв)…"
  python3 -m venv "$VENV" && "$VENV/bin/pip" install --quiet --upgrade pip \
    && "$VENV/bin/pip" install --quiet oci || { echo "❌ Не вдалося встановити OCI SDK"; exit 1; }
fi
echo "▶ Перевіряю доступ до Oracle"
"$VENV/bin/python" "$ROOT/deploy/arm_hunt.py" --check || exit 1

mkdir -p "$STATE"; chown "$RUN_AS:$RUN_AS" "$STATE"; chmod 700 "$STATE"
cat > "$UNIT" <<EOF
[Unit]
Description=Realty: пошук вільної ARM-машини Oracle (realty-multi-arm)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$ROOT
EnvironmentFile=-$ENVF
Environment=ARMHUNT_STATE=$STATE
ExecStart=$VENV/bin/python -u $ROOT/deploy/arm_hunt.py
Restart=on-failure
RestartSec=120
Nice=10
MemoryHigh=200M
MemoryMax=300M

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable realty-armhunt >/dev/null 2>&1
systemctl restart realty-armhunt
sleep 5
echo "▶ Пошук запущено: раз на ~1,5 хв, сповіщення — в цей бот, коли машину створено."
status
