# realty-bot

Telegram-бот, що викладає оголошення з RealtSoft CRM на dom.ria, rieltor.ua (далі — OLX).

```
bot/            Telegram-бот: лише інтерфейс (кнопки, файли, питання). Про сайти не знає нічого.
dispatcher/     Диспетчер: список сайтів (boards.json), запуск програми сайту окремим процесом,
                пересилання питань/відповідей, журнал. Сам нічого не публікує.
boards/<сайт>/  Окрема програма кожного сайту. Падіння однієї не зачіпає інші й бота.
crm/            Парсер презентації CRM (спільний).
common/         Протокол між диспетчером і програмами сайтів (board_protocol.py).
deploy/         Сервер: cloud-init, systemd-сервіс, скрипт оновлення.
```

## Як додати новий сайт (OLX)
1. Папка `boards/olx/` з `run.py`, що розуміє команди `post --url ... [--techpassport ...]` і `check`
   та спілкується через `common/board_protocol.py` (`bp.info`, `bp.ask_choice`, `bp.ask_text`, `bp.result`).
2. У `dispatcher/boards.json` для `olx` поставити `"enabled": true`.
Бот і інші сайти при цьому не змінюються.

## Програма сайту з консолі (для тестів)
```
REALTY_DATA_DIR=./data python boards/domria/run.py post --url "<посилання CRM>"
REALTY_DATA_DIR=./data python boards/rieltor/run.py check
```
dom.ria можна запускати й старим способом: `python boards/domria/post_domria.py post "<посилання>"`.

## Сервер (Oracle Cloud, Ubuntu)
- Код: `/opt/realty-bot` (git), venv: `/opt/realty-bot/.venv`
- Дані: `/var/lib/realty-bot` — cookie (`secrets/`), журнал, логи, звіти. **У git не потрапляють.**
- Налаштування: `/etc/realty-bot.env` (TELEGRAM_TOKEN, REALTY_DATA_DIR)
- Установка з нуля: `deploy/cloud-init.yaml` (вставляється при створенні віртуалки) → `deploy/install.sh`, лог: `/var/log/realty-bot-install.log`
- Сервіс: `systemctl status realty-bot`, логи: `journalctl -u realty-bot -f`
- Оновлення: команда `/update` у боті (git pull → перевірка синтаксису → перезапуск; при помилці — відкат)

## Команди бота
Кнопки меню: «Викласти оголошення», «Техпаспорт до чернетки», «Cookies», «Оновити дати rieltor», «Статус».
Команди: `/cancel`, `/update`, `/logs`, `/status`, `/id`.
Першим, хто надішле боту `/start`, стає власник — далі бот відповідає тільки йому.
