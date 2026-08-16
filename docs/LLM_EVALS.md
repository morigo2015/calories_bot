# Сталі перевірки та LLM eval

Для цього бота використовується простий локальний eval-runner. OpenAI Evals не
потрібен: набір невеликий, очікування легко описуються інваріантами, а поточний
Evals API [виводиться з експлуатації у 2026 році](https://developers.openai.com/api/docs/guides/evals).

## Звичайна повна перевірка

```bash
bash scripts/run_tests.sh
```

Команда запускає compile, Ruff, mypy, pytest з coverage та `pip check`. Вона не
використовує OpenAI API і нічого не змінює в робочому боті.

## Платний LLM-блок

```bash
bash scripts/run_tests.sh --llm --confirm \
  --name "Після зміни photo prompt"
```

Без `--confirm` runner лише покаже заплановану кількість запитів. Ключ береться
з чинного `.env`, але ніколи не друкується та не записується у звіт.

Кожен підтверджений запуск автоматично отримує immutable JSON-звіт у
`eval-results/runs/`. Якщо `--name` не передано, назва має формат
`Run 2026-08-08 15:42 UTC`. Опція `--report PATH` і далі підтримується та створює
додаткову копію того самого звіту, але не замінює автоматичний historical report.

Звіт schema v2 містить Git/prompt/dataset metadata, snapshot використаних кейсів,
нормалізований input, checks і фактичний `FoodAnalysis`. Для photo cases у ньому є
лише relative image path і SHA-256 — image bytes, API key та request body не
зберігаються.

Набір у `evals/cases.jsonl` містить українські й російські описи, явні значення,
побутові порції, складені страви, нехарчові повідомлення та синтетичні фото.
Для складених страв runner уміє перевіряти кожен названий компонент окремо,
його вагу, калорійність і порцію, а також сумарну вагу. Є окремі сценарії з
пропущеною пунктуацією, русизмами, повторами, обірваною думкою, приблизними
порціями та сумішшю точних і нечітких кількостей.
Персональні фото користувачів сюди не додаються. Успішний запуск — не менше 90%
пройдених кейсів і жодної помилки API, structured output або source ID.

Окремий набір `evals/weekly_meal_grouping.jsonl` перевіряє групування для
`/weekly_meals`: варіанти брендів, кольорів і форм слова, складені назви,
небезпечні «хибні друзі» на кшталт `кава`/`кавовий торт`, а також довгий
реалістичний список із лімітом у 20 категорій. Модель повертає типізовану
відповідність `source_id → group_name` через
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
а grader перевіряє потрібні об’єднання, розділення, назви категорій і ліміт.

## Вибір model / effort

Спочатку запускається чинна конфігурація. Якщо є невдалі кейси, порівнюються
лише вони, наприклад:

```bash
python -m scripts.eval_llm --confirm \
  --case composite_breakfast --case photo_label \
  --config gpt-5.6-luna:none \
  --config gpt-5.6-luna:low \
  --config gpt-5.6-terra:none
```

Два найкращі варіанти варто повторити тричі на всьому наборі:

```bash
python -m scripts.eval_llm --confirm --repeat 3 \
  --config gpt-5.6-luna:low \
  --config gpt-5.6-terra:none \
  --report eval-results/comparison.json
```

Обирається найдешевша й найшвидша конфігурація, яка стабільно проходить поріг.
Змінювати модель тільки через один випадковий прогін не слід. Такий підхід
відповідає рекомендаціям OpenAI: task-specific кейси, автоматичний запуск,
чіткі критерії та постійне поповнення регресійного набору
([Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)).

Для незалежного вибору конфігурації групування використовується task
`weekly-meals`:

```bash
python -m scripts.eval_llm --task weekly-meals --confirm \
  --config gpt-5.6-luna:none \
  --config gpt-5.6-luna:low \
  --config gpt-5.6-terra:none \
  --name "Порівняння weekly meals"
```

Без `--config` runner бере `WEEKLY_MEALS_LLM_MODEL` та
`WEEKLY_MEALS_LLM_REASONING_EFFORT`, а за їх відсутності — звичайні
`OPENAI_MODEL` та `OPENAI_REASONING_EFFORT`. Для стабільності дві найкращі
конфігурації слід прогнати з `--repeat 3`. Це окремі платні запити; звичайний
`bash scripts/run_tests.sh` їх не виконує.

## Локальний dashboard

### Запуск і перегляд із локальної Windows-машини

Сервер доступний через SSH alias `hetzner` (тобто команда підключення —
`ssh hetzner`). Найпростіше одночасно запустити dashboard на VPS і створити
захищений SSH-тунель. У локальному Windows PowerShell виконати:

```powershell
ssh -t -L 8765:127.0.0.1:8765 hetzner "cd /home/igor/calories-bot && .venv/bin/python -m scripts.eval_dashboard"
```

Не закривати це вікно PowerShell, поки dashboard потрібен. Після появи рядка
`Eval dashboard: http://127.0.0.1:8765/` відкрити в локальному браузері:

```text
http://127.0.0.1:8765
```

`127.0.0.1` у команді dashboard — це localhost VPS, а `127.0.0.1` у браузері —
localhost Windows. SSH-тунель з'єднує ці два порти. Зупинка dashboard і тунелю —
`Ctrl+C` у PowerShell.

Якщо dashboard уже запущений в окремій SSH-сесії на VPS, у локальному PowerShell
потрібно підняти тільки тунель:

```powershell
ssh -N -L 8765:127.0.0.1:8765 hetzner
```

Якщо локальний порт `8765` зайнятий, можна використати інший локальний порт,
наприклад:

```powershell
ssh -N -L 9876:127.0.0.1:8765 hetzner
```

Тоді dashboard відкривається за адресою `http://127.0.0.1:9876`.
Не запускати dashboard з `--host 0.0.0.0`: у dataset editor немає авторизації,
тому відкривати його в публічну мережу небезпечно.

### Коротка довідка по dashboard

#### Runs

Головна сторінка показує eval-запуски від найновіших до найстаріших. Один запуск
може містити кілька configurations, якщо runner викликався з кількома
`--config`. Для кожної configuration видно:

- скільки результатів пройшло та загальний pass rate;
- hard failures, загальну вартість і середню latency;
- model та reasoning effort;
- кнопки `Details` і `Compare`.

Позначка `Legacy report` означає старий звіт: його можна переглядати й
порівнювати, але normalized input, actual response, timestamp та інші нові поля
можуть бути відсутні.

#### Details

`Details` відкриває одну configuration. Спочатку показуються failures, нижче —
passed cases. У кожному кейсі є original text/photo, normalized text, expected
snapshot, фактичний `FoodAnalysis`, checks, latency, tokens і cost. У таблиці
actual особливо корисні `source ID`, `origin` та `estimated`: вони показують, чи
модель правильно використала явні вагу та калорійність із повідомлення.

#### Compare

Для звичайного workflow спочатку вибрати baseline як `A`, а новий запуск як `B`.
Це можна зробити двома способами: через два dropdown на головній сторінці або
кнопкою `Compare` біля baseline з подальшим вибором `B`.

- `Regressions` — кейс пройшов у A, але не пройшов у B;
- `Improvements` — кейс не пройшов у A, але пройшов у B;
- `B − A` — зміна pass rate, cost і average latency;
- `Not run` — у цьому запуску немає відповідного `case_id`/`repeat_index`;
- `Changed only` — приховує кейси з однаковим статусом.

Попередження `Different datasets` означає, що склад кейсів або ground truth між
запусками змінився, тому різниця може бути спричинена не лише prompt/model.

#### Dataset

`Dataset` показує чинний `evals/cases.jsonl` у початковому порядку.

- `View/Edit` відкриває raw JSON одного case;
- `Validate` перевіряє JSON і весь dataset, але нічого не зберігає;
- `Save` атомарно зберігає case після такої самої перевірки;
- `Add case` додає новий унікальний case у кінець файлу.

Редагування dataset не запускає платний eval і не змінює старі reports. Після
зміни ground truth треба окремо запустити новий eval, якщо потрібні нові
результати.

### Рекомендований робочий сценарій

1. Перед зміною prompt/model запустити повний eval із зрозумілим `--name` — це A.
2. Внести зміну й запустити ще один повний eval з іншою назвою — це B.
3. Відкрити dashboard через SSH-тунель і порівняти A з B.
4. Спочатку перевірити `Regressions`, потім cost/latency і failure `Details`.
5. Якщо проблема в ground truth, виправити case через `Dataset`, а потім створити
   новий eval-запуск; старий report залишиться незмінним.

### Окремий запуск server на VPS

Якщо зручніше тримати dashboard і тунель у різних терміналах, у SSH-сесії на
VPS запустити:

```bash
.venv/bin/python -m scripts.eval_dashboard
```

Dashboard слухає тільки `127.0.0.1:8765` і після старту друкує точний URL.
Інший bind або port треба передати явно:

```bash
.venv/bin/python -m scripts.eval_dashboard --host 127.0.0.1 --port 9000
```

Dashboard не читає `.env`, не потребує OpenAI API та не запускається як systemd
service.
