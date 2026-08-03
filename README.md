# Calories Bot

Telegram-бот для обліку калорій одного користувача в окремій групі або
супергрупі. Бот приймає текст або одне фото страви, аналізує їжу через OpenAI,
записує один рядок на повідомлення в Google Sheets і повертає підсумок за
обліковий день.

## Формат повідомлень

```text
сир 50
сир 50г 120#
#120 сир 50 г
2 яйця, хліб 50
йогурт 200г 65 кк/100гр
батончик 45g 380kcal/100g
```

`#120` і `120#` без пробілу означають 120 ккал/100 г. Також підтримуються
`кк`, `ккал`, змішані кириличні/латинські `к/k/а/a` і `kcal`. Усі варіанти
нормалізуються до `120 ккал/100г`.

Ціле число наприкінці повідомлення або перед комою, `;` чи переносом рядка
вважається вагою і отримує `гр`. Число в іншому місці є частиною опису:
`2 яйця` — кількість, а `піца 30 см` — розмір. Десяткові значення не
підтримуються. Некоректний `#` або десятковий запис відхиляється без викликів
OpenAI та Google.

Якщо ваги чи калорійності немає, бот оцінює значення та позначає результат `≈`.
Нормалізований текст показується першим рядком відповіді.

Також можна надіслати одне фото страви з підписом на кшталт `200 г` або без
підпису. Бот передає фото та підпис у модель, а першим рядком відповіді показує
розпізнану назву страви. Фото в альбомах і зображення, надіслані як документи,
ігноруються.

## Telegram

1. Створіть бота через BotFather.
2. Створіть окрему групу або супергрупу, додайте бота адміністратором і вимкніть
   privacy mode у BotFather.
3. Вкажіть ID групи в `TELEGRAM_CHAT_ID`, а свій ID — у `TELEGRAM_USER_ID`.

Бот ігнорує приватні повідомлення, канали, інші групи та інших користувачів.
Підтримуються `/start` і `/help` з однаковою короткою довідкою.

Щоб безпечно знайти ID, зупиніть бот, надішліть повідомлення в групу і запустіть:

```bash
python scripts/discover_telegram_ids.py --env-file C:\tmp\calories-bot-live-test\.env.live
```

Helper показує лише знайдені ID та не друкує токен.

## Google Sheets

1. Увімкніть Google Sheets API в Google Cloud.
2. Створіть service account і завантажте JSON-ключ.
3. Створіть таблицю й надайте service account права редактора.
4. Вкажіть ID таблиці та назву аркуша в `.env`.

Аркуш можна не створювати. Бот створить його та точні заголовки:

```text
timestamp, meal_name, total_weight_g, meal_kcal, kcal_per_100g,
telegram_message_id, normalized_request, request, photo_path, items_json,
estimated, model, effort, input_tokens, output_tokens, llm_cost_usd
```

Непорожній аркуш з іншими заголовками вважається несумісним; автоматичної
міграції немає. `timestamp` записується як числовий date-time Google Sheets у
часовому поясі `APP_TIMEZONE`, а не як текст.

Для повідомлень із фото `photo_path` містить абсолютний шлях до JPEG на сервері.
Для текстових повідомлень колонка порожня. Це локальний шлях, а не публічне
посилання.

## Конфігурація і локальний запуск

Потрібен Python 3.11 або новіший.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Обов’язкові змінні:

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID`, `TELEGRAM_CHAT_ID`;
- `OPENAI_API_KEY`;
- `GOOGLE_SERVICE_ACCOUNT_FILE`, `GOOGLE_SPREADSHEET_ID`.

Додаткові параметри:

- `OPENAI_MODEL` — типово `gpt-5.6-luna`;
- `OPENAI_REASONING_EFFORT` — типово `none`;
- три `OPENAI_*_COST_PER_1M` — тарифи в USD за мільйон токенів;
- `GOOGLE_SHEET_NAME` — типово `food_log`;
- `PHOTO_STORAGE_DIR` — каталог фотографій, типово `./data/photos`;
- `APP_TIMEZONE` — типово `Europe/Kyiv`;
- `DAY_START_TIME` — типово `01:00`.

Якщо тарифи порожні, бот працює та записує token usage, але залишає
`llm_cost_usd` порожньою. Некоректне непорожнє значення є помилкою конфігурації.

Запуск:

```bash
python -m calories_bot.main
```

Для live-конфігурації поза репозиторієм можна явно задати файл:

```powershell
$env:CALORIES_BOT_ENV_FILE='C:\tmp\calories-bot-live-test\.env.live'
.\.venv\Scripts\python.exe -m calories_bot.main
```

## systemd на Ubuntu

Приклад використовує `/opt/calories-bot` і системного користувача `calories-bot`:

```bash
sudo useradd --system --home /opt/calories-bot --shell /usr/sbin/nologin calories-bot
sudo mkdir -p /opt/calories-bot
# Скопіюйте код, .env і service-account.json у /opt/calories-bot
sudo mkdir -p /opt/calories-bot/data/photos
sudo chown -R calories-bot:calories-bot /opt/calories-bot
sudo chmod 600 /opt/calories-bot/.env /opt/calories-bot/service-account.json
sudo cp deploy/calories-bot.service /etc/systemd/system/calories-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now calories-bot
sudo systemctl status calories-bot
```

Логи: `journalctl -u calories-bot -f`.

## Автоматизація Git і оновлення VPS

PowerShell-скрипти для першої ініціалізації Git/GitHub, збереження версій і
деплою на `hetzner` знаходяться в каталозі `automation`.

```powershell
.\automation\init-git.ps1 -RemoteUrl "git@github.com:YOUR_USER/calories-bot.git"
.\automation\save-version.ps1 -Message "Describe the change"
.\automation\deploy-vps.ps1
```

Перший крок виконується один раз. Деталі та короткий перелік ручних дій наведені
в `automation/README.md`.

## Перевірки

```bash
pip install -r requirements-dev.txt
python -m compileall -q calories_bot
ruff format --check .
ruff check .
mypy calories_bot
pytest --cov=calories_bot --cov-branch --cov-fail-under=90
pip check
pip-audit
```

Live credentials зберігайте поза репозиторієм, наприклад у
`C:\tmp\calories-bot-live-test`. Не надсилайте секрети в чат.
