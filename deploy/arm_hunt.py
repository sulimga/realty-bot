#!/usr/bin/env python3
"""Пошук вільної ARM-машини Oracle (Ampere A1) з нашого ж сервера — поки не вийде.

Що робить: раз на ~1,5 хв пробує створити realty-multi-arm (усі availability domain; 2 ядра / 3 ГБ, якщо нема —
1 ядро / 3 ГБ) — те саме, що deploy/oci-create-arm.sh, але через OCI Python SDK і без ключів: сервер входить в Oracle
як «instance principal» (динамічна група realty-arm-hunter + політика realty-arm-hunter, створені в Cloud Shell;
політика забороняє видаляти машини). Коли машину створено — пише власнику в Telegram (токен тестового бота з
/etc/realty-bot.env, адресат — власник бота), ставить позначку done і більше не пробує.

Запуск: служба realty-armhunt (deploy/setup-arm-hunt.sh, команда бота /armhunt). Перевірка без створення:
  arm_hunt.py --check
Стан: $ARMHUNT_STATE/state.json (кількість спроб, останній результат), $ARMHUNT_STATE/done (коли створено).
OCID і IP у журнал не пишемо повністю (журнал можуть переслати в чат).
"""
import base64
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request

NAME = "realty-multi-arm"
SOURCE_VM = "realty-multi"               # беремо його підмережу (там відкриті 80/443)
SHAPE = "VM.Standard.A1.Flex"
SHAPES = [(2, 3), (1, 3)]               # (ядра, ГБ): 3 ГБ, щоб пам'ять була ≥ 20% і Oracle не вважав машину простоєм
BOOT_GB = 50
FREE_GB = 200                           # безкоштовно на всі диски
INTERVAL = int(os.environ.get("ARMHUNT_INTERVAL", "90"))
STATE_DIR = os.environ.get("ARMHUNT_STATE", "/var/lib/realty-armhunt")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBKEY_FILE = os.path.join(ROOT, "deploy", "realty_arm.pub")   # та сама пара, що в Cloud Shell (~/.ssh/realty_arm)

CLOUD_INIT = """#cloud-config
# realty-multi-arm: лише підготовка системи під переїзд мультибота (бота й секретів тут немає)
runcmd:
  - [bash, -c, "timedatectl set-timezone Europe/Kiev || true"]
  - [bash, -c, "if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab; fi"]
  - [bash, -c, "apt-get -o DPkg::Lock::Timeout=600 update -y && apt-get -o DPkg::Lock::Timeout=600 install -y git python3-venv python3-pip rsync > /var/log/realty-prep.log 2>&1; echo PREP OK >> /var/log/realty-prep.log"]
"""


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def short(ocid):
    return (ocid or "")[-6:]


# ------------------------------------------------------------------ стан і сповіщення
def state_path(name):
    return os.path.join(STATE_DIR, name)


def load_state():
    try:
        with open(state_path("state.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = state_path("state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, state_path("state.json"))


def owner_chat():
    env = os.environ.get("OWNER_ID", "").strip()
    if env.isdigit():
        return env
    data = os.environ.get("REALTY_DATA_DIR", "/var/lib/realty-bot")
    try:
        with open(os.path.join(data, "owner.json"), encoding="utf-8") as f:
            return str(int(json.load(f)["id"]))
    except (OSError, ValueError, KeyError):
        return None


def notify(text):
    """Повідомлення власнику тестового бота. Без токена/власника — лише в журнал."""
    log("TG:", text)
    if os.environ.get("ARMHUNT_DRY_NOTIFY"):
        return
    token, chat = os.environ.get("TELEGRAM_TOKEN", "").strip(), owner_chat()
    if not token or not chat:
        log("немає TELEGRAM_TOKEN або власника бота — повідомлення не надіслано")
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    for _ in range(3):
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20).read()
            return
        except Exception as e:  # noqa: BLE001
            log("Telegram не відповів:", type(e).__name__)
            time.sleep(10)


# ------------------------------------------------------------------ Oracle
class Oracle:
    def __init__(self):
        import oci  # noqa: PLC0415 — SDK лише на сервері, у venv /opt/realty-armhunt
        self.oci = oci
        self.signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        cfg = {"region": self.signer.region}
        self.T = self.signer.tenancy_id
        self.compute = oci.core.ComputeClient(cfg, signer=self.signer)
        self.vnet = oci.core.VirtualNetworkClient(cfg, signer=self.signer)
        self.bs = oci.core.BlockstorageClient(cfg, signer=self.signer)
        self.ident = oci.identity.IdentityClient(cfg, signer=self.signer)
        self.region = self.signer.region

    def all(self, fn, *a, **kw):
        return self.oci.pagination.list_call_get_all_results(fn, *a, **kw).data

    def instances(self, name, running_only=False):
        out = []
        for i in self.all(self.compute.list_instances, self.T, display_name=name):
            if i.lifecycle_state in ("TERMINATED", "TERMINATING"):
                continue
            if running_only and i.lifecycle_state != "RUNNING":
                continue
            out.append(i)
        return out

    def vnic(self, instance_id):
        att = [a for a in self.all(self.compute.list_vnic_attachments, self.T, instance_id=instance_id)
               if a.lifecycle_state == "ATTACHED"]
        return self.vnet.get_vnic(att[0].vnic_id).data if att else None

    def public_ip(self, instance_id):
        try:
            v = self.vnic(instance_id)
            return (v.public_ip if v else None) or "?"
        except Exception:  # noqa: BLE001
            return "?"

    def ads(self):
        return [a.name for a in self.ident.list_availability_domains(self.T).data]

    def used_gb(self, ads):
        total = 0
        for ad in ads:
            for b in self.all(self.bs.list_boot_volumes, availability_domain=ad, compartment_id=self.T):
                if b.lifecycle_state not in ("TERMINATED", "TERMINATING"):
                    total += int(b.size_in_gbs or 0)
        for v in self.all(self.bs.list_volumes, compartment_id=self.T):
            if v.lifecycle_state not in ("TERMINATED", "TERMINATING"):
                total += int(v.size_in_gbs or 0)
        return total

    def subnet(self):
        src = self.instances(SOURCE_VM, running_only=True)
        if not src:
            raise RuntimeError(f"не знайшов запущену машину {SOURCE_VM} (беру її підмережу)")
        v = self.vnic(src[0].id)
        if not v:
            raise RuntimeError(f"у машини {SOURCE_VM} немає мережевої карти")
        return v.subnet_id

    def image(self):
        imgs = self.all(self.compute.list_images, self.T, operating_system="Canonical Ubuntu",
                        operating_system_version="24.04", shape=SHAPE, sort_by="TIMECREATED", sort_order="DESC")
        imgs = [i for i in imgs if "Minimal" not in (i.display_name or "")]
        if not imgs:
            raise RuntimeError("не знайшов образ Ubuntu 24.04 для ARM")
        return imgs[0]

    def launch(self, ad, ocpus, mem, subnet, image_id, pubkey):
        m = self.oci.core.models
        details = m.LaunchInstanceDetails(
            compartment_id=self.T, availability_domain=ad, shape=SHAPE, display_name=NAME,
            shape_config=m.LaunchInstanceShapeConfigDetails(ocpus=float(ocpus), memory_in_gbs=float(mem)),
            source_details=m.InstanceSourceViaImageDetails(image_id=image_id, boot_volume_size_in_gbs=BOOT_GB),
            create_vnic_details=m.CreateVnicDetails(subnet_id=subnet, assign_public_ip=True),
            metadata={"ssh_authorized_keys": pubkey,
                      "user_data": base64.b64encode(CLOUD_INIT.encode()).decode()})
        return self.compute.launch_instance(details, retry_strategy=self.oci.retry.NoneRetryStrategy()).data

    def wait_running(self, instance_id, timeout=900):
        end = time.time() + timeout
        st = "?"
        while time.time() < end:
            st = self.compute.get_instance(instance_id).data.lifecycle_state
            if st in ("RUNNING", "TERMINATED", "TERMINATING", "STOPPED"):
                return st
            time.sleep(15)
        return st


def err_info(e):
    """(status, code, message) з ServiceError SDK або будь-якої іншої помилки."""
    return (getattr(e, "status", None), getattr(e, "code", None) or type(e).__name__,
            str(getattr(e, "message", None) or e)[:300])


def is_capacity(e):
    st, code, msg = err_info(e)
    return "capacity" in msg.lower()


# ------------------------------------------------------------------ основна логіка
class Hunter:
    def __init__(self, oracle):
        self.o = oracle
        self.st = load_state()
        self.st.setdefault("started", time.strftime("%Y-%m-%d %H:%M"))
        self.st.setdefault("attempts", 0)
        self.st.setdefault("cycles", 0)
        self.last_alert = {}          # код помилки → коли востаннє писали в Telegram
        self.ads = self.subnet_id = self.image_id = None
        self.storage_checked = 0
        with open(PUBKEY_FILE, encoding="utf-8") as f:
            self.pubkey = f.read().strip()

    def save(self, result, error=""):
        self.st["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.st["last_result"] = result
        if error:
            self.st["last_error"] = error
            self.st["last_error_at"] = self.st["last"]
        save_state(self.st)

    def alert(self, key, text, every=6 * 3600):
        if time.time() - self.last_alert.get(key, 0) >= every:
            self.last_alert[key] = time.time()
            notify(text)

    def prepare(self):
        """Усе, що не змінюється між спробами. Помилка тут — проблема з правами/налаштуванням."""
        if self.ads is None:
            self.ads = self.o.ads()
        if self.subnet_id is None:
            self.subnet_id = self.o.subnet()
        if self.image_id is None:
            img = self.o.image()
            self.image_id = img.id
            self.st["image"] = img.display_name
        if time.time() - self.storage_checked > 3600:
            used = self.o.used_gb(self.ads)
            self.st["used_gb"] = used
            self.storage_checked = time.time()
            if used + BOOT_GB > FREE_GB:
                raise RuntimeError(f"диски: зайнято {used} ГБ + {BOOT_GB} ГБ нової машини > {FREE_GB} ГБ безкоштовних")

    def done(self, inst, how):
        ip = self.o.public_ip(inst.id)
        cfg = getattr(inst, "shape_config", None)

        def num(x):
            return f"{x:g}" if isinstance(x, (int, float)) else "?"
        shape = f"{num(getattr(cfg, 'ocpus', None))} OCPU / {num(getattr(cfg, 'memory_in_gbs', None))} ГБ"
        self.st.update({"done": time.strftime("%Y-%m-%d %H:%M"), "ip": ip, "shape": shape})
        self.save("створено")
        with open(state_path("done"), "w", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M')} {ip}\n")
        notify(f"✅ Oracle: ARM-машина {NAME} {how} ({shape}). IP: {ip}.\n"
               f"Пошук зупинено. Напиши в чат проєкту «машина є» — далі встановлення і переїзд мультибота.")
        log("готово:", NAME, shape, "IP", ip)

    def cycle(self):
        """Один прохід по всіх AD і розмірах. True — машину створено (або вже була)."""
        have = self.o.instances(NAME)
        if have:
            self.done(have[0], "вже є")
            return True
        self.prepare()
        self.st["cycles"] += 1
        tried = 0
        for ad in self.ads:
            for ocpus, mem in SHAPES:
                tried += 1
                self.st["attempts"] += 1
                try:
                    inst = self.o.launch(ad, ocpus, mem, self.subnet_id, self.image_id, self.pubkey)
                except Exception as e:  # noqa: BLE001
                    if is_capacity(e):
                        time.sleep(2)
                        continue
                    st, code, msg = err_info(e)
                    if st == 429 or code == "TooManyRequests":
                        log("Oracle просить сповільнитись (429) — пауза 5 хв")
                        self.save("429 — пауза")
                        time.sleep(300)
                        return False
                    # незрозуміла помилка: раптом машина все ж створилась — перевіряємо, і лише потім далі
                    have = self.o.instances(NAME)
                    if have:
                        self.done(have[0], "створена")
                        return True
                    raise
                log(f"запит прийнято: {ad[-4:]} {ocpus}/{mem} — чекаю запуску ({short(inst.id)})")
                st = self.o.wait_running(inst.id)
                if st == "RUNNING":
                    self.done(self.o.compute.get_instance(inst.id).data, "створена")
                    return True
                log(f"машина не запустилась (стан {st}) — шукаю далі")
                self.save(f"не запустилась: {st}")
        log(f"цикл {self.st['cycles']}: немає місця ({tried} спроб, усього {self.st['attempts']})")
        self.save("немає місця")
        return False

    def run(self):
        log(f"пошук {NAME}: {self.o.region}, раз на ~{INTERVAL} с")
        while True:
            try:
                if self.cycle():
                    return 0
            except Exception as e:  # noqa: BLE001
                st, code, msg = err_info(e)
                text = f"{code}{f' ({st})' if st else ''}: {msg}"
                log("помилка:", text)
                self.save("помилка", text)
                hint = (" Схоже, бракує прав (динамічна група / політика realty-arm-hunter в Oracle)."
                        if st in (401, 403, 404) or "NotAuthorized" in str(code) else "")
                self.alert(str(code), f"⚠️ Пошук ARM-машини: {text[:300].rstrip('.')}.{hint} Пробую далі (раз на 10 хв).")
                if st in (401, 403, 404):            # після виправлення прав — перечитати все заново
                    self.ads = self.subnet_id = self.image_id = None
                time.sleep(600)
                continue
            time.sleep(INTERVAL + random.randint(-15, 15))


def check():
    """Перевірка прав і налаштувань без створення машини (для setup-arm-hunt.sh)."""
    try:
        o = Oracle()
        print(f"  вхід в Oracle як сервер: OK (регіон {o.region})")
        ads = o.ads()
        print(f"  availability domain: {len(ads)}")
        have = o.instances(NAME)
        if have:
            print(f"  ✅ {NAME} уже існує ({have[0].lifecycle_state}), IP {o.public_ip(have[0].id)}")
        used = o.used_gb(ads)
        print(f"  диски: зайнято {used} ГБ; з новою машиною {used + BOOT_GB} з {FREE_GB} ГБ")
        o.subnet()
        print(f"  підмережа машини {SOURCE_VM}: OK")
        print(f"  образ: {o.image().display_name}")
        if used + BOOT_GB > FREE_GB:
            print("❌ Вийде понад 200 ГБ дисків — не запускаю")
            return 1
        if not os.path.exists(PUBKEY_FILE):
            print(f"❌ немає {PUBKEY_FILE}")
            return 1
        return 0
    except Exception as e:  # noqa: BLE001
        st, code, msg = err_info(e)
        print(f"❌ {code}{f' ({st})' if st else ''}: {msg}")
        if st in (401, 403, 404) or "NotAuthorized" in str(code):
            print("   Бракує прав: у Oracle мають бути динамічна група і політика realty-arm-hunter (див. довідку).")
        return 1


def main():
    if "--check" in sys.argv:
        return check()
    if os.path.exists(state_path("done")):
        log("машину вже створено (є позначка done) — нічого не роблю")
        return 0
    return Hunter(Oracle()).run()


if __name__ == "__main__":
    sys.exit(main())
