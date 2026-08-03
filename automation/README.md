# Автоматизація Git і VPS

Скрипти запускаються у PowerShell з кореня проєкту.

## Один раз: Git і зовнішній репозиторій

1. Відкрийте `https://github.com/new` і створіть порожній private repository без
   README, `.gitignore` та license. Це потрібно зробити у своєму GitHub-акаунті,
   бо GitHub CLI на цьому комп’ютері не встановлений і не авторизований.
2. Якщо Git ще не знає ваше ім’я та email, виконайте:

   ```powershell
   git config --global user.name "Your Name"
   git config --global user.email "you@example.com"
   ```

3. Ініціалізуйте локальний Git, зробіть перший commit і push:

   ```powershell
   .\automation\init-git.ps1 -RemoteUrl "git@github.com:YOUR_USER/calories-bot.git"
   ```

Під час першого push Git може попросити авторизацію. Для SSH URL додайте свій
публічний SSH key у GitHub; для HTTPS можна передати URL виду
`https://github.com/YOUR_USER/calories-bot.git` і скористатися Git Credential
Manager.

## Зберегти нову версію

Скрипт запускає format check, lint, mypy і тести, потім створює commit і push:

```powershell
.\automation\save-version.ps1 -Message "Add meal photo support"
```

Тільки локальний commit:

```powershell
.\automation\save-version.ps1 -Message "Work in progress" -LocalOnly
```

## Оновити VPS

Перед деплоєм робоча папка має бути чистою, а commit — вже завантаженим в
`origin`. Скрипт не копіює `.env`, service-account JSON, `.venv` або фотографії.

```powershell
.\automation\deploy-vps.ps1
```

Він копіює код на SSH-аліас `hetzner`, встановлює production-залежності,
компілює Python-модулі, перезапускає `calories-bot` і показує статус та останні
30 рядків журналу. `sudo` може один раз попросити ваш пароль — це нормальна
системна дія, яку скрипт не може виконати без ваших прав.

Одноразово на VPS мають уже існувати:

```text
/home/igor/calories-bot/.env
/home/igor/calories-bot/service-account.json
/home/igor/calories-bot/.venv/
/etc/systemd/system/calories-bot.service
```
