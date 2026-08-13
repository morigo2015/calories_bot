import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest

from calories_bot.analytics import (
    AnalyticsStore,
    BotStatistics,
    OpenAICostClient,
    OpenAICostError,
)

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


class FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def test_store_counts_messages_by_period_and_ignores_duplicate_updates(
    tmp_path,
) -> None:
    path = tmp_path / "statistics.sqlite3"
    store = AnalyticsStore(path)
    store.record_message(1, NOW - timedelta(hours=1), 10, "Анна", "anna")
    store.record_message(1, NOW - timedelta(hours=1), 10, "Анна", "anna")
    store.record_message(2, NOW - timedelta(hours=2), 10, "Анна", "anna")
    store.record_message(3, NOW - timedelta(days=2), 20, "Богдан", "")
    store.record_message(4, NOW - timedelta(days=31), 30, "Старий", "old")

    day = store.message_summary(NOW - timedelta(hours=24))
    month = AnalyticsStore(path).message_summary(NOW - timedelta(days=30))

    assert day.total == 2
    assert [(user.telegram_user_id, user.count) for user in day.users] == [(10, 2)]
    assert month.total == 3
    assert [(user.telegram_user_id, user.count) for user in month.users] == [
        (10, 2),
        (20, 1),
    ]


def test_store_sums_llm_usage_and_requires_pricing_for_every_event(tmp_path) -> None:
    store = AnalyticsStore(tmp_path / "statistics.sqlite3")
    store.record_llm_usage(
        NOW - timedelta(days=1), "model", 100, 40, 20, Decimal("0.001")
    )
    store.record_llm_usage(
        NOW - timedelta(days=2), "model", 200, 50, 30, Decimal("0.002")
    )
    store.record_llm_usage(
        NOW - timedelta(days=31), "model", 999, 999, 999, Decimal("9")
    )

    summary = store.llm_summary(NOW - timedelta(days=30))

    assert summary.input_tokens == 300
    assert summary.cached_input_tokens == 90
    assert summary.output_tokens == 50
    assert summary.estimated_cost_usd == Decimal("0.003")

    store.record_llm_usage(NOW, "model", 1, 0, 1, None)
    assert store.llm_summary(NOW - timedelta(days=30)).estimated_cost_usd is None


def test_store_tracks_daily_total_message_ids_durably_by_chat(tmp_path) -> None:
    path = tmp_path / "statistics.sqlite3"
    store = AnalyticsStore(path)
    store.record_daily_total_message(10, 100, NOW - timedelta(minutes=3))
    store.record_daily_total_message(10, 105, NOW - timedelta(minutes=2))
    store.record_daily_total_message(10, 105, NOW - timedelta(minutes=1))
    store.record_daily_total_message(20, 110, NOW)

    reopened = AnalyticsStore(path)

    assert reopened.daily_total_message_ids_after(10, 100) == (105,)
    assert reopened.daily_total_message_ids_after(10, 0) == (100, 105)
    assert reopened.daily_total_message_ids_after(20, 0) == (110,)

    reopened.forget_daily_total_messages(10, (105,))

    assert store.daily_total_message_ids_after(10, 0) == (100,)
    assert store.daily_total_message_ids_after(20, 0) == (110,)


def test_cost_client_paginates_and_filters_by_project(monkeypatch) -> None:
    calls = []
    payloads = iter(
        [
            {
                "data": [{"results": [{"amount": {"value": 0.12, "currency": "usd"}}]}],
                "next_page": "next",
            },
            {
                "data": [
                    {"results": [{"amount": {"value": "0.03", "currency": "usd"}}]}
                ],
                "next_page": None,
            },
        ]
    )

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, request.headers, timeout))
        return FakeResponse(next(payloads))

    monkeypatch.setattr("calories_bot.analytics.urllib.request.urlopen", fake_urlopen)
    client = OpenAICostClient("admin-key", "project-123", 7)

    total = client.total_cost_usd(NOW - timedelta(days=30), NOW)

    assert total == Decimal("0.15")
    assert len(calls) == 2
    first_query = parse_qs(urlparse(calls[0][0]).query)
    second_query = parse_qs(urlparse(calls[1][0]).query)
    assert first_query["project_ids"] == ["project-123"]
    assert second_query["page"] == ["next"]
    assert calls[0][1]["Authorization"] == "Bearer admin-key"
    assert calls[0][2] == 7


def test_cost_client_requires_admin_key(tmp_path) -> None:
    client = OpenAICostClient("")
    with pytest.raises(OpenAICostError):
        client.total_cost_usd(NOW - timedelta(days=30), NOW)


def test_info_formats_restart_messages_tokens_and_missing_admin_key(tmp_path) -> None:
    store = AnalyticsStore(tmp_path / "statistics.sqlite3")
    store.record_message(1, NOW - timedelta(hours=1), 10, "Анна", "anna")
    store.record_message(2, NOW - timedelta(days=2), 20, "Богдан", "")
    store.record_llm_usage(
        NOW - timedelta(days=1), "model", 1200, 300, 45, Decimal("0.012345")
    )
    stats = BotStatistics(
        store,
        ZoneInfo("Europe/Kyiv"),
        OpenAICostClient(""),
        started_at=datetime(2026, 8, 12, 9, tzinfo=UTC),
    )

    text = stats.format_info("1.0.1", NOW)

    assert "Версія: 1.0.1" in text
    assert "Останній перезапуск: 12.08.2026 12:00:00 EEST" in text
    assert "Запити за 24 години:\n• разом: 1" in text
    assert "Запити за 30 днів:\n• разом: 2" in text
    assert "Анна (@anna) · ID 10: 1" in text
    assert "Богдан · ID 20: 1" in text
    assert "вхідні токени: 1 200" in text
    assert "вихідні токени: 45" in text
    assert "кешовані токени: 300" in text
    assert "розрахункова вартість: $0.012345" in text
    assert "не задано Admin API key" in text
