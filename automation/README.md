# Оновлення проєкту

## Зберегти версію і оновити VPS

Основна команда:

```powershell
.\automation\publish.ps1 -Message "Опис змін"
```

Вона послідовно:

1. Виконує `git add`, `git commit` і `git push` у
   `github.com/morigo2015/calories_bot`.
2. Копіює `calories_bot/` і `requirements.txt` через SSH на
   `/home/igor/calories-bot`.
3. Встановлює production-залежності на VPS.
4. Перезапускає systemd-сервіс `calories-bot`.
5. Показує статус і останні 20 рядків журналу.

Локальні тести, Ruff і mypy ця автоматизація не запускає.

## Окремі команди

Тільки commit і push:

```powershell
.\automation\save-version.ps1 -Message "Опис змін"
```

Тільки копіювання та перезапуск VPS:

```powershell
.\automation\deploy-vps.ps1
```

Скрипт деплою не копіює і не змінює серверні `.env`, `.venv`,
`service-account.json` та `data/photos`.

## Одноразові ручні дії

GitHub-репозиторій, SSH-доступ і systemd-сервіс потрібно створити один раз під
вашим акаунтом. Вони вже налаштовані; надалі достатньо `publish.ps1`.
