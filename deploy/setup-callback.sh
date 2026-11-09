#!/usr/bin/env bash
# Приймальна HTTPS-адреса для OAuth-повернення (OLX «Дозволити») на цьому сервері.
# Запуск (від root):  sudo bash /opt/realty-bot/deploy/setup-callback.sh [IPv4]
#   або з головного бота командою /callback. Можна запускати повторно — нічого не ламає.
#
# Що робить:
#   1. Визначає публічну IP і складає адресу  <ip-з-дефісами>.sslip.io  (безкоштовний DNS з реальним HTTPS-сертифікатом).
#   2. Ставить Caddy (сам отримує і оновлює сертифікат Let's Encrypt) — назовні лише /olx/callback і /olx/health.
#   3. Запускає сервіс realty-callback (common/oauth_callback.py, слухає тільки 127.0.0.1:8099).
#   4. Відкриває порти 80/443 у локальному фаєрволі (iptables / ufw).
#   5. Пише /var/lib/realty-oauth/host.txt (адреса для слотів) і каталог обміну кодами.
# ЗОВНІШНІЙ крок, який скрипт зробити не може: у панелі Oracle Cloud відкрити Ingress 80 і 443 (VCN → Security List).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

[ "$(id -u)" = 0 ] || { echo "Потрібні права root (sudo)."; exit 2; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(stat -c %U "$SRC")"
SPOOL=/var/lib/realty-oauth
PORT=8099

# ---------- 1. адреса
IP="${1:-}"
if [ -z "$IP" ]; then
  IP="$(curl -4 -fsS -m 10 https://api.ipify.org 2>/dev/null || true)"
fi
if ! [[ "$IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "Не вдалося визначити публічну IP. Запустіть із IP: sudo bash $0 1.2.3.4"
  exit 2
fi
HOST="${IP//./-}.sslip.io"
echo "Адреса: https://$HOST/olx/callback"

# ---------- 2. Caddy
if ! command -v caddy >/dev/null 2>&1; then
  echo "Ставлю Caddy…"
  apt-get update -y
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
  curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' -o /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi
cat > /etc/caddy/Caddyfile <<EOF
$HOST {
	encode gzip
	@cb path /olx/callback /olx/health
	handle @cb {
		reverse_proxy 127.0.0.1:$PORT
	}
	handle {
		respond "Not found" 404
	}
}
EOF

# ---------- 5. каталог обміну і адреса для слотів
mkdir -p "$SPOOL"
chown "$RUN_USER":"$RUN_USER" "$SPOOL"
chmod 700 "$SPOOL"
printf '%s\n' "$HOST" > "$SPOOL/host.txt"
chown "$RUN_USER":"$RUN_USER" "$SPOOL/host.txt"
chmod 644 "$SPOOL/host.txt"

# ---------- 3. сервіс
PY="$SRC/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
cat > /etc/systemd/system/realty-callback.service <<EOF
[Unit]
Description=Realty bot: OAuth callback (OLX)
After=network-online.target

[Service]
User=$RUN_USER
Environment=OAUTH_SPOOL=$SPOOL
Environment=OAUTH_PORT=$PORT
ExecStart=$PY $SRC/common/oauth_callback.py
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$SPOOL
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now realty-callback
systemctl restart realty-callback

# ---------- 4. фаєрвол (на образах Oracle Ubuntu порти за замовчуванням закриті правилами iptables)
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  echo "ufw: 80 і 443 відкрито."
fi
if command -v iptables >/dev/null 2>&1; then
  CHANGED=0
  for P in 80 443; do
    if ! iptables -C INPUT -p tcp --dport "$P" -j ACCEPT 2>/dev/null; then
      POS="$(iptables -L INPUT --line-numbers -n | awk '/REJECT/ {print $1; exit}')"
      if [ -n "$POS" ]; then iptables -I INPUT "$POS" -p tcp --dport "$P" -j ACCEPT
      else iptables -A INPUT -p tcp --dport "$P" -j ACCEPT; fi
      CHANGED=1
    fi
  done
  if [ "$CHANGED" = 1 ]; then
    if command -v netfilter-persistent >/dev/null 2>&1; then netfilter-persistent save >/dev/null 2>&1 || true
    elif [ -d /etc/iptables ]; then iptables-save > /etc/iptables/rules.v4; fi
    echo "iptables: 80 і 443 відкрито й збережено."
  fi
fi

systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy

# ---------- перевірка
sleep 2
if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/olx/health")" != 200 ]; then
  echo "❌ Сервіс realty-callback не відповідає. Лог: journalctl -u realty-callback -n 30"
  exit 1
fi
echo "✅ Локальний сервіс працює."
OK=0
for i in 1 2 3 4 5 6; do   # сертифікат видається за 10–40 с після відкриття портів
  if [ "$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$HOST/olx/health" || true)" = 200 ]; then OK=1; break; fi
  sleep 8
done
if [ "$OK" = 1 ]; then
  echo "✅ Готово: https://$HOST/olx/callback відповідає ззовні з HTTPS."
else
  echo "⚠️ Ззовні адреса поки не відповідає. Найімовірніше, у панелі Oracle не відкриті порти:"
  echo "   Networking → Virtual Cloud Networks → ваша VCN → Security Lists → Default → Add Ingress Rules:"
  echo "   Source CIDR 0.0.0.0/0, протокол TCP, Destination Port 80 — і окремо 443. Потім запустіть цей крок ще раз."
  exit 1
fi
