#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import shutil
import sys
from pathlib import Path

import gspread
from garminconnect import Garmin

from calories_bot.config import Settings
from calories_bot.garmin import GarminStoreProvider
from calories_bot.users import GoogleUserRegistry, UserRecord


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Керування персональними інтеграціями Garmin користувачів бота."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("connect", "status", "refresh"):
        command = subparsers.add_parser(name)
        command.add_argument("telegram_user_id", type=int)
    disconnect = subparsers.add_parser("disconnect")
    disconnect.add_argument("telegram_user_id", type=int)
    disconnect.add_argument(
        "--confirm",
        action="store_true",
        help="підтвердити видалення локальних Garmin-токенів і кешу",
    )
    return parser


def _registry(settings: Settings) -> GoogleUserRegistry:
    client = gspread.service_account(filename=str(settings.google_service_account_file))
    return GoogleUserRegistry(
        client,
        settings.users_spreadsheet_id,
        settings.users_sheet_name,
    )


def _active_user(settings: Settings, telegram_user_id: int) -> UserRecord:
    user = _registry(settings).get_user(telegram_user_id)
    if user is None:
        raise RuntimeError("Користувача з таким Telegram ID немає в реєстрі.")
    if user.status != "active":
        raise RuntimeError(
            f"Інтеграцію можна підключити лише активному користувачу "
            f"(поточний статус: {user.status})."
        )
    return user


def _provider(settings: Settings) -> GarminStoreProvider:
    return GarminStoreProvider(
        settings.garmin_user_data_dir,
        settings.timezone,
        fallback_user_id=settings.admin_telegram_user_id,
        fallback_tokenstore=settings.garmin_tokenstore,
        fallback_cache_path=settings.garmin_calorie_cache_path,
    )


def _secure_tree(path: Path) -> None:
    path.chmod(0o700)
    for child in path.rglob("*"):
        child.chmod(0o700 if child.is_dir() else 0o600)


def _connect(settings: Settings, telegram_user_id: int) -> None:
    user = _active_user(settings, telegram_user_id)
    provider = _provider(settings)
    tokenstore = provider.personal_tokenstore(telegram_user_id)
    tokenstore.mkdir(parents=True, exist_ok=True, mode=0o700)
    _secure_tree(provider.user_dir(telegram_user_id))

    email = input("Garmin email: ").strip()
    if not email:
        raise RuntimeError("Garmin email не може бути порожнім.")
    password = getpass.getpass("Garmin password: ")
    if not password:
        raise RuntimeError("Garmin password не може бути порожнім.")

    client = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
        retry_attempts=2,
    )
    client.login(str(tokenstore))
    _secure_tree(provider.user_dir(telegram_user_id))
    print(f"Garmin підключено для {user.display_name} ({telegram_user_id}).")
    print("Запускаю початкове завантаження 84 днів; воно може тривати кілька хвилин.")
    store = provider.store_for(telegram_user_id, user.day_start)
    if store is None:
        raise RuntimeError("Garmin-токени не були збережені.")
    store.refresh_if_due()
    print("Початковий кеш готовий.")


def _status(settings: Settings, telegram_user_id: int) -> None:
    user = _active_user(settings, telegram_user_id)
    provider = _provider(settings)
    tokenstore = provider.tokenstore_for(telegram_user_id)
    if tokenstore is None:
        print(f"Garmin не підключено для {user.display_name} ({telegram_user_id}).")
        return
    cache_path = provider.personal_cache_path(telegram_user_id)
    if (
        telegram_user_id == settings.admin_telegram_user_id
        and tokenstore == settings.garmin_tokenstore
    ):
        cache_path = settings.garmin_calorie_cache_path
    cache_state = "є" if cache_path.is_file() else "ще немає"
    print(
        f"Garmin підключено для {user.display_name} ({telegram_user_id}); "
        f"кеш: {cache_state}."
    )


def _refresh(settings: Settings, telegram_user_id: int) -> None:
    user = _active_user(settings, telegram_user_id)
    store = _provider(settings).store_for(telegram_user_id, user.day_start)
    if store is None:
        raise RuntimeError("Garmin не підключено для цього користувача.")
    store.refresh_if_due(force=True)
    print("Garmin-кеш примусово оновлено.")


def _disconnect(settings: Settings, telegram_user_id: int, *, confirmed: bool) -> None:
    _active_user(settings, telegram_user_id)
    if not confirmed:
        raise RuntimeError("Додай --confirm, щоб підтвердити відключення Garmin.")
    user_dir = _provider(settings).user_dir(telegram_user_id)
    if not user_dir.exists():
        print("Персональної Garmin-інтеграції немає.")
        return
    shutil.rmtree(user_dir)
    print("Персональні Garmin-токени та кеш видалено.")


def main() -> int:
    args = _parser().parse_args()
    try:
        settings = Settings.from_env()
        if args.command == "connect":
            _connect(settings, args.telegram_user_id)
        elif args.command == "status":
            _status(settings, args.telegram_user_id)
        elif args.command == "refresh":
            _refresh(settings, args.telegram_user_id)
        else:
            _disconnect(
                settings,
                args.telegram_user_id,
                confirmed=args.confirm,
            )
    except (KeyboardInterrupt, EOFError):
        print("\nСкасовано.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Помилка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
