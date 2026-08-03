# Calories Bot — коротка довідка для VPS (systemd)

```bash
cd ~/calories-bot
```

Сервіс називається `calories-bot`.

## Підключення до Hetzner

Усі команди нижче використовують SSH-аліас `hetzner`. Налаштуйте його один
раз у локальному файлі `~/.ssh/config` (Windows: `$HOME\.ssh\config`):

```sshconfig
Host hetzner
    HostName <IP-АДРЕСА-ВАШОГО-HETZNER>
    User igor
```

Після цього підключення виконується просто:

```bash
ssh hetzner
```

## Статус

```bash
sudo systemctl status --no-pager calories-bot
```

Короткий статус:

```bash
sudo systemctl is-active calories-bot
```

## Логи

```bash
sudo journalctl -u calories-bot -n 100 --no-pager
sudo journalctl -u calories-bot -f
```

Вийти з перегляду наживо: `Ctrl+C`.

## Запустити

```bash
sudo systemctl start calories-bot
```

## Запустити автоматично після перезавантаження

```bash
sudo systemctl enable calories-bot
```

## Перезапустити

```bash
sudo systemctl restart calories-bot
```

## Зупинити для оновлення або обслуговування

```bash
sudo systemctl stop calories-bot
```

Щоб сервіс не запускався після перезавантаження:

```bash
sudo systemctl disable calories-bot
```

## Увімкнути назад

```bash
sudo systemctl enable --now calories-bot
```

## Оновити код

Звичайне оновлення виконується з локального кореня проєкту однією командою:

```powershell
.\automation\deploy-vps.ps1
```

Скрипт перевіряє, що версію вже збережено й завантажено в GitHub, копіює код,
оновлює залежності, перевіряє компіляцію, перезапускає сервіс і показує журнал.

Нижче лишається ручний варіант для аварійного оновлення одного файла.

Команди виконуються на локальному комп’ютері з кореня проєкту.

1. Завантажте змінені файли у тимчасовий каталог на Hetzner:

   ```powershell
   scp .\calories_bot\sheets.py hetzner:/tmp/sheets.py
   ```

2. Підключіться до VPS:

   ```bash
   ssh hetzner
   ```

3. Зробіть резервну копію та встановіть файл із правильним власником:

   ```bash
   sudo cp /home/igor/calories-bot/calories_bot/sheets.py /home/igor/calories-bot/calories_bot/sheets.py.backup
   sudo install -o igor -g igor -m 0644 /tmp/sheets.py /home/igor/calories-bot/calories_bot/sheets.py
   ```

4. Перевірте імпорт без запису в захищений `__pycache__`, потім перезапустіть
   сервіс:

   ```bash
   cd /home/igor/calories-bot
   PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c "import calories_bot.sheets; print('Import OK')"
   sudo systemctl restart calories-bot
   sudo systemctl status --no-pager calories-bot
   sudo journalctl -u calories-bot --since "5 minutes ago" --no-pager
   ```

`pip install` потрібен лише тоді, коли змінювався `requirements.txt`.

## Змінити налаштування

```bash
nano ~/calories-bot/.env
sudo systemctl restart calories-bot
```

## Змінили systemd unit

Після зміни `/etc/systemd/system/calories-bot.service`:

```bash
sudo systemctl daemon-reload
sudo systemctl restart calories-bot
```
