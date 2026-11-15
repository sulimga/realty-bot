#!/usr/bin/env bash
# Разове перемикання ТЕСТОВОГО бота з публічного realty-bot на приватний realty-bot-multi, гілка test (15.11.2026).
# Викликає команда бота «/switchrepo go». Доступ до приватного репозиторію — ключ доступу (deploy key, лише читання),
# який бот створює командою «/switchrepo» (~/.ssh/realty_multi_deploy); власник додає його відкриту частину на GitHub:
# sulimga/realty-bot-multi → Settings → Deploy keys. Секретів у чаті немає.
# Якщо щось не так — повертаємо як було (адреса репозиторію, гілка, код). Код виходу 0 — перемкнено.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
NEW_URL="${SWITCH_URL:-git@github-realty-multi:sulimga/realty-bot-multi.git}"   # SWITCH_URL — лише для тесту
BRANCH=test
OLD=$(git rev-parse HEAD)
OLD_URL=$(git remote get-url origin)
OLD_BRANCH=$(git symbolic-ref --short -q HEAD || echo main)

rollback() {
  echo "↩️ Повертаю як було: $OLD_URL, гілка $OLD_BRANCH, $OLD"
  git remote set-url origin "$OLD_URL"
  git checkout -q -f -B "$OLD_BRANCH" "$OLD"
  exit 1
}

echo "Перевіряю доступ до realty-bot-multi…"
if ! timeout 60 git ls-remote "$NEW_URL" "refs/heads/$BRANCH" | grep -q "refs/heads/$BRANCH"; then
  echo "Немає доступу до гілки $BRANCH у realty-bot-multi. Чи додано ключ у Deploy keys репозиторію?"
  exit 1
fi
git remote set-url origin "$NEW_URL"
git fetch --quiet origin "$BRANCH" || { echo "Не вдалося завантажити гілку $BRANCH"; rollback; }
git checkout -q -f -B "$BRANCH" "origin/$BRANCH" || { echo "Не вдалося перемкнути код"; rollback; }
git branch -q --set-upstream-to="origin/$BRANCH" "$BRANCH" 2>/dev/null || true
echo "Код: $(git log -1 --format='%h %s')"
.venv/bin/pip install --quiet -r requirements.txt || { echo "pip install не вдався"; rollback; }
if ! .venv/bin/python -m py_compile bot/bot.py dispatcher/dispatcher.py common/*.py boards/*/*.py crm/*.py; then
  echo "Новий код з помилкою синтаксису"; rollback
fi
# пробний імпорт бота (на тимчасовій теці даних, справжні дані не чіпаємо) — щоб не впасти після перезапуску
TMPD=$(mktemp -d)
if ! REALTY_DATA_DIR="$TMPD" TELEGRAM_TOKEN=x timeout 60 .venv/bin/python -c \
     "import sys; sys.path.insert(0, 'bot'); import bot" >/dev/null 2>"$TMPD/err"; then
  echo "Новий бот не запускається:"; tail -5 "$TMPD/err"; rm -rf "$TMPD"; rollback
fi
rm -rf "$TMPD"
for S in realty-callback realty-browser; do
  if systemctl is-enabled --quiet "$S" 2>/dev/null; then
    sudo -n systemctl restart "$S" 2>/dev/null && echo "Перезапущено службу $S" || echo "Службу $S не перезапущено (немає sudo)"
  fi
done
echo "✅ Перемкнено: realty-bot-multi, гілка $BRANCH ($ROOT)"
