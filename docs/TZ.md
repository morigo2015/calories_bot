# Технічна специфікація Calories Bot

**Статус:** актуальна реалізована версія станом на 9 серпня 2026 року.

## 1. Склад системи

- Python 3.11+, `python-telegram-bot`, long polling без concurrent updates.
- OpenAI Structured Outputs для аналізу тексту й одного зображення.
- Google Sheets/Drive через service account.
- Окремої БД, кешу, background jobs і web-інтерфейсу немає.

Основні модулі:

- `main.py` — конфігурація Telegram-команд і handlers;
- `bot.py` — прикладні сценарії, доступ, стани й форматування;
- `analyzer.py` — нормалізація та LLM-аналіз;
- `models.py` — Pydantic-моделі, розрахунок і масштабування;
- `sheets.py` — журнал прийомів;
- `saved_meals.py` — шаблони збережених страв;
- `users.py` — реєстр користувачів;
- `workspace.py` — створення й відкриття персональних Spreadsheets.

## 2. Доступ і маршрутизація

Власник даних визначається лише за `telegram_user_id`. До LLM, фото та
персональних таблиць допускається тільки `active`-користувач у приватному
чаті. Групи, канали й edited updates ігноруються.

`UserManager` кешує `CaloriesService` за
`(telegram_user_id, spreadsheet_id, day_start)`. Сервіс отримує персональні
`MealStore`, `SavedMealStore` і каталог фото.

Для некомандного тексту порядок обробки такий:

1. ім’я для admin invite;
2. денна ціль;
3. вага або назва для saved/recent flow;
4. звичайний аналіз їжі.

Початок нового сценарію очікування очищує інші. Будь-яка інша команда також
очищує всі очікування. Службовий текст не передається analyzer.

## 3. Реєстр користувачів

Адміністративний Spreadsheet має точний заголовок:

```text
telegram_user_id
display_name
telegram_username
status
invite_token
spreadsheet_id
day_start
daily_kcal_goal
```

Підтримуються `invited`, `active`, `blocked`. Відома попередня схема без
`daily_kcal_goal` мігрується додаванням колонки.

`/start <token>` перевіряє одноразовий invite, створює або повторно
використовує персональний Spreadsheet і лише після цього встановлює `active`.
Повне видалення спочатку блокує користувача, потім видаляє Spreadsheet, фото і
рядок реєстру.

`/invite <name>` використовує чинний прямий flow. `/invite` без аргументу
встановлює `INVITE_WAITING_KEY`; наступний текст адміністратора передається
тій самій операції створення invite. `invite-cancel` або інша команда очищує
стан.

## 4. Персональний журнал `food_log`

Назва worksheet задається `MEAL_SHEET_NAME`, типово `food_log`. Точний
заголовок:

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

`timestamp` і `day` записуються як нативні числові значення Google Sheets.
Підтримувана legacy-схема без `day` мігрується вставленням колонки та
backfill. Інша непорожня схема відхиляється.

Облікова дата обчислюється в `APP_TIMEZONE`: локальний час раніше
персонального `day_start` належить попередній даті. Денний і тижневий підсумки
рахуються з рядків, окремі агрегати не зберігаються.

Звичайний додатний `telegram_message_id` унікальний у межах облікової дати.
Повторна доставка того самого update повертає вже записаний результат.

## 5. Аналіз і фото

`normalize_input()` розпізнає цілі ваги, ккал/100 г, скорочення та числа
словами. Analyzer отримує нормалізований текст, явні авторитетні значення й,
за потреби, bytes найбільшого Telegram-фото.

LLM повертає `FoodAnalysis`; `calculate_meal()` детерміновано обчислює калорії
компонентів і всього прийому. Явні значення мають origin `user_text`, оцінені —
`model_estimate`; це визначає позначки `≈`.

Фото записується тільки після успішного аналізу в
`PHOTO_STORAGE_DIR/<telegram_user_id>/<day>-<message_id>.jpg`. Видалення
дозволене лише всередині персонального каталогу. Старі фото без персональної
підтеки переносяться при першому відкритті таблиці.

## 6. Збережені страви

У кожному персональному Spreadsheet є окремий worksheet `saved_meals` із
точним заголовком:

```text
saved_meal_id
source_message_id
display_name
default_total_weight_g
meal_json
```

Модель:

```python
class SavedMeal(BaseModel):
    saved_meal_id: str  # URL-safe, 1..16
    source_message_id: int
    display_name: str  # normalized, 1..80
    default_total_weight_g: int  # 1..10_000
    base_meal: MealResult
```

`meal_json` містить повний готовий `MealResult`. Нові рядки додаються в кінець;
`list_meals()` повертає їх у зворотному порядку. `source_message_id` робить
збереження одного запису ідемпотентним. Невідома схема відхиляється без
міграції та без зміни `food_log`.

Store реалізує `list_meals`, `get`, `find_by_source`, `append`, `rename`,
`set_default_weight`, `delete`. Після зміни результат перечитується. Помилки
read, підтвердженого write та невизначеного write розділені.

Збереження кнопкою, `/save` без назви й з `Нещодавніх` автоматично додає
суфікс `(2)`, `(3)` при збігу назв. `/save <name>` і rename відхиляють зайняту
назву. Один source завжди повертає наявний шаблон.

## 7. Повторне додавання

`scale_meal(meal, target_weight_g)` пропорційно змінює вагу й калорії всіх
компонентів, успадковує origins і очищує `portion_display`, якщо загальна вага
змінена. LLM не викликається.

Callback швидкого додавання збереженої страви:

```text
saved-add:<saved_meal_id>:<weight_g>
```

Вага міститься в самій кнопці: старе меню додає надруковану вагу, але читає
актуальні назву й склад за ID. `saved-weight:<id>` переводить наступний текст у
разову вагу. Rename і default weight змінюють лише шаблон; історія не
переписується.

Для callback-подій використовується стабільний від’ємний event ID:

```python
raw = hashlib.sha256(callback_query.id.encode()).digest()
event_id = -(int.from_bytes(raw[:7], "big") & ((1 << 52) - 1) or 1)
```

Від’ємний ID порівнюється глобально незалежно від accounting day. Це робить
retry одного callback ідемпотентним, а різні натискання — різними прийомами без
нової колонки. Delete callback приймає додатні й від’ємні ID.

Повторний запис використовує звичайний `append_meal()` з порожніми `request` і
`photo_path`:

- saved: `normalized_request=saved_meal:<id>:<weight>g`, model `saved_meal`;
- recent: `normalized_request=recent_meal:<source_id>:<weight>g`, model
  `recent_meal`;
- `effort=none`, token fields і cost порожні.

## 8. Нещодавні

`get_recent_meals(8)` читає `food_log` один раз від останнього рядка,
пропускає малформовані записи та дедуплікує точні `MealResult` за канонічним
JSON у пам’яті. Fingerprint не зберігається.

Callbacks містять source ID і дату:

```text
recent-open:<message_id>:<day>
recent-add:<message_id>:<day>
recent-weight:<message_id>:<day>
recent-save:<message_id>:<day>
```

Перед дією source перечитується. Видалений source повертає stale-відповідь і
нічого не додає. `recent-save` не показується для запису зі `saved_meal:` або
коли цей source уже є в `saved_meals`.

## 9. Telegram handlers і стани

У command menu користувача: `/meals`, `/day`, `/week`, `/goal`, `/help`,
`/tips`. Адміністратор додатково має `/invite`, `/users`, `/block`, `/unblock`,
`/delete`. `/save` зареєстрований як handler, але не показується в меню.

Очікування зберігаються в `context.user_data` трьома простими ключами:

- `GOAL_WAITING_KEY`;
- `SAVED_MEAL_WAITING_KEY` з kind `saved_weight`, `recent_weight`, `rename` або
  `default_weight`;
- `INVITE_WAITING_KEY`.

Inline-списки `/meals`, `Нещодавні` та керування будуються цілком, без
пагінації. Видалення шаблону має окреме підтвердження.

## 10. Надійність і помилки

- Google append/delete перевіряються повторним читанням після помилки API.
- Якщо операція точно не відбулася, повертається write error; якщо стан
  невідомий — uncertain error з проханням перевірити таблицю.
- Source або template, якого вже немає, нічого не створює.
- Логи не повинні містити секрети, тексти прийомів, `meal_json` або фото.
- HTTP-клієнти логуються не вище WARNING, щоб URL Telegram API з token не
  потрапляли в журнал.

## 11. Конфігурація і перевірка

Обов’язкові env: `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_USER_ID`,
`OPENAI_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_FILE`, `USERS_SPREADSHEET_ID`,
`GOOGLE_DRIVE_FOLDER_ID`.

Основні optional env: `USERS_SHEET_NAME`, `MEAL_SHEET_NAME`,
`PHOTO_STORAGE_DIR`, `APP_TIMEZONE`, `DEFAULT_DAY_START`, модель, reasoning
effort і тарифи OpenAI. Окремої змінної для `saved_meals` немає.

Безкоштовна перевірка:

```bash
bash scripts/run_tests.sh
```

Вона запускає compileall, Ruff format/check, mypy, pytest із branch coverage та
`pip check`. Paid LLM eval запускається лише окремо з явним підтвердженням.
