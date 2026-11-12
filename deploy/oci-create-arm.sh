#!/usr/bin/env bash
# Створює ARM-машину (Ampere A1, Always Free) для переїзду мультибота — запускати в Oracle Cloud Shell:
#   curl -fsSL https://raw.githubusercontent.com/sulimga/realty-bot/<коміт>/deploy/oci-create-arm.sh | bash
#
# Що робить (нічого не видаляє й не чіпає наявні машини):
#   1. ключ ~/.ssh/realty_arm у Cloud Shell (приватна частина лишається тут) — щоб потім зайти на нову машину;
#   2. показує, скільки місця займають диски (безкоштовно 200 ГБ на всі);
#   3. бере ту саму підмережу, що в машини realty-multi (там уже відкриті 80/443), образ Ubuntu 24.04 для ARM;
#   4. створює realty-multi-arm: VM.Standard.A1.Flex, 2 OCPU, 3 ГБ (щоб пам'ять була ≥ 20% і машину не вважали
#      простоєм), диск 50 ГБ, публічна IP; cloud-init лише готує систему (часовий пояс, swap, пакети) — бота НЕ ставить;
#   5. немає вільних потужностей — пробує 1 ядро / 3 ГБ і інші availability domain; не вийшло — пише, що спробувати пізніше.
# Повторний запуск безпечний: якщо realty-multi-arm уже є — просто показує її IP.
# Те саме запускає GitHub Actions (.github/workflows/oci-arm.yml) кожні 10 хв: тоді OCI_TENANCY береться з конфіга,
# публічний ключ — з SSH_PUBKEY_FILE (deploy/realty_arm.pub — та сама пара, що лежить у Cloud Shell), IP і OCID
# ховаються з журналу (репозиторій публічний), а про успіх — повідомлення в Telegram (TG_TOKEN/TG_CHAT, якщо задані).
# Коди виходу: 0 — машина є/створена, 2 — немає потужностей, 1 — інша помилка.
set -uo pipefail
NAME=realty-multi-arm
T="${OCI_TENANCY:?немає OCI_TENANCY — це треба запускати в Oracle Cloud Shell}"
q() { oci "$@" 2>/dev/null; }
mask() { [ -n "${GITHUB_ACTIONS:-}" ] && [ -n "$1" ] && echo "::add-mask::$1"; return 0; }
mask "$T"
notify() {
  [ -n "${TG_TOKEN:-}" ] && [ -n "${TG_CHAT:-}" ] || return 0
  curl -s -o /dev/null --max-time 20 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
       --data-urlencode "chat_id=${TG_CHAT}" --data-urlencode "text=$1" || true
}

echo "▶ Ключ для входу на нову машину"
if [ -n "${SSH_PUBKEY_FILE:-}" ]; then
  PUB="$SSH_PUBKEY_FILE"
  [ -f "$PUB" ] || { echo "❌ Немає $PUB"; exit 1; }
  echo "  $PUB (приватна частина — у Cloud Shell, ~/.ssh/realty_arm)"
else
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  # Cloud Shell працює в режимі FIPS — ed25519 там заборонений, тому RSA 4096
  [ -f ~/.ssh/realty_arm ] || ssh-keygen -q -t rsa -b 4096 -N "" -C realty-arm -f ~/.ssh/realty_arm
  [ -f ~/.ssh/realty_arm.pub ] || { echo "❌ Не вдалося створити ключ ~/.ssh/realty_arm"; exit 1; }
  PUB=~/.ssh/realty_arm.pub
  echo "  ~/.ssh/realty_arm (лишається в Cloud Shell)"
fi

EXIST="$(q compute instance list -c "$T" --display-name "$NAME" --query 'data[?"lifecycle-state"!=`TERMINATED`] | [0].id' --raw-output)"
if [ -n "$EXIST" ] && [ "$EXIST" != "null" ]; then
  mask "$EXIST"
  IP="$(q compute instance list-vnics --instance-id "$EXIST" --query 'data[0]."public-ip"' --raw-output)"
  mask "$IP"
  echo "✅ $NAME уже існує: $IP"
  exit 0
fi

ADS="$(q iam availability-domain list -c "$T" --query 'data[].name' --raw-output | tr -d '[]", ' | grep .)"
echo "▶ Диски (безкоштовно 200 ГБ на всі)"
TOTAL=0
for AD in $ADS; do
  for GB in $(q bv boot-volume list -c "$T" --availability-domain "$AD" --query 'data[?"lifecycle-state"!=`TERMINATED`]."size-in-gbs"' --raw-output | tr -d '[], ' | grep .); do
    TOTAL=$((TOTAL + GB))
  done
done
for GB in $(q bv volume list -c "$T" --query 'data[?"lifecycle-state"!=`TERMINATED`]."size-in-gbs"' --raw-output | tr -d '[], ' | grep .); do
  TOTAL=$((TOTAL + GB))
done
echo "  зайнято: ${TOTAL} ГБ; нова машина: 50 ГБ → разом $((TOTAL + 50)) ГБ"
if [ $((TOTAL + 50)) -gt 200 ]; then
  echo "❌ Вийде понад 200 ГБ — за зайве Oracle візьме гроші (або не дасть створити). Зупиняюсь."
  exit 1
fi

MULTI="$(q compute instance list -c "$T" --display-name realty-multi --lifecycle-state RUNNING --query 'data[0].id' --raw-output)"
SUBNET="$(q compute instance list-vnics --instance-id "$MULTI" --query 'data[0]."subnet-id"' --raw-output)"
mask "$MULTI"; mask "$SUBNET"
[ -n "$SUBNET" ] && [ "$SUBNET" != "null" ] || { echo "❌ Не знайшов підмережу машини realty-multi"; exit 1; }
IMG="$(q compute image list -c "$T" --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
        --shape VM.Standard.A1.Flex --sort-by TIMECREATED --sort-order DESC \
        --query 'data[?!contains("display-name", `Minimal`)] | [0].id' --raw-output)"
IMGNAME="$(q compute image get --image-id "$IMG" --query 'data."display-name"' --raw-output)"
[ -n "$IMG" ] && [ "$IMG" != "null" ] || { echo "❌ Не знайшов образ Ubuntu 24.04 для ARM"; exit 1; }
echo "▶ Образ: $IMGNAME"

PREP="${TMPDIR:-/tmp}/realty-arm-prep.yaml"
[ -z "${GITHUB_ACTIONS:-}" ] && PREP=~/realty-arm-prep.yaml
cat > "$PREP" <<'EOF'
#cloud-config
# realty-multi-arm: лише підготовка системи під переїзд мультибота (бота й секретів тут немає)
runcmd:
  - [bash, -c, "timedatectl set-timezone Europe/Kiev || true"]
  - [bash, -c, "if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab; fi"]
  - [bash, -c, "apt-get -o DPkg::Lock::Timeout=600 update -y && apt-get -o DPkg::Lock::Timeout=600 install -y git python3-venv python3-pip rsync > /var/log/realty-prep.log 2>&1; echo PREP OK >> /var/log/realty-prep.log"]
EOF

# спершу 2 ядра / 3 ГБ, якщо місця нема — 1 ядро / 3 ГБ (менші машини частіше знаходять місце; ядра можна додати пізніше)
for AD in $ADS; do
  for SHAPE in '{"ocpus": 2, "memoryInGBs": 3}' '{"ocpus": 1, "memoryInGBs": 3}'; do
    EXIST="$(q compute instance list -c "$T" --display-name "$NAME" --query 'data[?"lifecycle-state"!=`TERMINATED`] | [0].id' --raw-output)"
    if [ -n "$EXIST" ] && [ "$EXIST" != "null" ]; then
      echo "⚠️ $NAME уже з'явилась ($EXIST) — другу не створюю. Перевірте в Compute → Instances."; exit 1
    fi
    echo "▶ Створюю $NAME у $AD, $SHAPE (до 5 хв)…"
    OUT="$(oci compute instance launch -c "$T" --availability-domain "$AD" --shape VM.Standard.A1.Flex \
          --shape-config "$SHAPE" --image-id "$IMG" --subnet-id "$SUBNET" \
          --assign-public-ip true --display-name "$NAME" --boot-volume-size-in-gbs 50 \
          --ssh-authorized-keys-file "$PUB" --user-data-file "$PREP" --no-retry \
          --wait-for-state RUNNING --max-wait-seconds 600 --query 'data.id' --raw-output 2>&1)"
    ID="$(printf '%s\n' "$OUT" | grep -o 'ocid1\.instance[^" ]*' | head -1)"
    if [ -n "$ID" ]; then
      mask "$ID"
      IP="$(q compute instance list-vnics --instance-id "$ID" --query 'data[0]."public-ip"' --raw-output)"
      mask "$IP"
      echo "✅ Готово: $NAME працює ($SHAPE), публічна IP: $IP"
      notify "✅ Oracle: ARM-машину $NAME створено ($SHAPE). IP: $IP. Напиши в чат проєкту «машина є» — далі встановлення і переїзд."
      echo "   Вхід з Cloud Shell: ssh -i ~/.ssh/realty_arm ubuntu@$IP"
      exit 0
    fi
    if printf '%s' "$OUT" | grep -qi "capacity"; then
      echo "  немає вільних ARM-потужностей"
      continue
    fi
    echo "❌ Помилка (не «немає потужностей») — зупиняюсь, щоб не створити зайвого:"
    printf '%s\n' "$OUT" | grep -iE '"message"|"code"|error|fail' | sed -E 's/ocid1\.[A-Za-z0-9._-]+/<ocid>/g' | head -8 | sed 's/^/   /'
    exit 1
  done
done
echo "❌ Зараз ARM-машин немає в жодному AD. Це звично для Oracle — запустіть ще раз за 10–60 хв."
exit 2
