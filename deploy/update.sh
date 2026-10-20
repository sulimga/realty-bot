#!/usr/bin/env bash
# Оновлення коду з GitHub (викликається командою /update у боті).
# Якщо новий код не компілюється — повертаємо попередню версію, бот не ламається.
set -uo pipefail
cd "$(dirname "$0")/.."
OLD=$(git rev-parse HEAD)
git fetch --quiet origin main || { echo "Не вдалося звернутися до GitHub"; exit 1; }
NEW=$(git rev-parse origin/main)
if [ "$OLD" = "$NEW" ]; then echo "Код уже найновіший ($(git log -1 --format='%h %s'))"; exit 1; fi
git reset --quiet --hard origin/main
echo "Зміни:"; git log --format='  %h %s' "$OLD..$NEW" | head -20
.venv/bin/pip install --quiet -r requirements.txt || { echo "pip install не вдався — відкат"; git reset --quiet --hard "$OLD"; exit 1; }
if ! .venv/bin/python -m py_compile bot/bot.py dispatcher/dispatcher.py common/board_protocol.py boards/*/*.py crm/*.py; then
  echo "Новий код з помилкою синтаксису — відкат на $OLD"; git reset --quiet --hard "$OLD"; exit 1
fi
echo "Оновлено до $(git log -1 --format='%h')"
