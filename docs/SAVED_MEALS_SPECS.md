# Технічна специфікація: збережені страви, нещодавні та `/invite`

**Аудиторія:** розробники та QA

**Статус:** реалізовано

**Дата:** 9 серпня 2026 року

## 1. Межі й принципи

Документ реалізує лише інкремент із `docs/SAVED_MEALS_PRD.md` поверх чинного
`docs/PRODUCT.md`.

Збережені й нещодавні страви повторно використовують готовий `MealResult` без
LLM. Не додаються БД, кеш, background jobs, нові колонки або міграція
`food_log`, usage statistics, fingerprints чи пагінація.

## 2. Модель і сховище

```python
class SavedMeal(BaseModel):
    saved_meal_id: str  # URL-safe, <= 16 chars
    source_message_id: int
    display_name: str  # normalized whitespace, 1..80 chars
    default_total_weight_g: int  # 1..10_000
    base_meal: MealResult
```

У персональному Spreadsheet створюється worksheet `saved_meals`:

```text
saved_meal_id
source_message_id
display_name
default_total_weight_g
meal_json
```

`meal_json` містить повний `MealResult`. Worksheet має точний заголовок;
невідома непорожня схема відхиляється без змін. `food_log` не мігрується.

`SavedMealStore` у `calories_bot/saved_meals.py` підтримує:

```python
list_meals()
get(saved_meal_id)
find_by_source(source_message_id)
append(saved_meal)
rename(saved_meal_id, name)
set_default_weight(saved_meal_id, weight_g)
delete(saved_meal_id)
```

Нові рядки додаються в кінець, а списки читаються у зворотному порядку.
`source_message_id` забезпечує ідемпотентне збереження одного запису.

Після write/delete store повторно читає ціль. Підтверджена й невизначена
помилки розрізняються так само, як у чинному `GoogleSheetsStore`.

## 3. Назви

Для порівняння назва очищається від зайвих пробілів і переводиться через
`casefold()`.

- Для кнопки збереження, `/save` без назви та збереження з `Нещодавніх`
  конфлікт розв’язується автоматично: `Назва`, `Назва (2)`, `Назва (3)`.
- Для `/save <назва>` й rename зайнята назва повертає валідаційну помилку.
- Повторний source повертає наявний шаблон до перевірки назви.

Окремий fingerprint або стан `save_name_conflict` не потрібні.

## 4. Читання журналу

`MealStore` отримує:

```python
get_meal(day, telegram_message_id) -> StoredMeal | None
get_latest_meal() -> tuple[int, date, StoredMeal] | None
get_recent_meals(limit=8) -> list[RecentMeal]
```

```python
class RecentMeal(BaseModel):
    telegram_message_id: int
    day: date
    meal: MealResult
    normalized_request: str
```

`get_recent_meals()` читає `food_log` один раз від останнього рядка. Малформовані
рядки пропускаються. Однакові `MealResult`, визначені однаковим канонічним JSON
у пам’яті, пропускаються; повертаються перші вісім різних результатів.

## 5. Збереження

Звичайна відповідь отримує callback:

```text
save:<message_id>:<YYYY-MM-DD>
```

Handler читає source row, перевіряє `find_by_source()`, вибирає доступну назву,
зберігає `SavedMeal` зі стандартною вагою
`round_whole(meal.total_weight_g)` і залишає у відповіді лише `Видалити`.

`/save [назва]` читає останній валідний рядок. Якщо його
`normalized_request` починається з `saved_meal:`, handler відповідає, що страва
вже збережена. Інакше використовує той самий save flow.

Видалений source row або stale callback нічого не створює.

## 6. `/meals` і `Нещодавні`

`/meals` одним читанням будує одну inline-клавіатуру з усіма шаблонами:

```text
[ <name> · <default_weight> г ] [ ⚖️ ]
...
[ Нещодавні ] [ Керувати стравами ]
```

Пагінації немає. Довгі назви скорочуються тільки на кнопці.

Callback швидкого додавання містить вагу з конкретної кнопки:

```text
saved-add:<saved_meal_id>:<weight_g>
```

Тому старе меню додає надруковану на ньому вагу, але читає актуальні назву й
склад за ID.

`Нещодавні` будує один список із `get_recent_meals(8)`. Callback елемента
містить source message ID і day та відкриває detail-view з діями:

```text
recent-add:<message_id>:<day>
recent-weight:<message_id>:<day>
recent-save:<message_id>:<day>
```

Перед кожною дією source row читається повторно. `recent-save` не показується,
якщо `normalized_request` має префікс `saved_meal:` або цей source вже знайдено
в `SavedMealStore`.

## 7. Повторний запис і масштабування

Для прямого callback створюється стабільний від’ємний ID:

```python
raw = hashlib.sha256(callback_query.id.encode()).digest()
event_id = -(int.from_bytes(raw[:7], "big") & ((1 << 52) - 1) or 1)
```

Від’ємні ID порівнюються в `GoogleSheetsStore` незалежно від accounting day;
додатні Telegram message ID — за чинними правилами. Це зберігає
ідемпотентність без нової колонки. Delete callback приймає `-?\d+`.

Для ваги, надісланої окремим повідомленням, event ID дорівнює її Telegram
message ID. Час прямого callback — поточний час у `APP_TIMEZONE`; час введеної
ваги — `message.date`.

Збережений або нещодавній `MealResult` масштабується чистою функцією:

```text
scale = target_weight / base_total_weight
item_weight = base_item_weight × scale
item_kcal = item_weight × kcal_per_100g / 100
```

Після цього перераховуються total weight, meal kcal і kcal/100 g. Назва
збереженої страви береться з актуального `display_name`; для нещодавньої — із
source row. Origins успадковуються. `portion_display` очищається при зміні
базової ваги.

Новий рядок використовує чинний `append_meal()`:

- для saved: `normalized_request=saved_meal:<id>:<weight>g`;
- для recent: `normalized_request=recent_meal:<source_id>:<weight>g`;
- `request` і `photo_path` порожні;
- metadata: `model=saved_meal|recent_meal`, `effort=none`, без token cost.

## 8. Керування й стани

Керування читає весь список без пагінації. Rename змінює лише `display_name`,
default weight — лише `default_total_weight_g`, delete — лише рядок
`saved_meals`. Історія та фото не змінюються.

Для наступного тексту використовується один `SAVED_MEAL_WAITING_KEY` із kind:

```text
saved_weight
recent_weight
rename
default_weight
```

`Скасувати` або інша команда очищує стан. Валідаційна помилка залишає його для
повторної спроби.

## 9. `/invite` без аргументу

Чинний `/invite <ім’я>` не змінюється. Для admin `/invite` без аргументу:

1. встановити `INVITE_WAITING_KEY`;
2. відповісти `Введи ім’я нового користувача.` з `Скасувати`;
3. наступний непорожній текст передати чинній операції `create_invite()`;
4. після успіху або скасування очистити стан.

Це локальна зміна `TelegramHandlers.invite()` і `text()`; command menu та інші
admin handlers не змінюються. Не-admin отримує чинну відповідь `Недоступно.`

## 10. Маршрутизація станів

Залишаються три прості ключі: чинний goal state, saved-meal state та invite
state. Спільний state framework не створюється.

Команда або callback, що починає очікування, спочатку очищує інші два стани.
Інша команда очищує всі очікування. Для некомандного тексту порядок:

1. invite state для адміністратора;
2. goal state;
3. saved/recent state;
4. чинний food flow.

Службовий текст не передається analyzer.

## 11. Реєстрація handlers і помилки

Додаються `/save`, `/meals`, saved/recent callbacks та `invite-cancel`.
`/meals` входить до user/admin command menu; `/save` — ні. `/invite` у меню не
змінюється.

Для saved worksheet достатньо `SavedMealsReadError`, `SavedMealsWriteError` і
`SavedMealsWriteUncertainError`; конфлікт явної назви є звичайною
валідаційною помилкою. Stale ID повертає `None`.

Помилка до append не змінює підсумок. Невизначений append використовує чинне
повідомлення з проханням перевірити останній запис. Логи не містять назв,
`meal_json`, текстів, фото чи секретів.

## 12. Файли й тести

Основні зміни:

- `models.py`: `SavedMeal`, `RecentMeal`, scaling helper;
- `saved_meals.py`: worksheet store;
- `sheets.py`: три read-операції та підтримка від’ємних event ID;
- `workspace.py`, `bot.py`, `main.py`: store, flows, states і handlers;
- `help.txt`, `tips.txt`: коротка довідка;
- `tests/`: unit та інтеграційні сценарії.

Обов’язкові тести:

- save/idempotence, автоматичний суфікс і конфлікт явної назви;
- `/meals` і довгий список без пагінації;
- recent order, deduplication, stale source, add/weight/save;
- масштабування одного й кількох компонентів;
- різні callback створюють різні записи, retry — ні;
- delete з від’ємним ID;
- rename/default/delete без зміни історії;
- взаємне скасування goal/saved/invite states;
- `/invite`, `/invite <ім’я>`, cancel і admin-only;
- регресія text/photo, `/day`, `/week`, `/goal`, access та admin flows.

Після реалізації:

```bash
bash scripts/run_tests.sh
```

Paid LLM eval не потрібен, якщо analyzer не змінюється.

## 13. Поза технічним обсягом

- Text aliases і natural-language intent routing.
- Редактор компонентів і пропорцій.
- Пошук, custom sorting, pagination, БД, кеш і background jobs.
- Нові колонки або міграція `food_log`.
