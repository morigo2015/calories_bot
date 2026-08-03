# Live acceptance checklist

## Безпечна підготовка

Не надсилайте credentials у чат і не копіюйте їх у репозиторій.

1. Створіть `C:\tmp\calories-bot-live-test`.
2. Скопіюйте `live-test.env.example` у цей каталог під назвою `.env.live` і
   заповніть значення.
3. Покладіть service-account JSON як `google-service-account.json`.
4. Покладіть тимчасовий SSH private key як `vps_ssh_key`.
5. Надайте service account права редактора Google Spreadsheet.
6. Додайте Telegram-бота адміністратором групи та вимкніть privacy mode.
7. Залиште `GOOGLE_SHEET_NAME=food_log_test`; бот створить тестовий аркуш.

Якщо Telegram ID невідомі, зупиніть бот, надішліть повідомлення в групу та
виконайте:

```powershell
.\.venv\Scripts\python.exe scripts\discover_telegram_ids.py `
  --env-file C:\tmp\calories-bot-live-test\.env.live
```

## Повідомлення дозволеного користувача в дозволеній групі

Запустіть бот із зовнішнім env-файлом:

```powershell
$env:CALORIES_BOT_ENV_FILE='C:\tmp\calories-bot-live-test\.env.live'
.\.venv\Scripts\python.exe -m calories_bot.main
```

Надішліть по одному повідомленню й дочекайтеся відповіді перед наступним:

```text
/start
/help
сир 50
сир 50г 120#
#120 сир 50 г
сир 50; яблуко 120
2 яйця, хліб 50
йогурт 200г 65 kk/100гр
батончик 45g 380kcal/100g
кава без цукру
піца 30 см
піца 30
сир # 120
сир 50.5
зустріч завтра
```

Очікування:

- `/start` і `/help` повертають однакову довідку;
- валідна їжа отримує нормалізований заголовок і створює один рядок;
- точні калорії для сиру — 60, йогурту — 130, батончика — 171;
- оцінені значення мають `≈`;
- помилки формату й нехарчовий текст не створюють рядків.

## Перевірка обмежень

- Надішліть `сир 50` цьому боту напряму — відповіді й нового рядка бути не має.
- Інший учасник надсилає `сир 50` у дозволеній групі — відповіді й рядка немає.

## VPS/systemd

Після локального E2E:

1. Розгорнути застосунок у `/opt/calories-bot` і залишити `food_log_test`.
2. Перевірити start/status/journal та автоматичний restart.
3. Зупинити сервіс, надіслати одне валідне повідомлення і запустити сервіс.
4. Переконатися, що pending update оброблено за початковим Telegram timestamp.
5. Після 10 хв стабільної роботи змінити аркуш на чистий `food_log`.

Результати записуються в
`C:\tmp\calories-bot-live-test\results\<timestamp>`, а санітизований підсумок —
у `TEST_REPORT.md`.
