#!/usr/bin/env bash
# Видалити слот, створений add-instance.sh: зупиняє бота, прибирає сервіс, код і налаштування.
# Дані (cookie, журнал, звіти) за замовчуванням ЗАЛИШАЮТЬСЯ в /var/lib/realty-bot-<ім'я>.
# Головний бот (/opt/realty-bot, /var/lib/realty-bot) цим скриптом не чіпається.
#
# Запуск (від root):  sudo bash /opt/realty-bot/deploy/remove-instance.sh <ім'я> [--purge]
#   --purge — видалити й дані слота.
set -euo pipefail

NAME="${1:-}"
if ! [[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,19}$ ]]; then
  echo "Вкажіть ім'я слота (як при створенні)."
  exit 2
fi
[ "$(id -u)" = 0 ] || { echo "Потрібні права root (sudo)."; exit 2; }

CODE="/opt/realty-bot-$NAME"
DATA="/var/lib/realty-bot-$NAME"
ENVF="/etc/realty-bot-$NAME.env"
SVC="realty-bot-$NAME"
UNIT="/etc/systemd/system/$SVC.service"

if [ ! -e "$UNIT" ] && [ ! -e "$CODE" ] && [ ! -e "$ENVF" ]; then
  echo "Слота «$NAME» немає."
  [ -d "$DATA" ] && echo "(залишилися лише дані: $DATA)"
  exit 3
fi

systemctl disable --now "$SVC" >/dev/null 2>&1 || true
rm -f "$UNIT"
systemctl daemon-reload
rm -rf "$CODE"
rm -f "$ENVF"
echo "✅ Слот «$NAME» зупинено й видалено (сервіс, код, налаштування)."

if [ "${2:-}" = "--purge" ]; then
  rm -rf "$DATA"
  echo "Дані слота теж видалено."
elif [ -d "$DATA" ]; then
  echo "Дані залишено: $DATA (cookie, журнал). Повне видалення: remove-instance.sh $NAME --purge"
fi
