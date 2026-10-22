# Довідка проєкту «Постер нерухомості» (стан на 21.10.2026)

## 1. Що це
Telegram-бот (приватний, слухається лише власника), який бере об'єкт із RealtSoft CRM
(публічна презентація `https://crm-capital.realtsoft.net/estate-…html?t=…`) і викладає
його на dom.ria, rieltor.ua, OLX. Код: GitHub **sulimga/realty-bot** (публічний, гілка main).

## 2. Архітектура
```
bot/bot.py            Telegram-бот (python-telegram-bot 21.6). Лише інтерфейс: кнопки, питання, файли.
dispatcher/           boards.json — список сайтів; dispatcher.py — запускає програму сайту окремим
                      процесом, пересилає питання/відповіді, таймаут (час на відповідь не рахується),
                      журнал journal.jsonl.
boards/domria/        програма dom.ria (post_domria.py, mapping.py, domria_client.py, run.py)
boards/rieltor/       програма rieltor.ua (crm_to_rieltor_payload.py, rieltor_geo/photos/refresh_dates.py,
                      set_pickup_rate.py, run.py)
boards/olx/           програма OLX (olx_client.py, mapping.py, run.py)
crm/extract_estate.py парсер презентації CRM (спільний)
common/board_protocol.py  протокол між диспетчером і програмами сайтів
deploy/               cloud-init.yaml, install.sh, update.sh, realty-bot.service
```
**Протокол** (stdout/stdin, JSON по рядку):
`{"type":"info","text"}` · `{"type":"ask","id","text","options":[…]|null,"allow_text"}` ·
`{"type":"result","ok","text","url","id","draft", …додаткові поля}`; відповідь на ask:
`{"id","choice":i}` / `{"id","text"}` / `{"id"}` (пропуск). У програмах: `bp.info`, `bp.ask_choice`,
`bp.ask_text`, `bp.result`. print() автоматично стає info. З консолі (tty) питання йдуть через input().

Кожна програма сайту розуміє: `post --url <CRM> [--techpassport файли…]`, `check`,
(rieltor ще `refresh`, `post --no-ask`; dom.ria ще `techpassport --realty-id N файли…`;
OLX ще `post --dry-run`).

## 3. Сервер
- Oracle Cloud Free Tier, VM «realty-bot», Ubuntu 24.04, E2.1.Micro (x86), створена через
  cloud-init (deploy/cloud-init.yaml → install.sh: swap, apt, git clone, venv, systemd).
- Код: `/opt/realty-bot`, venv `.venv`. Дані: `/var/lib/realty-bot` (secrets/, logs/, journal.jsonl,
  domria/, rieltor/, olx/ — звіти й логи запитів). Налаштування: `/etc/realty-bot.env`
  (TELEGRAM_TOKEN, REALTY_DATA_DIR). Сервіс systemd `realty-bot`.
- У Claude немає SSH на сервер. Оновлення — команда `/update` у боті. Логи — `/logs`.
- Стара VM (instance-20260915-1204) ще існує — видалити лише після мого «так».

## 4. Бот: меню і сценарії
Кнопки: 📤 Викласти оголошення · 📎 Техпаспорт до чернетки · 🍪 Cookies · 🔄 Оновити дати rieltor · ℹ️ Статус.
Команди: /start (перший — стає власником), /cancel, /update, /logs, /status, /id.

**Викласти оголошення:** спершу вибір сайтів →
- лише rieltor: можна одне посилання, кілька (кома/рядки) або .txt → **пакет**: у фоні по черзі
  (пауза 5 с), без питань (`--no-ask`), у чаті тримаються звіти лише поточного й попереднього
  оголошення, решта видаляється; наприкінці один підсумок (✅/❌ по артикулах). /cancel зупиняє після
  поточного. Пакет сам зупиняється при протухлих cookie або ліміті фото rieltor (`stop_batch`).
- інші сайти: одне посилання → зведення → (для dom.ria техпаспорт: PDF / кілька фото / нічого)
  → підтвердження → публікація.
Просто кинуте посилання — зведення, потім вибір сайтів.

**Cookies:** вибір сайту → повідомлення з cookie (або для OLX — рядок Authorization) → бот зберігає
у `secrets/<сайт>_cookies.txt` / `<сайт>_auth.txt` (chmod 600), видаляє повідомлення, запускає `check`.

## 5. CRM (extract_estate.py)
Повертає: estate_id, lat/lng (НЕ використовуємо), price («2 500 000 $»), deal_title
(«Продаж / Будинок»), address («Київ, Дарницький район, вул. …, 7» або «Києво-Святошинський район,
Лісники, вул. Стежки»), quick_facts, article («#A36007»), parameters (таблиця: «Поверховість»,
«Площа загальна,м²», «Площа ділянки,соток», «Кіл. кімнат», «Тип будинку», «Ремонт», «Комплекс»,
«Новобудова» …; буває російською), description, photos (повнорозмірні URL).

## 6. dom.ria
- Авторизація: cookie. Створення: `/node/saveRealty` (→ realty_id, beautiful_url), фото
  `9.riastatic.com/upload/v4/` + `/node/savePhotosOrdering/`.
- Техпаспорт: `/uploadfile/` (entity_type=techpassport) → `/v1/api/inspections/sendRealtyDataAndCreateInspectionOrder/`
  → `/node/api/publishByVideoInspection/`. З техпаспортом публікація безкоштовна. Без нього — чернетка
  (потім «📎 Техпаспорт до чернетки»). Кілька фото/тека → склеюються в один PDF (з EXIF-поворотом).
- Перевірка повноти: `/node/checkIfRealtyHasMainCharacteristics`; відповідь publish перевіряється
  (`missingCharacteristics` = помилка).
- Гео: autocompleteV3 (місто, вулиця), getPointByGeo (`ENTITY` = точний будинок, `DB` = запасний),
  getGeoByPoint (район/метро лише якщо будинок ENTITY, обгорнуто в try). Пріоритет Київської обл. (state_id 10).
- Типи: 2 квартира, 3 кімната, 5 будинок, 6 частина будинку, 7 дача, 8 дуплекс, 9 таунхаус,
  11 офіс, 12 офісна будівля, 14–22 комерція, 25–27 ділянки, 31–35 гаражі.
- realty_sale_type = 1 завжди. Стіни: WALL_ALIASES (газоблок↔газобетон), дефолт цегла.
- Таймаути 40 с, повтори лише для GET.

## 7. rieltor.ua
- Cookie. `POST /api/offers/`; фото `/api/offers/upload-photo/` (мін. ширина 1200 — дрібні
  розтягуються; мін. 3 фото — дублюється останнє; ліміт `image_upload_rate_limit` → чекати 5 хв, до 12 разів).
- Продаж будинку вимагає groundArea (питаємо або в пакеті пропускаємо).
- **Продаж ділянки (itemType=5):** rieltor.ua приймає поле `cadastralNumber` (рядок, формат
  `3220882200:03:005:1306` — підтверджено реальним HAR). Шукаємо його регуляркою
  `\d{10}:\d{2}:\d{3}:\d{4}` у вільному тексті опису CRM (`find_cadastral_in_text()` у
  `crm_to_rieltor_payload.py`) і кладемо в payload. Якщо в описі кадастру нема — об'єкт
  пропускається одразу (без фото/публікації, без питань навіть поза `--no-ask`), щоб можна
  було вантажити всю землю з CRM без ручної перевірки кожного оголошення (22.10.2026).
- Таймаут публікації ≠ провал: оголошення зазвичай створюється — перевіряти кабінет перед повтором.
- `refresh` — оновлення дат (`GET /api/offers/item-action/?id=…&action=refresh`). **Ліміт ≈49 оновлень
  на добу**, тому скрипт сортує активні оголошення від найстарішої дати оновлення і оновлює, доки
  сайт не відповість помилкою ліміту (або 429) — тоді зупиняється. Поле дати шукається автоматично
  (список кандидатів у `rieltor_refresh_dates.py`); не знайдено — порядок за ID. Стоп також при 401/403
  і після 3 незрозумілих помилок поспіль. Звіт кожного запуску (з назвами полів оголошення і текстом
  помилки ліміту) — `/var/lib/realty-bot/rieltor/runs/*_refresh.json`.
  Кнопка «🔄 Оновити дати rieltor» спершу питає: усі оголошення або категорія (Будинки, Квартири+кімнати,
  Комерційна, Земля, Паркомісця) → продаж/оренда. З консолі/розкладу: `refresh --all` або
  `refresh --category houses|flats|commercial|land|parking --oper sale|rent`. Категорія фільтрується
  на нашому боці за полями itemType/operType оголошення (1 квартира, 2 кімната, 3 будинок, 4 комерція,
  5 земля, 6 паркомісце; 1 продаж, 2 оренда). Якщо цих полів у списку немає — нічого не оновлюємо.
  ⚠️ Назву поля дати, полів типу і точний текст помилки ліміту ще не перевірено на реальному запуску.
- `set_pickup_rate.py` — ставка за підняття (можливо, пов'язано з «програмою вибору ціни», див. плани).

## 8. OLX (перша версія, лише «Продаж будинків», категорія 1602)
- **Вхід:** AWS Cognito (пул eu-west-1_V2AJUX0XE, client_id 309lsgh0deirlo2la9kmrmhe3v, логін через
  login.olx.ua, на сайті auth0-spa-js). API приймає лише `Authorization: Bearer <access token>`
  (живе **15 хв**). Cookie `access_token` — це інший токен (id), для API = invalid_token.
  Довгий refresh token лежить у localStorage браузера (`@@auth0spajs@@::…::offline_access`), оновлення —
  `POST https://login.olx.ua/oauth2/token` (form: grant_type=refresh_token, client_id, refresh_token);
  **refresh token ротується** при кожному оновленні (не можна ділити між браузером і ботом).
  Зараз токен вставляю вручну перед публікацією (🍪 → OLX → значення authorization з запиту users/me).
  Автооновлення — НЕ зроблено, рішення за мною (окремий вхід в інкогніто лише для бота).
- **CloudFront** блокує звичайні Python-запити (403) — використовується `curl_cffi` з імітацією Chrome.
- Ендпоінти: `GET www.olx.ua/api/v1/users/me/`; гео (публічні) `/api/v1/geo-encoder/location-autocomplete/?query=…&limit=10&scope=posting`
  (з Accept-Language: uk — назви українською; для Києва — записи з district) і `/api/v1/geo-encoder/cities/{id}/`;
  фото: `POST /api/v1/apollo/token/` → `{"token"}`, далі `POST https://ireland.apollo.olxcdn.com/v1/temp-files`
  (тіло JPEG, `Authorization: Bearer <apollo token>`, `Expires` = завтра) → `{"data":{"filename"}}`;
  оголошення: `POST https://posting-services.prd.01.eu-west-1.eu.olx.org/api/v2/offers` (заголовок postingid=uuid).
  Визначення категорії: `posting-services…/v2/categories?categoryID=1602&brand=olxua&language=uk`.
- Обов'язкові параметри 1602 (лише цілі числа `^[1-9]\d*$`): total_floors, total_area, number_of_rooms,
  land_area (соток), property_type_houses, price {currency, price, arranged:1}. Фото макс. 24.
  house_type (стіни) 0 цегла…7 інше; repair 1 авторський … 7 аварійний.
- Опис: текст CRM + артикул з пробілами («A 3 6 0 0 7»). Заголовок 16–70 символів (бот пропонує або вписую сам).
  Контакти (ім'я, телефон, email) питаються один раз → `/var/lib/realty-bot/olx/config.json`.
- Перед відправкою бот показує зведення і питає «Створити на OLX?». Після створення оголошення
  чекає на оплату розміщення в кабінеті (сторінку платної реклами API не проходить).

## 9. Корисні уроки
- Chrome «sanitized» HAR вирізає cookie, Authorization і тіла відповідей — достатньо для структури запитів.
- Не брати координати з CRM (бувають неправильні). Регулярки на адресу — лише з межами слів
  («ЖК» ловило «Стежки»).
- Завжди перевіряти відповідь сайту по суті (status у тілі), а не лише HTTP 200.
- Тестувати категорії чернетками; реальні публікації — лише з дозволу.

## 10. Що зроблено
- dom.ria: усі основні категорії (квартири/будинки продаж і оренда, офіси, комерційна земля), техпаспорт.
- rieltor: публікація, пакетне викладання, оновлення дат з урахуванням добового ліміту (найстаріші першими; усі або окрема категорія).
- OLX: продаж будинків (протестовано — оголошення створюється, чекає оплати).
- Сервер, бот, оновлення через /update, статус, cookie.

## 11. Плани (по порядку)
0. Розклад: щоденне оновлення дат rieltor (раз на добу вистачає — ліміт ≈49); вирішити автооновлення токена OLX.
1. **Таблиця об'єктів**: артикул/ID CRM, ID на rieltor, dom.ria, OLX, поточна ціна, статус.
   Записувати ID після кожної публікації. Далі — моніторинг Telegram-чатів (бот у групі або читання
   від мого акаунта — вирішити), розпізнавання зміни ціни / «продано» / «знято» → підтвердження в боті
   → зміна на всіх сайтах → оновлення таблиці. Потрібні HAR редагування ціни і зняття на кожному сайті.
2. **Статистика**: перегляди, відкриття телефону тощо з кожного сайту; знімати щогодини й зберігати
   приріст → активність по годинах/днях. Вивід: тижневий звіт у бот і/або сторінка з графіками.
   Потрібні HAR сторінок статистики.
3. **Програма вибору оптимальної ціни** для rieltor (лежить на ноутбуці / в іншому проєкті) —
   перенести й вбудувати в бота.
4. OLX: інші категорії (квартири, оренда тощо).
