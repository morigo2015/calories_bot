import base64
from datetime import UTC, date, datetime
from types import SimpleNamespace

from calories_bot.analyzer import ModelPricing
from calories_bot.burn_screenshots import (
    BURN_SCREENSHOT_PROMPT,
    BurnScreenshotAnalysis,
    BurnScreenshotDay,
    OpenAIBurnScreenshotAnalyzer,
    prepare_burn_screenshot_entries,
)
from calories_bot.burned import BodyProfile


def profile() -> BodyProfile:
    return BodyProfile(
        sex="male",
        birth_date=date(1990, 5, 15),
        height_cm=180,
        weight_kg=80,
    )


def test_openai_screenshot_analyzer_sends_image_and_reference_date() -> None:
    parsed = BurnScreenshotAnalysis(
        app="garmin_connect",
        detected_app_name="Garmin Connect",
        days=[BurnScreenshotDay(day=date(2026, 9, 1), total_kcal=3182)],
    )
    response = SimpleNamespace(output_parsed=parsed, usage=None)
    fake_responses = SimpleNamespace(kwargs=None)

    def parse(**kwargs):
        fake_responses.kwargs = kwargs
        return response

    fake_responses.parse = parse
    analyzer = OpenAIBurnScreenshotAnalyzer.__new__(OpenAIBurnScreenshotAnalyzer)
    analyzer._client = SimpleNamespace(responses=fake_responses)
    analyzer._model = "test-model"
    analyzer._effort = "none"
    analyzer._pricing = ModelPricing(None, None, None)
    analyzer._usage_recorder = None

    result = analyzer.analyze(b"jpeg-data", date(2026, 9, 2))

    assert result == parsed
    assert fake_responses.kwargs["input"][0]["content"] == BURN_SCREENSHOT_PROMPT
    content = fake_responses.kwargs["input"][1]["content"]
    assert "2026-09-02" in content[0]["text"]
    assert content[1] == {
        "type": "input_image",
        "image_url": (
            "data:image/jpeg;base64," + base64.b64encode(b"jpeg-data").decode()
        ),
        "detail": "auto",
    }


def test_prepare_entries_prefers_total_and_ignores_unfinished_day() -> None:
    analysis = BurnScreenshotAnalysis(
        app="garmin_connect",
        detected_app_name="Garmin Connect",
        days=[
            BurnScreenshotDay(
                day=date(2026, 9, 2),
                total_kcal=2049,
                active_kcal=500,
                resting_kcal=1549,
            ),
            BurnScreenshotDay(day=date(2026, 9, 1), total_kcal=3182),
            BurnScreenshotDay(day=date(2026, 8, 31), total_kcal=2933),
        ],
    )

    prepared = prepare_burn_screenshot_entries(
        [analysis],
        last_completed_day=date(2026, 9, 1),
        updated_at=datetime(2026, 9, 2, 12, tzinfo=UTC),
        profile=None,
    )

    assert prepared.ignored_days == (date(2026, 9, 2),)
    assert list(prepared.entries) == [date(2026, 8, 31), date(2026, 9, 1)]
    assert prepared.entries[date(2026, 9, 1)][0].effective_total_kcal == 3182
    assert prepared.entries[date(2026, 9, 1)][0].input_type == "total"


def test_prepare_entries_combines_visible_components_and_uses_bmr_for_active() -> None:
    analysis = BurnScreenshotAnalysis(
        app="zepp_life",
        detected_app_name="Zepp Life",
        days=[
            BurnScreenshotDay(
                day=date(2026, 8, 30), active_kcal=600, resting_kcal=1700
            ),
            BurnScreenshotDay(day=date(2026, 8, 31), active_kcal=700),
        ],
    )

    prepared = prepare_burn_screenshot_entries(
        [analysis],
        last_completed_day=date(2026, 9, 1),
        updated_at=datetime(2026, 9, 2, 12, tzinfo=UTC),
        profile=profile(),
    )

    visible_total = prepared.entries[date(2026, 8, 30)][0]
    calculated_total = prepared.entries[date(2026, 8, 31)][0]
    assert (visible_total.input_type, visible_total.effective_total_kcal) == (
        "total",
        2300,
    )
    assert (calculated_total.input_type, calculated_total.resting_kcal) == (
        "active",
        1750,
    )
    assert calculated_total.effective_total_kcal == 2450


def test_prepare_entries_keeps_only_distinct_effective_totals() -> None:
    day = date(2026, 8, 31)
    analyses = [
        BurnScreenshotAnalysis(
            app="garmin_connect",
            days=[BurnScreenshotDay(day=day, total_kcal=2300)],
        ),
        BurnScreenshotAnalysis(
            app="zepp_life",
            days=[
                BurnScreenshotDay(day=day, active_kcal=550),
                BurnScreenshotDay(day=day, total_kcal=2400),
            ],
        ),
    ]

    prepared = prepare_burn_screenshot_entries(
        analyses,
        last_completed_day=date(2026, 9, 1),
        updated_at=datetime(2026, 9, 2, 12, tzinfo=UTC),
        profile=profile(),
    )

    # 550 active + 1750 resting equals the explicit 2300 and is deduplicated.
    assert [item.effective_total_kcal for item in prepared.entries[day]] == [2300, 2400]
    assert prepared.entries[day][0].input_type == "total"


def test_prepare_entries_reports_active_days_that_need_profile() -> None:
    day = date(2026, 8, 31)
    prepared = prepare_burn_screenshot_entries(
        [
            BurnScreenshotAnalysis(
                app="zepp_life",
                days=[BurnScreenshotDay(day=day, active_kcal=500)],
            )
        ],
        last_completed_day=date(2026, 9, 1),
        updated_at=datetime(2026, 9, 2, 12, tzinfo=UTC),
        profile=None,
    )

    assert prepared.entries == {}
    assert prepared.profile_required_days == (day,)
