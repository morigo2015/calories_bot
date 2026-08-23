from __future__ import annotations

import json
import logging
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger(__name__)
OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"


class AnalyticsError(RuntimeError):
    """Raised when local bot statistics cannot be read or written."""


class OpenAICostError(RuntimeError):
    """Raised when the OpenAI Costs API cannot provide a usable total."""


@dataclass(frozen=True)
class UserMessageCount:
    telegram_user_id: int
    display_name: str
    username: str
    count: int


@dataclass(frozen=True)
class MessageSummary:
    total: int
    users: tuple[UserMessageCount, ...]


@dataclass(frozen=True)
class LLMSummary:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal | None


class AnalyticsStore:
    """Small durable SQLite event store for operational bot statistics."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS message_events (
                        update_id INTEGER PRIMARY KEY,
                        received_at TEXT NOT NULL,
                        telegram_user_id INTEGER NOT NULL,
                        display_name TEXT NOT NULL,
                        username TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS message_events_received_at
                        ON message_events(received_at);

                    CREATE TABLE IF NOT EXISTS llm_usage_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recorded_at TEXT NOT NULL,
                        model TEXT NOT NULL,
                        input_tokens INTEGER NOT NULL,
                        cached_input_tokens INTEGER NOT NULL,
                        output_tokens INTEGER NOT NULL,
                        estimated_cost_usd TEXT
                    );
                    CREATE INDEX IF NOT EXISTS llm_usage_events_recorded_at
                        ON llm_usage_events(recorded_at);

                    CREATE TABLE IF NOT EXISTS daily_total_messages (
                        chat_id INTEGER NOT NULL,
                        telegram_message_id INTEGER NOT NULL,
                        sent_at TEXT NOT NULL,
                        PRIMARY KEY (chat_id, telegram_message_id)
                    );
                    CREATE INDEX IF NOT EXISTS daily_total_messages_chat_message
                        ON daily_total_messages(chat_id, telegram_message_id);
                    """
                )
        except sqlite3.Error as exc:
            raise AnalyticsError("Could not initialize analytics storage") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def record_message(
        self,
        update_id: int,
        received_at: datetime,
        telegram_user_id: int,
        display_name: str,
        username: str,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO message_events (
                        update_id, received_at, telegram_user_id,
                        display_name, username
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        update_id,
                        _utc_iso(received_at),
                        telegram_user_id,
                        display_name.strip(),
                        username.strip(),
                    ),
                )
        except sqlite3.Error as exc:
            raise AnalyticsError("Could not record a Telegram message") from exc

    def record_llm_usage(
        self,
        recorded_at: datetime,
        model: str,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: Decimal | None,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO llm_usage_events (
                        recorded_at, model, input_tokens, cached_input_tokens,
                        output_tokens, estimated_cost_usd
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _utc_iso(recorded_at),
                        model,
                        input_tokens,
                        cached_input_tokens,
                        output_tokens,
                        str(estimated_cost_usd)
                        if estimated_cost_usd is not None
                        else None,
                    ),
                )
        except sqlite3.Error as exc:
            raise AnalyticsError("Could not record OpenAI token usage") from exc

    def record_daily_total_message(
        self,
        chat_id: int,
        telegram_message_id: int,
        sent_at: datetime,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO daily_total_messages (
                        chat_id, telegram_message_id, sent_at
                    ) VALUES (?, ?, ?)
                    """,
                    (chat_id, telegram_message_id, _utc_iso(sent_at)),
                )
        except sqlite3.Error as exc:
            raise AnalyticsError(
                "Could not record a Telegram daily-total message"
            ) from exc

    def daily_total_message_ids_between(
        self,
        chat_id: int,
        period_start: datetime,
        period_end: datetime,
    ) -> tuple[int, ...]:
        if _as_utc(period_end) <= _as_utc(period_start):
            raise ValueError("Daily-total period end must be after its start")
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT telegram_message_id
                    FROM daily_total_messages
                    WHERE chat_id = ? AND sent_at >= ? AND sent_at < ?
                    ORDER BY telegram_message_id
                    """,
                    (chat_id, _utc_iso(period_start), _utc_iso(period_end)),
                ).fetchall()
        except sqlite3.Error as exc:
            raise AnalyticsError(
                "Could not read Telegram daily-total messages"
            ) from exc
        return tuple(int(row[0]) for row in rows)

    def forget_daily_total_messages(
        self, chat_id: int, telegram_message_ids: Sequence[int]
    ) -> None:
        if not telegram_message_ids:
            return
        try:
            with self._connect() as connection:
                connection.executemany(
                    """
                    DELETE FROM daily_total_messages
                    WHERE chat_id = ? AND telegram_message_id = ?
                    """,
                    ((chat_id, message_id) for message_id in telegram_message_ids),
                )
        except sqlite3.Error as exc:
            raise AnalyticsError(
                "Could not forget Telegram daily-total messages"
            ) from exc

    def message_summary(self, since: datetime) -> MessageSummary:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT telegram_user_id, display_name, username
                    FROM message_events
                    WHERE received_at >= ?
                    ORDER BY received_at DESC, update_id DESC
                    """,
                    (_utc_iso(since),),
                ).fetchall()
        except sqlite3.Error as exc:
            raise AnalyticsError("Could not read Telegram message statistics") from exc

        counts = Counter(int(row[0]) for row in rows)
        latest: dict[int, tuple[str, str]] = {}
        for telegram_user_id, display_name, username in rows:
            user_id = int(telegram_user_id)
            latest.setdefault(user_id, (str(display_name), str(username)))
        users = tuple(
            UserMessageCount(user_id, *latest[user_id], count)
            for user_id, count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0])
            )
        )
        return MessageSummary(total=len(rows), users=users)

    def llm_summary(self, since: datetime) -> LLMSummary:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT input_tokens, cached_input_tokens, output_tokens,
                           estimated_cost_usd
                    FROM llm_usage_events
                    WHERE recorded_at >= ?
                    """,
                    (_utc_iso(since),),
                ).fetchall()
        except sqlite3.Error as exc:
            raise AnalyticsError("Could not read OpenAI token statistics") from exc
        input_tokens = sum(int(row[0]) for row in rows)
        cached_input_tokens = sum(int(row[1]) for row in rows)
        output_tokens = sum(int(row[2]) for row in rows)
        raw_costs = [row[3] for row in rows]
        estimated_cost: Decimal | None
        if any(value is None for value in raw_costs):
            estimated_cost = None
        else:
            estimated_cost = sum(
                (Decimal(str(value)) for value in raw_costs), Decimal(0)
            )
        return LLMSummary(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost,
        )


class OpenAICostClient:
    def __init__(
        self,
        admin_api_key: str,
        project_id: str = "",
        timeout_seconds: float = 15,
    ) -> None:
        self._admin_api_key = admin_api_key.strip()
        self._project_id = project_id.strip()
        self._timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self._admin_api_key)

    @property
    def project_scoped(self) -> bool:
        return bool(self._project_id)

    def total_cost_usd(self, start: datetime, end: datetime) -> Decimal:
        if not self.configured:
            raise OpenAICostError("OPENAI_ADMIN_API_KEY is not configured")
        params: dict[str, Any] = {
            "start_time": int(_as_utc(start).timestamp()),
            "end_time": int(_as_utc(end).timestamp()),
            "bucket_width": "1d",
            "limit": 31,
        }
        if self._project_id:
            params["project_ids"] = [self._project_id]
        total = Decimal(0)
        page: str | None = None
        while True:
            current = dict(params)
            if page:
                current["page"] = page
            url = f"{OPENAI_COSTS_URL}?{urllib.parse.urlencode(current, doseq=True)}"
            request = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self._admin_api_key}",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self._timeout_seconds
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
                raise OpenAICostError("OpenAI Costs API request failed") from exc
            try:
                for bucket in payload.get("data", []):
                    for result in bucket.get("results", []):
                        amount = result.get("amount", {})
                        if amount.get("currency", "usd").lower() != "usd":
                            raise OpenAICostError(
                                "OpenAI Costs API returned a non-USD amount"
                            )
                        total += Decimal(str(amount.get("value", 0)))
                next_page = payload.get("next_page")
                page = str(next_page) if next_page else None
            except (AttributeError, InvalidOperation, TypeError) as exc:
                raise OpenAICostError("OpenAI Costs API response is invalid") from exc
            if page is None:
                return total


class BotStatistics:
    def __init__(
        self,
        store: AnalyticsStore,
        timezone: ZoneInfo,
        cost_client: OpenAICostClient,
        started_at: datetime | None = None,
    ) -> None:
        self._store = store
        self._timezone = timezone
        self._cost_client = cost_client
        self._started_at = _as_utc(started_at or datetime.now(UTC))

    def record_message(
        self,
        update_id: int,
        received_at: datetime,
        telegram_user_id: int,
        display_name: str,
        username: str,
    ) -> None:
        self._store.record_message(
            update_id,
            received_at,
            telegram_user_id,
            display_name,
            username,
        )

    def record_llm_usage(
        self,
        recorded_at: datetime,
        model: str,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: Decimal | None,
    ) -> None:
        self._store.record_llm_usage(
            recorded_at,
            model,
            input_tokens,
            cached_input_tokens,
            output_tokens,
            estimated_cost_usd,
        )

    def record_daily_total_message(
        self,
        chat_id: int,
        telegram_message_id: int,
        sent_at: datetime,
    ) -> None:
        self._store.record_daily_total_message(chat_id, telegram_message_id, sent_at)

    def daily_total_message_ids_between(
        self,
        chat_id: int,
        period_start: datetime,
        period_end: datetime,
    ) -> tuple[int, ...]:
        return self._store.daily_total_message_ids_between(
            chat_id, period_start, period_end
        )

    def forget_daily_total_messages(
        self, chat_id: int, telegram_message_ids: Sequence[int]
    ) -> None:
        self._store.forget_daily_total_messages(chat_id, telegram_message_ids)

    def format_info(self, version: str, now: datetime | None = None) -> str:
        current = _as_utc(now or datetime.now(UTC))
        day = self._store.message_summary(current - timedelta(hours=24))
        month = self._store.message_summary(current - timedelta(days=30))
        llm = self._store.llm_summary(current - timedelta(days=30))

        lines = [
            f"Версія: {version}",
            "Останній перезапуск: "
            + self._started_at.astimezone(self._timezone).strftime(
                "%d.%m.%Y %H:%M:%S %Z"
            ),
            "",
            *_format_message_period("Запити за 24 години", day),
            "",
            *_format_message_period("Запити за 30 днів", month),
            "",
            "OpenAI за 30 днів:",
            f"• вхідні токени: {_format_integer(llm.input_tokens)}",
            f"• вихідні токени: {_format_integer(llm.output_tokens)}",
            f"• кешовані токени: {_format_integer(llm.cached_input_tokens)}",
            "• розрахункова вартість: "
            + _format_cost(llm.estimated_cost_usd, missing="не розраховано"),
        ]
        if not self._cost_client.configured:
            lines.append("• OpenAI API: недоступно — не задано Admin API key")
        else:
            scope = "проєкт" if self._cost_client.project_scoped else "організація"
            try:
                actual_cost = self._cost_client.total_cost_usd(
                    current - timedelta(days=30), current
                )
            except OpenAICostError:
                LOGGER.warning(
                    "Could not retrieve OpenAI organization costs", exc_info=True
                )
                lines.append("• OpenAI API: тимчасово недоступно")
            else:
                lines.append(f"• OpenAI API ({scope}): {_format_cost(actual_cost)}")
        return "\n".join(lines)


def _format_message_period(title: str, summary: MessageSummary) -> list[str]:
    lines = [f"{title}:", f"• разом: {_format_integer(summary.total)}"]
    if not summary.users:
        lines.append("• користувачі: немає запитів")
        return lines
    for user in summary.users:
        label = user.display_name or "Без імені"
        lines.append(f"• {label}: {_format_integer(user.count)}")
    return lines


def _format_integer(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _format_cost(value: Decimal | None, *, missing: str = "—") -> str:
    if value is None:
        return missing
    return f"${value.quantize(Decimal('0.000001'))}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_iso(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds")
