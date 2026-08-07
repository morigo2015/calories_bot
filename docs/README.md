# Calories Bot v2

Закритий багатокористувацький Telegram-бот обліку калорій для приватних чатів.
Доступ надається одноразовими invite-посиланнями. Кожен користувач має окремий
Google Spreadsheet, окрему локальну теку фотографій і власну межу облікової
доби. Реєстр користувачів зберігається в окремому адміністративному Spreadsheet.

Повні продуктові й технічні вимоги: `docs/PRD_v2.md` і `docs/TZ_v2.md`.

## Повідомлення користувача

```text
сир 50
сир 50г 120#
#120 сир 50 г
2 яйця, хліб 50
батончик 45g 380kcal/100g
```

`#120` і `120#` означають 120 ккал/100 г. Якщо ваги чи калорійності немає,
бот оцінює її. Також підтримується одне фото з caption або без нього. Фотоальбоми
й зображення як document ігноруються.

Користувацькі команди:

- `/start <invite_token>` — активувати invite; повторний `/start` показує довідку;
- `/help` — правила та приклади;
- `/day` — згрупований підсумок поточної персональної облікової доби.

Кнопка `Видалити` фізично прибирає рядок із персональної таблиці, безпечно
видаляє пов'язане локальне фото й перераховує відповідну добу. Початкове
повідомлення користувача в Telegram залишається.

## Адміністрування

Admin-команди працюють лише в приватному чаті користувача з ID із
`ADMIN_TELEGRAM_USER_ID`:

```text
/invite Вася
/block 123456789
/unblock 123456789
/delete 123456789
```

`/delete` вимагає inline-підтвердження. Невдала спроба повного видалення лишає
користувача `blocked`, щоб операцію можна було безпечно повторити.

Реєстр має точні колонки:

```text
telegram_user_id, display_name, telegram_username, status, invite_token,
spreadsheet_id, day_start
```

Для перенесення на v2 додайте поточного користувача в реєстр вручну зі статусом
`active`, його Telegram ID, існуючим `spreadsheet_id` і `day_start`. Підтримувана
legacy meal-схема без `day` мігрується автоматично. Фото, чиї збережені шляхи
вказують безпосередньо в корінь `PHOTO_STORAGE_DIR`, переносяться в персональну
підтеку під час першого відкриття таблиці.

## Google Cloud

Service account потребує доступу до Google Sheets API та Google Drive API.
Створіть одну Drive-папку, адміністративний Spreadsheet реєстру в цій папці й
надайте service account права редактора. Нові персональні Spreadsheets бот
створює в цій самій папці автоматично.

## Конфігурація

Потрібен Python 3.11 або новіший.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Обов'язкові змінні:

- `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_USER_ID`;
- `OPENAI_API_KEY`;
- `GOOGLE_SERVICE_ACCOUNT_FILE`, `USERS_SPREADSHEET_ID`;
- `GOOGLE_DRIVE_FOLDER_ID`.

Додаткові змінні:

- `USERS_SHEET_NAME=users`;
- `MEAL_SHEET_NAME=food_log`;
- `PHOTO_STORAGE_DIR=./data/photos`;
- `APP_TIMEZONE=Europe/Kyiv`;
- `DEFAULT_DAY_START=01:00`;
- `OPENAI_MODEL`, `OPENAI_REASONING_EFFORT` і тарифи `OPENAI_*_COST_PER_1M`.

Секрети `.env` і `service-account.json` повинні мати права `600`.

Запуск:

```bash
python -m calories_bot.main
```

## Перевірки

```bash
pip install -r requirements-dev.txt
python -m compileall -q calories_bot
ruff format --check .
ruff check .
mypy calories_bot
python -m pytest --cov=calories_bot --cov-branch
pip check
pip-audit
```

На live-сервері зміни вже знаходяться в `/home/igor/calories-bot`. Після
налаштування `.env`, Drive-папки та реєстру застосуйте оновлення командою,
яку наведено у фінальному звіті.
