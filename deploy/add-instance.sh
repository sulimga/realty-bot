#!/usr/bin/env bash
# Окремий слот бота для іншої людини: свій Telegram-бот, свої cookie, дані, журнал і оновлення.
# Головний бот і його дані не зачіпаються.
#
# Запуск (від root):  sudo bash /opt/realty-bot/deploy/add-instance.sh <ім'я>
#   або з головного бота командою /newbot (він передає дані через stdin).
#   stdin, рядок 1 — токен нового бота від @BotFather (інтерактивно питає, не показуючи на екрані);
#   stdin, рядок 2 — Telegram id власника (порожньо — власником стане той, хто першим надішле /start).
#
# Створює:
#   /opt/realty-bot-<ім'я>              код (свій git; оновлюється командою /update у СВОЄМУ боті)
#   /var/lib/realty-bot-<ім'я>          дані (cookie, журнал, звіти)
#   /etc/realty-bot-<ім'я>.env          TELEGRAM_TOKEN, REALTY_DATA_DIR, REALTY_INSTANCE, [OWNER_ID]
#   realty-bot-<ім'я>.service           сервіс systemd
# Видалення: deploy/remove-instance.sh <ім'я>
set -euo pipefail

NAME="${1:-}"
if ! [[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,19}$ ]]; then
  echo "Ім'я слота — латиницею малими літерами, цифри, дефіс (до 20 символів), напр.: anna"
  exit 2
fi
[ "$(id -u)" = 0 ] || { echo "Потрібні права root (sudo)."; exit 2; }

SRC="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(stat -c %U "$SRC")"
CODE="/opt/realty-bot-$NAME"
DATA="/var/lib/realty-bot-$NAME"
ENVF="/etc/realty-bot-$NAME.env"
SVC="realty-bot-$NAME"
UNIT="/etc/systemd/system/$SVC.service"

for p in "$CODE" "$ENVF" "$UNIT"; do
  if [ -e "$p" ]; then
    echo "Слот «$NAME» уже існує ($p). Оберіть інше ім'я або спершу видаліть слот (remove-instance.sh)."
    exit 3
  fi
done

# ---------- токен і власник
TOKEN="" OWNER=""
if [ -t 0 ]; then
  read -rsp "Токен нового бота від @BotFather: " TOKEN; echo
  read -rp "Telegram id власника (Enter — хто першим надішле /start): " OWNER
else
  IFS= read -r TOKEN || true
  IFS= read -r OWNER || true
fi
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
OWNER="$(printf '%s' "$OWNER" | tr -d '[:space:]')"
if ! [[ "$TOKEN" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]]; then
  echo "Це не схоже на токен бота (вигляд: 123456789:AAE…)."
  exit 2
fi
if [ -n "$OWNER" ] && ! [[ "$OWNER" =~ ^[0-9]{3,}$ ]]; then
  echo "Telegram id — лише цифри."
  exit 2
fi

# один і той самий бот не може працювати у двох слотах (Telegram віддає повідомлення лише одному)
for f in /etc/realty-bot.env /etc/realty-bot-*.env; do
  [ -f "$f" ] || continue
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      TELEGRAM_TOKEN=*)
        val="${line#TELEGRAM_TOKEN=}"
        if [ "${val%%:*}" = "${TOKEN%%:*}" ]; then
          echo "Цей бот уже працює в іншому слоті ($f). Створіть НОВОГО бота в @BotFather."
          exit 3
        fi ;;
    esac
  done < "$f"
done

# перевіряємо токен у Telegram (токен іде через змінну середовища, не в аргументах команди)
if ! BOT_USER="$(TOKEN="$TOKEN" python3 - <<'PY'
import json, os, sys, urllib.request
try:
    url = "https://api.telegram.org/bot" + os.environ["TOKEN"] + "/getMe"
    r = json.load(urllib.request.urlopen(url, timeout=20))
except Exception:
    sys.exit(1)
if not r.get("ok"):
    sys.exit(1)
print(r["result"].get("username") or "")
PY
)"; then
  echo "Telegram не прийняв токен — перевірте, що скопійовано повністю."
  exit 2
fi
echo "Бот: @$BOT_USER"

# ---------- створення (з цього моменту при помилці прибираємо все створене)
DATA_EXISTED=0
[ -d "$DATA" ] && DATA_EXISTED=1
cleanup() {
  rc=$?
  [ $rc -eq 0 ] && return
  echo "❌ Не вдалося — прибираю створене."
  systemctl disable --now "$SVC" >/dev/null 2>&1 || true
  rm -f "$UNIT"
  systemctl daemon-reload || true
  rm -rf "$CODE"
  rm -f "$ENVF"
  [ "$DATA_EXISTED" = 1 ] || rm -rf "$DATA"
}
trap cleanup EXIT

REPO="$(sudo -u "$RUN_USER" git -C "$SRC" remote get-url origin)"
echo "▶ Код → $CODE"
mkdir -p "$CODE"
chown "$RUN_USER:$RUN_USER" "$CODE"
sudo -u "$RUN_USER" git clone --quiet "$REPO" "$CODE"

echo "▶ Python-середовище (1–3 хв)…"
sudo -H -u "$RUN_USER" python3 -m venv "$CODE/.venv"
sudo -H -u "$RUN_USER" "$CODE/.venv/bin/pip" install --quiet --upgrade pip
sudo -H -u "$RUN_USER" "$CODE/.venv/bin/pip" install --quiet -r "$CODE/requirements.txt"

echo "▶ Дані → $DATA"
[ "$DATA_EXISTED" = 1 ] && echo "  (тека вже була — використовую наявні дані)"
mkdir -p "$DATA"
chown -R "$RUN_USER:$RUN_USER" "$DATA"
chmod 750 "$DATA"

echo "▶ Налаштування → $ENVF"
(
  umask 077
  {
    echo "TELEGRAM_TOKEN=$TOKEN"
    echo "REALTY_DATA_DIR=$DATA"
    echo "REALTY_INSTANCE=$NAME"
    if [ -n "$OWNER" ]; then echo "OWNER_ID=$OWNER"; fi
  } > "$ENVF"
)
chgrp "$RUN_USER" "$ENVF"
chmod 640 "$ENVF"

echo "▶ Сервіс $SVC"
cat > "$UNIT" <<EOF
[Unit]
Description=Realty bot (слот $NAME)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$CODE
EnvironmentFile=$ENVF
ExecStart=$CODE/.venv/bin/python bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now "$SVC" 2>&1 | grep -v '^Created symlink' || true
sleep 6
if ! systemctl is-active --quiet "$SVC"; then
  echo "Сервіс не запустився. Останні рядки журналу:"
  journalctl -u "$SVC" -n 15 --no-pager 2>&1 | sed -E 's/[0-9]{5,}:[A-Za-z0-9_-]{30,}/<токен>/g' || true
  exit 1
fi

trap - EXIT
echo
echo "✅ Слот «$NAME» працює: @$BOT_USER"
if [ -n "$OWNER" ]; then
  echo "Власник: Telegram id $OWNER — бот слухається лише його."
else
  echo "Власником стане той, хто ПЕРШИМ надішле /start боту @$BOT_USER — нехай майбутній власник зробить це одразу."
fi
echo "Оновлення коду — командою /update у цьому боті (окремо від головного)."
