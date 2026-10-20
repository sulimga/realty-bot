#!/usr/bin/env bash
# Повна установка бота на чисту Ubuntu (Oracle Cloud). Запускається з cloud-init від root.
# Токен бота має бути в /etc/realty-bot.env (його пише cloud-init).
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

timedatectl set-timezone Europe/Kiev || true

# 1 ГБ пам'яті мало для pip install — додаємо swap
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

apt-get update -y
apt-get install -y git python3-venv python3-pip

grep -q '^REALTY_DATA_DIR=' /etc/realty-bot.env || echo 'REALTY_DATA_DIR=/var/lib/realty-bot' >> /etc/realty-bot.env
chmod 640 /etc/realty-bot.env
chgrp ubuntu /etc/realty-bot.env

[ -d /opt/realty-bot/.git ] || git clone https://github.com/sulimga/realty-bot.git /opt/realty-bot
mkdir -p /var/lib/realty-bot
chown -R ubuntu:ubuntu /opt/realty-bot /var/lib/realty-bot

sudo -u ubuntu python3 -m venv /opt/realty-bot/.venv
sudo -u ubuntu /opt/realty-bot/.venv/bin/pip install --quiet --upgrade pip
sudo -u ubuntu /opt/realty-bot/.venv/bin/pip install --quiet -r /opt/realty-bot/requirements.txt

cp /opt/realty-bot/deploy/realty-bot.service /etc/systemd/system/realty-bot.service
systemctl daemon-reload
systemctl enable --now realty-bot
echo "INSTALL OK"
