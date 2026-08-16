# Telegram E2E: підготовка тестового акаунта

Для E2E використовується окремий Telegram-акаунт. Його API credentials і
Telethon session зберігаються поза репозиторієм.

## 1. Локальні каталоги

```bash
install -d -m 700 /home/igor/.config/calories-bot
install -d -m 700 /home/igor/.local/state/calories-bot-e2e
touch /home/igor/.config/calories-bot/e2e.env
chmod 600 /home/igor/.config/calories-bot/e2e.env
```

## 2. Конфігурація

Відкрити файл:

```bash
nano /home/igor/.config/calories-bot/e2e.env
```

Вміст:

```dotenv
TELEGRAM_E2E_API_ID=
TELEGRAM_E2E_API_HASH=
TELEGRAM_E2E_PHONE=+380XXXXXXXXX
```

OTP і пароль 2FA у файл не додаються. Необов'язковий
`TELEGRAM_E2E_SESSION` потрібен лише для зміни стандартного шляху
`/home/igor/.local/state/calories-bot-e2e/test-user.session`.

## 3. Одноразова авторизація

```bash
cd /home/igor/calories-bot
.venv/bin/python -m scripts.telegram_e2e_auth
```

Після введення коду з Telegram та, за потреби, пароля 2FA створюється
`/home/igor/.local/state/calories-bot-e2e/test-user.session` з правами `600`.

Повторна безпечна перевірка без надсилання нового коду:

```bash
.venv/bin/python -m scripts.telegram_e2e_auth --check
```

## 4. Повний user journey

Runner показує план і нічого не надсилає без `--confirm`:

```bash
bash scripts/run_tests.sh --e2e
```

Підтверджений запуск:

```bash
bash scripts/run_tests.sh --e2e --confirm
```

Перевіряються `/start`, `/help`, `/tips`, `/weekly_calories`, `/weekly_meals`,
зміна цілі, два послідовні
прийоми їжі, `/day`, формула для штучних порцій, некоректний формат, нехарчове
повідомлення та фото з явною вагою. Окремий блок перевіряє точну трикомпонентну
страву й неохайно сформульовані багатокомпонентні страви: кількість окремих
Telegram-відповідей і рядків Google Sheets, збереження одного компонента, зміну
його ваги, видалення сусіднього компонента, повторне додавання зі збережених,
повторну зміну ваги та видалення шаблону через `/meals`.

Усі тестові записи видаляються кнопками бота, бібліотека збережених страв і
попередня денна ціль відновлюються до точного baseline. Звіт записується в
ігнорований каталог `eval-results/`.

Cleanup робить короткі паузи й до трьох спроб, щоб серія callback-дій не
створювала штучний burst до Google API. Якщо UI-очищення все одно не завершилось,
runner безпечно видаляє лише суцільний суфікс рядків після зафіксованого baseline
і позначає прогін невдалим: тестовий акаунт відновлюється, але дефект не маскується.

Session-файл дає повний доступ до тестового акаунта. Його не можна надсилати,
копіювати до репозиторію або використовувати для особистого акаунта.
