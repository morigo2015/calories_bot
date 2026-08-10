# План реалізації локального eval-dashboard

## 1. Мета

Додати до `calories-bot` простий локальний інтерфейс для:

- перегляду поточних і попередніх LLM eval-запусків;
- порівняння будь-яких двох запусків незалежно від того, що між ними
  змінилося: prompt, модель, effort, дата, версія API або dataset;
- детального перегляду кейсів, на яких eval завершився невдало;
- перегляду й простого редагування ground truth dataset.

Це інструмент для одного власника невеликого проєкту, а не production analytics
platform. Рішення має залишатися локальним, простим у підтримці та не вимагати
хмарних workflow.

## 2. Принципові рішення

### Використовувати

- JSONL-файл `evals/cases.jsonl` як єдине джерело ground truth;
- окремий immutable JSON-звіт для кожного eval-запуску;
- маленький Python HTTP server, який запускається тільки за потреби;
- HTML, CSS і мінімальний vanilla JavaScript без frontend framework;
- стандартну бібліотеку Python, якщо немає вагомої причини для залежності.

### Не використовувати у v1

- Codex Sites;
- OpenAI Evals service;
- SQLite або іншу БД;
- React, Next.js, Streamlit, Flask чи інший web framework;
- постійно запущений dashboard service;
- cloud storage або синхронізацію даних;
- authentication і публічний доступ;
- порівняння більше двох запусків одночасно;
- графіки, trend-аналітику, tags і складні фільтри;
- автоматичну переоцінку старих відповідей за новим ground truth;
- повний історичний dashboard для кожного unit-тесту.

Sites свідомо не використовується: eval-дані, dataset, зображення і grader живуть
у checkout на Hetzner. Hosted UI потребував би окремої передачі звітів,
persistence та двосторонньої синхронізації dataset, що для цього проєкту зайве.

## 3. Чинний стан проєкту

Перед реалізацією перечитати фактичні версії файлів, оскільки цей документ може
виконуватися пізніше:

- `scripts/eval_llm.py` — платний runner;
- `scripts/run_tests.sh` — загальна команда перевірки;
- `evals/cases.jsonl` — ground truth;
- `evals/images/` — синтетичні test images;
- `tests/test_llm_eval.py` — тести grader і loader;
- `docs/LLM_EVALS.md` — поточна інструкція;
- `.gitignore` — `eval-results/` вже має бути ignored.

На момент написання документа runner:

- підтримує `--case`, `--config`, `--repeat`, `--min-pass-rate`, `--report` і
  обов'язковий `--confirm`;
- зберігає pass rate, hard failures, latency, tokens, cost і список checks;
- не зберігає назву запуску, timestamp, Git/prompt/dataset metadata;
- не зберігає фактичний `FoodAnalysis`, normalized input або snapshot expected;
- створює звіт тільки коли явно передано `--report`;
- використовує ключ `runs` для конфігурацій усередині одного запуску. Не
  трактувати ці елементи як історичні запуски dashboard.

У `eval-results/` можуть уже лежати legacy reports на кшталт `current.json`,
`luna-low.json` і `caption-comparison.json`. Їх не видаляти й не переписувати.

Робоче дерево може містити сторонні незакомічені зміни. Перед редагуванням
перевірити `git status` і не змінювати файли, які не стосуються dashboard.

## 4. Користувацький сценарій

### Запуск eval

Ручна назва:

```bash
bash scripts/run_tests.sh --llm --confirm \
  --name "Після зміни photo prompt"
```

Автоматична назва, якщо `--name` не передано:

```text
Run 2026-08-08 15:42 UTC
```

Кожен підтверджений eval автоматично пише звіт у:

```text
eval-results/runs/<timestamp>_<run-id>.json
```

Явний `--report PATH` залишити для backward compatibility. Він може створювати
додаткову копію того самого report, але canonical historical report має
зберігатися завжди.

Без `--confirm` runner, як і зараз, лише показує план запитів і нічого не
записує до історії.

### Запуск dashboard

```bash
python -m scripts.eval_dashboard
```

Server:

- за замовчуванням слухає тільки `127.0.0.1`;
- використовує фіксований параметризований порт, наприклад `8765`;
- друкує точний URL після старту;
- не запускається автоматично через systemd;
- не має доступу до `.env`, API key, Telegram token або production data.

Дозволити `--host` і `--port` лише як явні CLI options. Не робити
`0.0.0.0` default.

## 5. Report schema v2

Додати `schema_version: 2`. Один JSON-файл описує одну CLI invocation, навіть
якщо в ній було кілька конфігурацій або repeat.

Орієнтовна структура:

```json
{
  "schema_version": 2,
  "run_id": "20260808T154200Z-a1b2c3",
  "name": "Після зміни photo prompt",
  "started_at": "2026-08-08T15:42:00Z",
  "finished_at": "2026-08-08T15:42:48Z",
  "git": {
    "commit": "bd950f1",
    "dirty": true
  },
  "prompt_sha256": "...",
  "dataset_sha256": "...",
  "cases_path": "evals/cases.jsonl",
  "minimum_pass_rate": 0.9,
  "passed": true,
  "dataset_snapshot": [],
  "configurations": []
}
```

Вимоги:

- `run_id` незмінний і не залежить від display name;
- display name може містити Unicode;
- timestamps зберігати в UTC ISO 8601;
- Git metadata отримувати best-effort; відсутність Git не ламає eval;
- `prompt_sha256` рахувати від фактичного `SYSTEM_PROMPT`;
- `dataset_sha256` рахувати від bytes використаного `cases.jsonl`;
- `dataset_snapshot` містить точні case objects, використані в запуску;
- не дублювати image bytes у кожному report: зберігати relative path і SHA-256;
- report не містить secrets, base64 images або повних API request bodies.

`configurations` є новою назвою для поточного внутрішнього `runs`. Кожна
конфігурація містить чинні summary fields і `results`.

Кожен case result додатково має містити:

- `repeat_index`;
- `normalized_input` без image bytes;
- фактичний serialized `FoodAnalysis`, якщо відповідь отримано;
- усі поля item, необхідні для діагностики, включно з origins, estimated flags,
  `weight_source_id` та `kcal_source_id`;
- checks із `name`, `passed`, `actual`, `expected`;
- latency, input/output tokens і cost;
- sanitized error type/message для hard failure.

Не використовувати звичайний `model_dump()`, якщо Pydantic виключає source ID.
Зробити явний serializer для eval report і покрити його тестом.

Для sanitized errors дозволені class name, контрольоване повідомлення, HTTP
status/request ID, якщо вони доступні без request body. Не записувати `repr()`
довільного exception, headers або credentials.

## 6. Dashboard v1

### 6.1. Список запусків

Головна сторінка читає `eval-results/runs/*.json` і показує найновіші першими.

Для кожного run:

- name;
- timestamp;
- passed/total і pass rate;
- hard failures;
- total cost;
- average latency;
- model/effort дрібним metadata, а не головною категорією;
- кнопки `Details` і `Compare`.

Якщо invocation містить кілька configurations, показати їх усередині run.
Для compare користувач вибирає одну configuration з кожного run; якщо вона одна,
вибрати автоматично.

Legacy v1 reports показувати read-only з позначкою `Legacy report`. Відсутні
name/timestamp/input/actual не вигадувати. Ім'я можна взяти з filename. Якщо
надійно визначити timestamp неможливо, показати `Unknown`.

### 6.2. Порівняння двох запусків

Показати:

- summary cards для A і B;
- різницю pass rate, total cost і average latency;
- список `Regressions`: pass у A, fail у B;
- список `Improvements`: fail у A, pass у B;
- таблицю всіх case IDs зі статусом A/B;
- перемикач `Changed only`.

Порівнювати за `case_id` і `repeat_index`. Якщо dataset/repeat відрізняються,
показати відсутній кейс як `Not run`, а не failure.

Якщо dataset hashes різні, показати помітне попередження: результати можуть
відрізнятися через ground truth або склад набору.

Не робити графіків у v1.

### 6.3. Деталі run і failure

Сторінка run спочатку показує failures, потім passed cases.

Для кожного result у detail view показати:

- case ID;
- оригінальний text;
- image preview, якщо case має image;
- normalized text;
- expected object зі snapshot;
- фактичний `FoodAnalysis` у читабельній таблиці;
- список checks із підсвіченим actual/expected для failed checks;
- hard failure message;
- latency, tokens і cost.

Для explicit values окремо показати source IDs, values, origins і estimated
flags. Це основний diagnostic view, тому його не скорочувати до одного error
рядка.

Image server повинен дозволяти тільки safe relative paths усередині
`evals/images/`. Заборонити `..`, absolute paths і symlink escape.

## 7. Dataset viewer/editor v1

### Перегляд

Сторінка `/dataset` показує кейси в порядку JSONL:

- ID;
- text або image preview;
- compact expected summary;
- кнопку `View/Edit`.

### Редагування

Не робити form-builder для кожного expected field. Для вибраного кейсу показати:

- читабельний preview;
- JSON textarea з одним case object;
- `Validate`;
- `Save`.

Додатково підтримати `Add case` як порожній JSON object/template. `Duplicate` і
видалення не потрібні у v1; їх можна зробити вручну у файлі або додати пізніше.

Перед save:

- JSON має бути object;
- `id` — непорожній string і унікальний;
- `text` — string, якщо присутній;
- `image` — safe relative path, якщо присутній;
- має бути `expected` object із boolean `is_food`;
- діапазони мають бути масивами з двох чисел, min <= max;
- весь результатний JSONL повторно проходить загальну dataset validation.

Зберігати атомарно: temporary file у тому самому каталозі, flush/fsync за
можливості, потім `os.replace`. Не пошкоджувати решту dataset при помилці.
Зберегти owner write permission. Не змінювати права secrets.

Під час редагування existing case замінювати лише відповідний JSONL-рядок і
зберігати порядок та сторонні blank/comment lines. Новий case додавати в кінець.

Dataset editor змінює тільки ground truth. Він не запускає LLM, не перераховує
старі runs і не змінює historical snapshots.

## 8. Звичайні тести у dashboard

У v1 не будувати історичну матрицю всіх pytest tests.

Якщо це не ускладнює `scripts/run_tests.sh`, дозволяється додати лише compact
summary останнього deterministic run:

- overall pass/fail;
- кількість pytest passed/failed;
- coverage;
- назви failed tests.

Це другорядний пункт. Історія LLM eval і failure drill-down мають пріоритет.
Не додавати pytest plugins лише заради dashboard. Pytest JUnit XML і coverage
JSON достатні, але інтеграцію можна відкласти, якщо вона розширює scope.

## 9. Запропонована структура змін

Мінімально:

- змінити `scripts/eval_llm.py` — name, metadata, schema v2, automatic history;
- додати `scripts/eval_dashboard.py` — local server, HTML rendering, editor;
- за потреби додати невеликий `scripts/eval_storage.py` лише якщо це справді
  зменшує дублювання між runner і dashboard;
- змінити `scripts/run_tests.sh` лише для передачі eval arguments; поточна форма
  вже передає їх після `--llm` і може не потребувати змін;
- розширити `tests/test_llm_eval.py`;
- додати `tests/test_eval_dashboard.py`;
- оновити `docs/LLM_EVALS.md`;
- не змінювати production bot handlers, Google Sheets або Telegram flow.

Не перетворювати `scripts/` на великий application package. Якщо
`eval_dashboard.py` стає надто великим, дозволяється розділити storage/schema,
але не додавати framework.

## 10. Етапи реалізації

### Етап 1. Report v2 і автоматична історія

1. Додати `--name`.
2. Додати run identity/timestamps та hashes.
3. Додати dataset snapshot, normalized input і actual analysis.
4. Завжди атомарно зберігати canonical report після підтвердженого запуску,
   включно з failed run.
5. Зберегти `--report` і чинні CLI options.
6. Додати unit-тести schema/serialization/path/name.

Не робити реальних API calls у unit-тестах.

### Етап 2. Read-only dashboard

1. Реалізувати local server і run index.
2. Додати run details/failure details.
3. Додати compare A/B і `Changed only`.
4. Додати safe serving test images.
5. Підтримати legacy reports настільки, наскільки дозволяють наявні поля.

### Етап 3. Dataset viewer/editor

1. Додати dataset list і case preview.
2. Додати raw JSON validate/save.
3. Додати `Add case`.
4. Додати atomic-write, path traversal і validation tests.

### Етап 4. Документація й перевірка

1. Оновити `docs/LLM_EVALS.md` з точними командами.
2. Запустити повну безкоштовну перевірку:

   ```bash
   bash scripts/run_tests.sh
   ```

3. Перевірити `git diff --check` і права файлів.
4. Вручну відкрити dashboard на synthetic fixture reports або на наявних
   ignored legacy reports.

Не запускати paid LLM eval без нового явного підтвердження користувача в тому
завданні. Наявність ключа або попередні підтвердження з інших чатів не є
дозволом.

## 11. Тестування

Обов'язкові тести без реального OpenAI API:

- manual і automatic run names;
- унікальний run ID і UTC timestamps;
- canonical report path;
- report пишеться для pass і failure;
- `--confirm` gate залишається чинним;
- prompt/dataset/image hashes;
- explicit source IDs присутні в serialized analysis;
- dataset snapshot не змінюється після редагування source file;
- secrets і base64 image відсутні у report;
- load/schema v2;
- graceful legacy report parsing;
- compare pass/pass, pass/fail, missing case і different dataset;
- dashboard HTML escapes dataset/model output;
- image path traversal блокується;
- valid case edit;
- invalid JSON, duplicate ID, invalid expected/range;
- failed save не пошкоджує original JSONL;
- add case append;
- server bind defaults to `127.0.0.1`.

HTTP tests мають запускати handler на temporary directories або тестувати
route/service functions без відкриття зовнішнього порту. Не торкатися реального
`evals/cases.jsonl` у тестах.

## 12. Безпека і приватність

- Dashboard local-only by default.
- Не читати й не показувати `.env`.
- Не передавати дані у Sites або інші зовнішні сервіси.
- Не зберігати OpenAI key, Telegram token, request headers чи base64 photos.
- Дозволяти image paths тільки під `evals/images/`.
- HTML-escape весь text, expected, actual і errors до rendering.
- Dataset editor не має доступу до довільних filesystem paths.
- Не додавати реальні персональні Telegram messages/photos у Git або fixtures.

## 13. Критерії готовності

Робота завершена, коли:

1. Кожен підтверджений eval автоматично отримує name і historical JSON report.
2. Історія не перезаписується новим запуском.
3. Dashboard показує список runs і працює без OpenAI API.
4. Можна порівняти рівно два runs і побачити regressions/improvements.
5. Для failed case видно input/image, expected, actual і failed checks.
6. Ground truth можна переглянути, перевірити та зберегти через raw JSON editor.
7. Старий run зберігає власний dataset snapshot після редагування ground truth.
8. Legacy reports не ламають dashboard.
9. Немає нової БД, cloud deployment, Sites або постійного service.
10. `bash scripts/run_tests.sh` проходить без регресій.

## 14. Поза scope v1

- Sites deployment;
- remote/public dashboard;
- multi-user access;
- authentication;
- SQLite/D1/R2;
- charts і довгі trend lines;
- порівняння більше двох runs;
- tags/categories/notes;
- редагування historical reports;
- regrading старих actual outputs новим grader/ground truth;
- автоматичний запуск eval за cron або після deployment;
- production Telegram дані;
- LLM-as-judge.

## 15. Handoff після реалізації

Не commit, push і не restart автоматично. Зберегти unrelated user changes.

Якщо реалізація змінює application/test tooling, після успішних перевірок дати
користувачу команду:

```bash
bash /home/igor/calories-bot/run_update.sh "Додати локальний dashboard для eval-запусків"
```
