from datetime import date, timedelta

from calories_bot.charts import WeeklyChartPoint, render_weekly_chart


def test_render_weekly_chart_returns_png_with_deficit_surplus_and_weight() -> None:
    start = date(2026, 6, 1)
    points = [
        WeeklyChartPoint(
            start_day=start + timedelta(days=index * 7),
            end_day=start + timedelta(days=index * 7 + 6),
            average_balance_kcal=(-350 + index * 65),
            average_weight_kg=82.0 - index * 0.25,
        )
        for index in range(12)
    ]

    image = render_weekly_chart(
        points,
        reliable_start=date(2026, 6, 15),
        reliable_end=date(2026, 8, 10),
        weight_trend=(date(2026, 6, 1), 82.0, date(2026, 8, 23), 79.0),
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 50_000


def test_render_weekly_chart_handles_missing_values() -> None:
    image = render_weekly_chart(
        [WeeklyChartPoint(date(2026, 8, 1), date(2026, 8, 7), None, 80.0)],
        weight_trend=(date(2026, 8, 1), 80.0, date(2026, 8, 7), 79.8),
        show_balance=False,
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
