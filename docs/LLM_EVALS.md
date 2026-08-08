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
  --report eval-results/current.json
```

Без `--confirm` runner лише покаже заплановану кількість запитів. Ключ береться
з чинного `.env`, але ніколи не друкується та не записується у звіт.

Набір у `evals/cases.jsonl` містить українські й російські описи, явні значення,
побутові порції, складені страви, нехарчові повідомлення та синтетичні фото.
Персональні фото користувачів сюди не додаються. Успішний запуск — не менше 90%
пройдених кейсів і жодної помилки API, structured output або source ID.

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
