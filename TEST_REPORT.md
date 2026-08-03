# Test Report

Дата локальної перевірки: 2026-08-02  
Середовище: Windows, Python 3.12.13

## Автоматизовані перевірки

| Перевірка | Результат |
|---|---|
| Python compile | PASS |
| Ruff format | PASS |
| Ruff lint | PASS |
| Mypy strict | PASS, 8 source files |
| Pytest | PASS, 103 tests |
| Branch coverage | PASS, 94.47% |
| `pip check` | PASS |
| `pip-audit` | PASS, known vulnerabilities not found |

JUnit і coverage XML збережено поза репозиторієм у
`C:\tmp\calories-bot-live-test\results\local`.

## Live OpenAI, Google Sheets і Telegram

Статус: **PENDING — credentials ще не надані локально**.

Для запуску потрібні `.env.live` і `google-service-account.json` у
`C:\tmp\calories-bot-live-test`, підготовлена Telegram-група та аркуш
`food_log_test`. Секретні значення не повинні потрапляти в цей звіт.

## Ubuntu/systemd

Статус: **PENDING — VPS SSH key/host ще не надані локально**.

Після live E2E потрібно перевірити deployment, права секретів, start/restart,
pending update, journal і стабільну роботу протягом 10 хвилин.

## Відкриті блокери приймання

- Live credentials відсутні.
- Telegram E2E потребує ручного надсилання тестового набору користувачем.
- Фінальний `food_log` не створюється до успішного live-приймання.
