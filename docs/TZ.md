# Технічна специфікація Calories Bot

**Статус:** актуальна реалізація станом на 7 вересня 2026 року.

## 1. Склад системи

- Python 3.11+, `python-telegram-bot`, long polling, послідовна обробка updates.
- OpenAI Structured Outputs для їжі, emoji збережених страв, семантичного
  групування тижня й розпізнавання скріншотів витрати; окрема транскрипція
  голосу.
- Google Sheets / Drive через service account як основне сховище користувачів.
- Локальні SQLite для аналітики, JSON-кеш Garmin і файлове сховище фото.
- Rich Telegram messages із `<details>`; при недоступності rich API є fallback
  до звичайного Telegram HTML.

Основні модулі:

- `main.py` — складання залежностей, меню, handlers і Garmin job;
- `bot.py` — сценарії, стани, доступ, форматування й orchestration;
- `analyzer.py` — нормалізація, аналіз їжі та транскрипція;
- `models.py` — Pydantic-моделі, розрахунок і масштабування;
- `sheets.py`, `saved_meals.py`, `burned.py` — три персональні worksheets;
- `burn_screenshots.py` — аналіз та узгодження скріншотів Garmin / Zepp Life;
- `meal_grouping.py` — семантичне групування тижневого звіту;
- `users.py`, `workspace.py` — реєстр і персональні Spreadsheets;
- `garmin.py` — renewable tokens, денне оновлення та локальний кеш;
- `analytics.py` — статистика подій, повідомлень-зведень і вартості OpenAI.

## 2. Доступ і маршрутизація

Власник даних визначається тільки за `telegram_user_id`. До персональних
даних, фото, голосу й LLM допускається лише `active`-користувач у приватному
чаті. Адміністративні команди перевіряють точний `ADMIN_TELEGRAM_USER_ID`.

`UserManager` кешує `CaloriesService` за
`(telegram_user_id, spreadsheet_id, day_start)`. Сервіс отримує персональні
`MealStore`, `SavedMealStore`, `BurnedCalorieStore` і каталог фото.

Некомандний текст спочатку перевіряється як відповідь на активний діалог:

1. ім’я для invite;
2. ціль калорій або білка;
3. нова вага історичного прийому;
4. назва або стандартна вага збереженої страви;
5. дата чи значення витрати;
6. поле налаштувань;
7. звичайний опис їжі.

Початок нового діалогу й будь-яка команда очищують інші pending-стани.

## 3. Допомога та command menu

У меню користувача: `/saved`, `/recent`, `/day`, `/week`, `/goal`,
`/protein_goal`, `/burn`, `/settings`, `/help`.

Handlers також підтримують `/meals`, `/save [назва]` і `/tips`, але не
показують їх у menu. `/tips` лишився сумісним маршрутом до розширеної довідки.

`/help` завантажує `help.txt` під час кожного виклику та додає callback
`help-more`. Він редагує те саме повідомлення текстом `tips.txt`; `help-main`
повертає коротку версію. Адміністратор додатково бачить `help-admin`, що
відкриває `admin_help.txt`. Таким чином деталі не займають окремого місця в
меню й не перевантажують перший екран.

## 4. Реєстр користувачів

Адміністративний worksheet має точний заголовок:

```text
telegram_user_id
display_name
telegram_username
status
invite_token
spreadsheet_id
day_start
daily_kcal_goal
daily_protein_goal
bmr_sex
birth_date
height_cm
weight_kg
```

Відомі попередні схеми мігруються послідовним додаванням колонок. Невідома
непорожня схема відхиляється.

`/start <token>` повторно використовує вже створений Spreadsheet після
частково успішної активації. `/delete` спочатку блокує користувача, потім
видаляє Spreadsheet, фото й рядок реєстру; завдяки цьому невдалу операцію
можна безпечно повторити.

## 5. Журнал `food_log`

Назва worksheet задається `MEAL_SHEET_NAME`, типово `food_log`. Заголовок:

```text
timestamp
day
meal_name
total_weight_g
meal_kcal
kcal_per_100g
telegram_message_id
normalized_request
request
photo_path
items_json
estimated
model
effort
input_tokens
output_tokens
llm_cost_usd
```

Повне КБЖВ, origins, basis і дані компонентів зберігаються в `items_json`;
плоскі колонки калорій лишаються для сумісності. Legacy-схема без `day`
мігрується вставленням колонки й backfill.

Облікова дата розраховується в `APP_TIMEZONE`: локальний час раніше
персонального `day_start` належить попередній даті. `timestamp` і `day`
записуються як нативні значення Google Sheets.

Один аналіз із кількома компонентами зберігає кожен компонент окремо. Перший
використовує ID Telegram-повідомлення, наступні — стабільні від’ємні ID.
`normalized_request` містить `simple_meal:v1` із source ID, позицією й
кількістю компонентів. Повторна доставка тієї самої події відновлює всі
картки без дублювання.

## 6. Аналіз і розрахунок

`normalize_input()` виділяє явні вагу, КБЖВ, basis та числа словами. Підтримує
цілі й дробові значення з крапкою або комою. Analyzer отримує нормалізований
текст, source IDs явних значень і, за потреби, bytes найбільшого фото.

LLM повертає `FoodAnalysis`; `calculate_meal()` локально й детерміновано
обчислює порції. Origin `user_text` має пріоритет над `image`,
`deterministic_reference` і `model_estimate`. Оцінені значення формують `≈`.

Перевірка `calorie_macro_mismatch_percent()` лише показує попередження, якщо
розбіжність із `4/9/4` перевищує
`NUTRITION_MISMATCH_THRESHOLD_PERCENT` (типово 20%); вхідні значення не виправляються.

Фото записується тільки після успішного аналізу в
`PHOTO_STORAGE_DIR/<telegram_user_id>/<day>-<message_id>.jpg`. Видалення
дозволене лише всередині персональної теки.

## 7. Збережені страви

Worksheet `saved_meals` має точний заголовок:

```text
saved_meal_id
source_message_id
display_name
default_total_weight_g
simple_meal_json
icon
is_pinned
sort_order
```

`SavedMeal.base_meal` завжди містить рівно один компонент. `list_meals()`
сортує спочатку закріплені, потім звичайні; усередині групи більший
`sort_order` іде вище. Новий шаблон отримує найбільший порядок.

Store підтримує читання, append, rename, зміну стандартної ваги, pin/unpin,
reorder і delete. Після write операція перевіряється повторним читанням.
Попередня схема з `simple_meal_json` без полів порядку мігрується зі
збереженням даних. Старі схеми `meal_json`, що могли містити складені шаблони,
очищуються одноразово й переходять на поточний формат.

Збереження source ідемпотентне. Автоматичні однакові назви отримують `(2)`,
`(3)`; явно задана зайнята назва відхиляється. Emoji зберігається тільки при
confidence `>= 0.8`.

## 8. Повторення й зміна ваги

`/recent` запитує 16 останніх різних `MealResult`, переглядаючи журнал від
кінця й дедуплікуючи канонічний JSON у пам’яті.

Callbacks повторення:

```text
saved-add:<saved_meal_id>:<weight_g>
recent-add:<message_id>:<day>:<weight_g>
```

Для callback-подій із query ID детерміновано утворюється від’ємний 52-bit
event ID. Повторний callback ідемпотентний, а різні натискання створюють різні
прийоми.

`scale_meal()` перераховує готовий однокомпонентний результат без LLM.
`update_meal()` редагує той самий рядок і повторно читає його для перевірки.
Вага, калорії й КБЖВ змінюються, а timestamp, request, metadata та фото — ні.

## 9. День, тиждень і витрата

`/day` будується безпосередньо з рядків журналу. Заголовок використовує звичайний `<b>` без
блочних відступів `<h3>`. Кожен із чотирьох показників має `<details>` зі списком окремих
прийомів. У верхньому `<summary>` одиниці виміру не показуються; для калорій і білка цілі та
progress bars лишаються видимими. Navigation callback показує сусідні дати, але не майбутнє.

ID денних зведень зберігаються в SQLite. Перед новим зведенням бот видаляє
попередні повідомлення поточної персональної доби пакетами до 100 ID з
fallback на поштучне видалення.

`/week` бере до семи завершених днів і скорочує період для нової історії.
Назви попередньо агрегуються точно, потім можуть бути семантично об’єднані
окремим LLM-запитом. Помилка повертає exact grouping; довгий tail згортається
до `Інше`.

Worksheet `burned_calories`:

```text
day
input_type
input_kcal
resting_kcal
effective_total_kcal
profile_snapshot
updated_at
```

`input_type` — `total` або `active`. Для `active` обов’язковий повний профіль,
а пасивна витрата рахується за Mifflin–St Jeor та зберігається разом зі
знімком профілю.

Імпорт `/burn` аналізує одне фото або Telegram media group як пакет,
відкидає поточну добу, об’єднує дублікати й запускає conflict flow лише для
різних значень однієї дати.

Garmin cache оновлюється один раз на початку бот-доби; hourly job надолужує
пропущене оновлення після рестарту. Garmin використовується як fallback лише
для адміністратора, а персональний worksheet `/burn` має пріоритет.

## 10. Надійність, конфігурація й перевірки

- Google append/update/delete перевіряються повторним читанням після успіху чи
  помилки API; known failure і uncertain result мають різні повідомлення.
- Логи не повинні містити bot token, секрети, тексти прийомів, JSON страв або
  фото; `httpx` і `httpcore` обмежені рівнем WARNING.
- `concurrent_updates(False)` та локальні locks захищають read-modify-write.
- Вартість OpenAI обчислюється з окремих тарифів основної та grouping моделей;
  admin Costs API є необов’язковим.

Обов’язкові env: `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_USER_ID`,
`OPENAI_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_FILE`, `USERS_SPREADSHEET_ID`,
`GOOGLE_DRIVE_FOLDER_ID`. Повний список із defaults є в [.env.example](../.env.example).

Безкоштовний pipeline:

```bash
bash scripts/run_tests.sh
```

Він запускає compileall, Ruff format/check, mypy, pytest із branch coverage не
нижче 75% та `pip check`. Paid LLM eval запускається тільки окремо з явним
підтвердженням користувача.
