# Технічна специфікація Calories Bot

**Статус:** актуальна реалізована версія станом на 10 серпня 2026 року.

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
3. нова вага прийому або назва/стандартна вага saved meal;
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

`get_day_summary()` передає поточний `daily_kcal_goal` у `format_day_reply()`.
Перший рядок `/day` має заголовок `Сьогодні` й містить суму, ціль і залишок.
Якщо суму перевищено, ціль лишається видимою та показується величина
перевищення.

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
icon
```

Модель:

```python
class SavedMeal(BaseModel):
    saved_meal_id: str  # URL-safe, 1..16
    source_message_id: int
    display_name: str  # normalized, 1..80
    default_total_weight_g: int  # 1..10_000
    base_meal: MealResult
    icon: str | None
```

`meal_json` містить повний готовий `MealResult`. Нові рядки додаються в кінець;
`list_meals()` повертає їх у зворотному порядку. `source_message_id` робить
збереження одного запису ідемпотентним. Попередня схема без `icon` мігрується
додаванням порожньої останньої колонки. Інша невідома схема відхиляється без
зміни `food_log`.

Store реалізує `list_meals`, `get`, `find_by_source`, `append` і `delete`.
Після зміни результат перечитується. Помилки read, підтвердженого write та
невизначеного write розділені.

Збереження кнопкою, `/save` без назви й з `Нещодавніх` автоматично додає
суфікс `(2)`, `(3)` при збігу назв. `/save <name>` і rename відхиляють зайняту
назву. Один source завжди повертає наявний шаблон.

Під час першого збереження `suggest_meal_icon()` робить окремий Structured
Outputs запит із назвою та компонентами й повертає `emoji` та `confidence`.
Іконка зберігається тільки при `confidence >= 0.8`. Помилка або низька оцінка
дає `icon=None` і не перериває основне збереження. Повторне збереження того
самого source не викликає LLM знову.

## 7. Повторне додавання

`scale_meal(meal, target_weight_g)` перераховує вагу й калорії, успадковує
origins і очищує `portion_display`, якщо вага змінена. Зміна ваги дозволена
лише для `len(meal.items) == 1`; складена страва повертає
`CompositeMealWeightError`. LLM для перерахунку не викликається.

Callback швидкого додавання збереженої страви:

```text
saved-add:<saved_meal_id>:<weight_g>
```

Вага міститься в самій кнопці: натискання одразу додає надруковану стандартну
вагу, але читає актуальні назву й склад за ID. Окремої кнопки ваги в `/meals`
немає. Rename змінює назву шаблону; default weight доступна лише для одного
компонента. Історія при керуванні шаблоном не переписується.

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

## 8. Редагування ваги прийому

Кожна стандартна відповідь має callback:

```text
meal-weight:<message_id>:<day>
```

Handler повторно читає source. Для кількох компонентів він одразу показує
alert і не створює стан. Для одного компонента встановлюється
`MEAL_WEIGHT_WAITING_KEY` із kind `meal_weight`, source ID/day та Telegram ID
бот-повідомлення з результатом.

Наступне ціле число масштабує один компонент. `update_meal()` одним
`batch_update` замінює в тому самому рядку `meal_name`, загальну вагу,
калорійність, kcal/100 g, `items_json` і `estimated`, після чого перечитує рядок
та денний підсумок. Timestamp, source request, metadata й фото не змінюються.
Бот редагує попередню відповідь у Telegram та лишає стандартні кнопки. Зміна
рядка, створеного із saved template, не змінює сам template.

## 9. Нещодавні

`get_recent_meals(8)` читає `food_log` один раз від останнього рядка,
пропускає малформовані записи та дедуплікує точні `MealResult` за канонічним
JSON у пам’яті. Fingerprint не зберігається.

`/recent` входить до command menu та відразу показує весь список. Кожна кнопка
містить source ID, дату й надруковану вагу:

```text
recent-add:<message_id>:<day>:<weight_g>
```

Натискання одразу повторює показану порцію; detail-view, інша вага й кнопка
`Нещодавні` всередині `/meals` відсутні. Перед дією source перечитується.
Видалений source повертає stale-відповідь і нічого не додає. Новий результат
має стандартні кнопки, тому за потреби його можна запам’ятати або змінити вагу
одного компонента.

## 10. Telegram handlers і стани

У command menu користувача: `/meals`, `/recent`, `/day`, `/weekly_calories`,
`/weekly_meals`, `/goal`, `/help`, `/tips`. Адміністратор додатково має
`/burned`, `/invite`, `/users`, `/block`, `/unblock`, `/delete`. `/save`
зареєстрований як handler, але не показується в меню. Кожен опис команди й
кожна службова inline-кнопка мають emoji-іконку.

Очікування зберігаються в `context.user_data` трьома простими ключами:

- `GOAL_WAITING_KEY`;
- `MEAL_WEIGHT_WAITING_KEY` з kind `meal_weight` для зміни ваги прийому;
- `INVITE_WAITING_KEY`.

Inline-списки `/meals`, `/recent` та видалення будуються цілком, без
пагінації. `/meals` містить страви та `🗑 Видалити із збережених`; окреме меню
пропонує вибрати шаблон і підтвердити видалення.

## 11. Надійність і помилки

- Google append/update/delete перевіряються повторним читанням після помилки
  API.
- Якщо операція точно не відбулася, повертається write error; якщо стан
  невідомий — uncertain error з проханням перевірити таблицю.
- Source або template, якого вже немає, нічого не створює.
- Логи не повинні містити секрети, тексти прийомів, `meal_json` або фото.
- HTTP-клієнти логуються не вище WARNING, щоб URL Telegram API з token не
  потрапляли в журнал.

## 12. Конфігурація і перевірка

Обов’язкові env: `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_USER_ID`,
`OPENAI_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_FILE`, `USERS_SPREADSHEET_ID`,
`GOOGLE_DRIVE_FOLDER_ID`.

Основні optional env: `USERS_SHEET_NAME`, `MEAL_SHEET_NAME`,
`PHOTO_STORAGE_DIR`, `APP_TIMEZONE`, `DEFAULT_DAY_START`, модель, reasoning
effort, `OPENAI_TIMEOUT_SECONDS` і тарифи OpenAI. Окремої змінної для
`saved_meals` немає.

Безкоштовна перевірка:

```bash
bash scripts/run_tests.sh
```

Вона запускає compileall, Ruff format/check, mypy, pytest із branch coverage не
нижче 75% та `pip check`. Paid LLM eval запускається лише окремо з явним
підтвердженням.
