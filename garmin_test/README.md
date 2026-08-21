# Перевірка Garmin Connect через `python-garminconnect`

Цей проєкт перевіряє реальний Garmin-акаунт у двох режимах:

- `garmin-probe audit` робить read-only інвентаризацію доступних даних і кілька
  повторних запитів для оцінки стабільності;
- `pytest -m integration` є коротким тестом для CI або ручного запуску: він
  перевіряє відновлення сесії, повторюваність відповіді та максимальну затримку.

Звіт **не містить** email, пароля, токенів, імені чи значень показників здоров'я.
Він зберігає лише статуси, час відповіді, тип JSON та назви полів — цього
достатньо, щоб спроєктувати інтеграцію з іншою власною аппкою.

## Важливе обмеження

`python-garminconnect` — community-бібліотека для внутрішніх endpoint'ів Garmin
Connect, а не офіційний публічний API з контрактом сумісності. Сьогодні вона
покриває понад 140 методів, але Garmin може без попередження змінити endpoint,
формат відповіді, логін або rate limit. Тому успішний тест доводить, що
інтеграція працює зараз для конкретного акаунта; він не створює SLA.

Для особистої аппки/прототипу це практичний варіант. Для багатокористувацького
або комерційного продукту варто планувати офіційний
[Garmin Connect Developer Program](https://developer.garmin.com/gc-developer-program/overview/):
офіційні API використовують OAuth 2.0, мають документацію і підтримку, але
програма орієнтована на business use.

## Що потрібно підготувати в Garmin

Нічого не треба копіювати з DevTools браузера: ані cookies, ані `sessionId`, ані
OAuth-токени. Не надсилайте пароль, одноразовий код або папку з токенами іншій
людині чи в чат. Усі секрети вводяться тільки локально у вашому терміналі.

1. **Знайдіть точний email потрібного акаунта.**

   - Android: Garmin Connect → **More** → **Settings** → **Device Sync Audit**;
     email показано вгорі.
   - iPhone: Garmin Connect → **More** → **Settings** → **Profile & Privacy** →
     **Manage Garmin Account**.
   - Якщо акаунтів кілька, переконайтесь, що годинник синхронізується саме з
     цим email. Офіційна інструкція Garmin:
     [перевірка email у Garmin Connect](https://support.garmin.com/en-US/?faq=PNFiDLuFZz6livOYiIknL6).

2. **Перевірте звичайний вхід у браузері.** Відкрийте
   [Garmin Connect](https://connect.garmin.com/modern/), вийдіть і зайдіть з
   цим email та паролем. Якщо пароль невідомий, на сторінці входу натисніть
   **Forgot Password** і завершіть відновлення. Спочатку доможіться успішного
   входу в браузері — це відділяє проблему акаунта від проблеми бібліотеки.

3. **Перевірте двоетапну верифікацію.** Відкрийте
   [Garmin Account Profile](https://www.garmin.com/account/profile) →
   **Edit Sign In Settings**. Переконайтесь, що маєте доступ до вибраного email
   або номера телефону. Під час першого запуску Garmin може надіслати код;
   [за документацією Garmin](https://support.garmin.com/en-GB/?faq=uGHS8ZqOIhA0usBzBMdJu7&productID=73207&tab=)
   код діє 30 хвилин. Перевірте Spam, якщо листа немає.

4. **Синхронізуйте пристрій.** Відкрийте Garmin Connect на телефоні, дочекайтесь
   завершення sync і перевірте, що за вчора в UI видно очікувані показники.
   Бібліотека читає вже завантажені в Garmin Connect дані, а не сенсори
   годинника напряму.

Це вся інформація для доступу: email, пароль і, якщо Garmin попросить,
одноразовий код. ID профілю, ID пристрою та cookies вручну шукати не потрібно.

## Встановлення

Потрібен Python 3.12 або новіший. У каталозі цього проєкту:

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
```

На Windows PowerShell команда активації інша:

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
```

Версію `garminconnect` зафіксовано в `pyproject.toml`. Це робить повторні тести
порівнюваними: оновлення бібліотеки не відбудеться непомітно.

## Перший вхід — покроково

1. Активуйте `.venv`.
2. Запустіть:

   ```bash
   garmin-probe login
   ```

3. На `Garmin email:` введіть email, знайдений вище.
4. На `Garmin password (input is hidden):` введіть пароль. Символи в терміналі
   не відображатимуться — це нормально.
5. Якщо з'явиться `Garmin one-time verification code:`, відкрийте SMS/email від
   Garmin і введіть код. Не вводьте його на сторонніх сайтах.
6. Після `Login successful` токени будуть у `~/.garminconnect/`. Код обмежує
   директорію правами `700`, а файли — `600`. Пароль проєкт не записує.

Наступні запуски використовують renewable tokens. Повторно вводити пароль
зазвичай не треба. Після зміни пароля, виходу з усіх сесій або зміни логіну
токени можуть стати недійсними — тоді знову виконайте `garmin-probe login`.

За потреби окремого сховища:

```bash
garmin-probe --tokenstore /absolute/private/path login
garmin-probe --tokenstore /absolute/private/path audit
```

Шлях має бути приватним і не повинен потрапляти в Git, backup для спільного
доступу, Docker image чи CI artifact.

## Повний audit доступних даних

За замовчуванням перевіряється вчорашній завершений день, бо сьогоднішні дані
можуть законно змінюватися після кожної синхронізації:

```bash
garmin-probe audit
```

Результат у консолі буде приблизно таким:

```json
{
  "verdict": "working",
  "available_data_types": 13,
  "probed_data_types": 15,
  "requested_data_types": 15,
  "stability_success_rate": 1.0,
  "rate_limited": false,
  "endpoint_errors": 0,
  "stopped_early": false
}
```

Детальний privacy-safe звіт записується у
`artifacts/garmin_probe_report.json`. Можна вказати дату й інтенсивність:

```bash
garmin-probe audit --date 2026-08-12 --runs 5 --delay 3 \
  --output artifacts/garmin_probe_2026-08-12.json
```

Не ставте малу затримку і десятки/сотні повторів: це не load test, а перевірка
надійності персональної інтеграції. Агресивне опитування може викликати HTTP
429 або тимчасове блокування логіну.

### Як читати звіт

- `available` — endpoint працює і повернув дані;
- `available_empty` — endpoint працює, але для дати/акаунта немає значень;
- `not_supported_or_not_found` — метрика не підтримується пристроєм/акаунтом
  або Garmin змінив endpoint; це не робить весь тест невдалим;
- `authentication_failed` — треба повторити login;
- `connection_failed` — мережа або Garmin тимчасово недоступні;
- `rate_limited` — зупинити запити й повторити пізніше;
- `stopped_early: true` — audit навмисно припинив решту запитів після auth
  failure або rate limit, щоб не погіршувати блокування;
- `shape_stable: false` — між повторами змінилася структура JSON; перед
  використанням в іншій аппці потрібна defensive schema validation;
- `latency_seconds` — min/median/max; стандартний поріг тесту — 15 секунд.

Вердикти `working` і `working_but_slow` означають, що базова інтеграція працює.
`unstable_request_failures` та `unstable_rate_limited` треба розслідувати за
полем `statuses`.

## Інтеграційний pytest

Звичайний `pytest` запускає тільки локальні unit-тести і ніколи не торкається
Garmin. Реальний тест треба увімкнути явно:

```bash
pytest
pytest -m integration -o addopts='-ra'
```

Другий запуск:

- завантажує тільки збережені токени;
- тричі читає daily summary за вчора;
- вимагає 100% успішних відповідей;
- перевіряє стабільність JSON shape;
- падає, якщо один запит довший за 15 секунд.

Параметри можна змінити environment variables:

```bash
GARMIN_TEST_DATE=2026-08-12 \
GARMIN_STABILITY_RUNS=5 \
GARMIN_STABILITY_DELAY=3 \
GARMIN_MAX_LATENCY_SECONDS=20 \
pytest -m integration -o addopts='-ra'
```

Якщо токени лежать не в `~/.garminconnect`, додайте
`GARMINTOKENS=/absolute/private/path`.

## Які дані можна використати в інших аппках

Audit навмисно перевіряє репрезентативний read-only набір:

| Категорія | Дані |
|---|---|
| Daily health | кроки, дистанція/калорії у daily summary, пульс, сон, stress, intensity minutes |
| Advanced health | Body Battery, HRV, Pulse Ox/SpO2, respiration, training readiness |
| Wellness | hydration |
| Activities | останні активності, personal records |
| Devices | зареєстровані Garmin-пристрої та доступні metadata |

Сама бібліотека також має endpoint'и для ваги/body composition, VO2 max,
training status/load, workouts і schedule, gear, goals, badges/challenges,
menstrual health, blood pressure, golf, завантаження FIT/GPX/TCX та редагування
деяких даних. Вони не входять у цей audit, бо частина з них змінює акаунт, а
частина дуже залежить від моделі пристрою й активованих функцій. Повний поточний
перелік дивіться в
[README `python-garminconnect`](https://github.com/cyberjunky/python-garminconnect#api-coverage-statistics).

Назви полів у `response_shape` показують, що реально можна мапити в модель іншої
аппки. Не покладайтеся на наявність кожного поля: зберігайте сирий JSON у
захищеному сховищі, валідовуйте відому підмножину, допускайте `null`/порожні
масиви і версіонуйте власну схему.

### Чого тут немає

- Це не real-time потік із годинника. Нові дані з'являються після sync у Garmin
  Connect.
- Не кожна метрика доступна на кожній моделі пристрою.
- Порожня відповідь не обов'язково є помилкою: користувач міг не носити годинник
  або вимкнути сенсор.
- Не можна безпечно вбудовувати Garmin email/password у frontend, мобільний
  binary або розширення браузера. Запити мають іти з приватного backend/local
  service, а іншим компонентам слід віддавати ваш мінімальний нормалізований API.

## Як реально оцінити надійність

Один успішний audit — це smoke test. Для обґрунтованої оцінки запускайте audit
1–4 рази на день протягом 7–14 днів, кожного разу в окремий файл, і рахуйте:

1. availability = успішні повтори / усі повтори;
2. p50/p95 latency;
3. кількість 401/403 (token/auth), 429 (rate limit), 5xx/network errors;
4. частоту змін `response_shape` після оновлення бібліотеки;
5. затримку між sync пристрою і появою даних у Garmin Connect.

Для іншої аппки рекомендовано: помірний polling, exponential backoff з jitter
для 429/5xx, cache останньої успішної відповіді, idempotent upsert за датою/ID
активності, observability без логування health values і токенів, а також pin
версії бібліотеки з окремим тестом перед оновленням.

## Безпека та відкликання доступу

- Ніколи не комітьте `.env`, `~/.garminconnect` чи `garmin_tokens.json`.
- Не друкуйте exception/debug HTTP logs у публічний CI — вони можуть містити
  приватні metadata.
- Якщо токени могли витекти, видаліть **лише конкретну** приватну папку токенів,
  змініть Garmin-пароль, перевірте sign-in settings і виконайте login заново.
- Health-дані є чутливими персональними даними; для аппки з іншими користувачами
  потрібні явна згода, мінімізація даних, retention/deletion policy та перевірка
  застосовного законодавства.

Джерела: [офіційний Health API Garmin](https://developer.garmin.com/gc-developer-program/health-api/),
[FAQ Developer Program](https://developer.garmin.com/gc-developer-program/program-faq/),
[проєкт `python-garminconnect`](https://github.com/cyberjunky/python-garminconnect),
[PyPI](https://pypi.org/project/garminconnect/).
