from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, Protocol, cast

from openai import OpenAI
from openai.types.shared import ReasoningEffort
from pydantic import BaseModel, Field, model_validator

from .analyzer import ModelPricing, UsageRecorder, metadata_from_usage
from .burned import (
    MAX_BURNED_KCAL,
    BodyProfile,
    BurnedCaloriesEntry,
    build_burned_entry,
)
from .models import LLMMetadata

BURN_SCREENSHOT_PROMPT = """You read screenshots of daily calories burned.
Return only the structured BurnScreenshotAnalysis object.

Supported applications:
- Garmin Connect (`garmin_connect`). A "Calories Burned" history screen can
  show a daily "Total" together with active and resting calories.
- Zepp Life (`zepp_life`). Treat a daily calories-burned/activity value as
  active calories unless the screenshot explicitly labels it as total or also
  shows resting calories.

Rules:
- Identify the application from visible branding and interface context. Use
  `unknown` for every other application or when the source cannot be identified.
- Extract only per-day values visibly stated in the screenshot. Never estimate
  a value from a chart, average, goal, workout plan, exercise list, or partial
  bar height.
- Do not calculate totals. Put each visible value into its matching field:
  `total_kcal`, `active_kcal`, or `resting_kcal`.
- A plain Zepp Life daily "Calories" value is active, not total.
- A Garmin daily row explicitly marked "Total" is total.
- Omit days that do not have a readable date or any readable calorie value.
- Resolve an omitted year using the supplied reference date, visible weekday,
  and surrounding date range. Do not return a future year. If the date remains
  ambiguous, omit that day.
- Values must be whole kilocalories. Thousands separators are grouping marks.
- Do not infer missing calories from the user's body or profile.
"""


class BurnScreenshotError(RuntimeError):
    """Raised when a calorie screenshot cannot be analyzed safely."""


class BurnScreenshotDay(BaseModel):
    day: date
    total_kcal: int | None = Field(default=None, ge=1, le=MAX_BURNED_KCAL)
    active_kcal: int | None = Field(default=None, ge=1, le=MAX_BURNED_KCAL)
    resting_kcal: int | None = Field(default=None, ge=1, le=MAX_BURNED_KCAL)

    @model_validator(mode="after")
    def require_calories(self) -> BurnScreenshotDay:
        if (
            self.total_kcal is None
            and self.active_kcal is None
            and self.resting_kcal is None
        ):
            raise ValueError("A screenshot day must contain calories")
        return self


class BurnScreenshotAnalysis(BaseModel):
    app: Literal["garmin_connect", "zepp_life", "unknown"]
    detected_app_name: str = Field(default="", max_length=80)
    days: list[BurnScreenshotDay] = Field(default_factory=list, max_length=31)

    @model_validator(mode="after")
    def normalize_app_name(self) -> BurnScreenshotAnalysis:
        self.detected_app_name = " ".join(self.detected_app_name.split())
        return self


class BurnScreenshotAnalyzer(Protocol):
    def analyze(
        self, image_bytes: bytes, reference_date: date
    ) -> BurnScreenshotAnalysis: ...


class OpenAIBurnScreenshotAnalyzer:
    def __init__(
        self,
        api_key: str,
        model: str,
        effort: str,
        timeout_seconds: float,
        pricing: ModelPricing,
        usage_recorder: UsageRecorder | None = None,
    ) -> None:
        self._client = OpenAI(api_key=api_key, timeout=timeout_seconds)
        self._model = model
        self._effort = effort
        self._pricing = pricing
        self._usage_recorder = usage_recorder

    def analyze(
        self, image_bytes: bytes, reference_date: date
    ) -> BurnScreenshotAnalysis:
        return self.analyze_result(image_bytes, reference_date).analysis

    def analyze_result(
        self, image_bytes: bytes, reference_date: date
    ) -> BurnScreenshotResult:
        if not image_bytes:
            raise BurnScreenshotError("Screenshot is empty")
        encoded_image = base64.b64encode(image_bytes).decode("ascii")
        image_mime_type = (
            "image/png"
            if image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
            else "image/jpeg"
        )
        try:
            response = self._client.responses.parse(
                model=self._model,
                reasoning={"effort": cast(ReasoningEffort, self._effort)},
                store=False,
                input=cast(
                    Any,
                    [
                        {"role": "system", "content": BURN_SCREENSHOT_PROMPT},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": (
                                        "Reference local date for resolving omitted "
                                        "years and relative labels: "
                                        f"{reference_date.isoformat()}"
                                    ),
                                },
                                {
                                    "type": "input_image",
                                    "image_url": (
                                        f"data:{image_mime_type};base64,{encoded_image}"
                                    ),
                                    "detail": "auto",
                                },
                            ],
                        },
                    ],
                ),
                text_format=BurnScreenshotAnalysis,
            )
        except Exception as exc:
            raise BurnScreenshotError("OpenAI screenshot request failed") from exc
        metadata = metadata_from_usage(
            getattr(response, "usage", None),
            self._model,
            self._effort,
            self._pricing,
            self._usage_recorder,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise BurnScreenshotError("OpenAI returned no screenshot analysis")
        return BurnScreenshotResult(analysis=parsed, metadata=metadata)


@dataclass(frozen=True)
class BurnScreenshotResult:
    analysis: BurnScreenshotAnalysis
    metadata: LLMMetadata


@dataclass(frozen=True)
class PreparedBurnScreenshots:
    entries: dict[date, tuple[BurnedCaloriesEntry, ...]]
    ignored_days: tuple[date, ...]
    invalid_days: tuple[date, ...]
    profile_required_days: tuple[date, ...]


def prepare_burn_screenshot_entries(
    analyses: list[BurnScreenshotAnalysis],
    *,
    last_completed_day: date,
    updated_at: datetime,
    profile: BodyProfile | None,
) -> PreparedBurnScreenshots:
    by_day: dict[date, dict[int, BurnedCaloriesEntry]] = {}
    ignored: set[date] = set()
    invalid: set[date] = set()
    profile_required: set[date] = set()

    for analysis in analyses:
        if analysis.app == "unknown":
            continue
        for item in analysis.days:
            if item.day > last_completed_day:
                ignored.add(item.day)
                continue
            try:
                if item.total_kcal is not None:
                    if (
                        item.active_kcal is not None
                        and item.total_kcal < item.active_kcal
                    ):
                        raise ValueError("Total calories are below active calories")
                    entry = build_burned_entry(
                        item.day, "total", item.total_kcal, updated_at
                    )
                elif item.active_kcal is not None and item.resting_kcal is not None:
                    entry = build_burned_entry(
                        item.day,
                        "total",
                        item.active_kcal + item.resting_kcal,
                        updated_at,
                    )
                elif item.active_kcal is not None:
                    if profile is None:
                        profile_required.add(item.day)
                        continue
                    entry = build_burned_entry(
                        item.day, "active", item.active_kcal, updated_at, profile
                    )
                else:
                    raise ValueError("Resting calories alone are not enough")
            except ValueError:
                invalid.add(item.day)
                continue

            choices = by_day.setdefault(item.day, {})
            previous = choices.get(entry.effective_total_kcal)
            if previous is None or (
                previous.input_type == "active" and entry.input_type == "total"
            ):
                choices[entry.effective_total_kcal] = entry

    return PreparedBurnScreenshots(
        entries={
            day: tuple(choice[value] for value in sorted(choice))
            for day, choice in sorted(by_day.items())
        },
        ignored_days=tuple(sorted(ignored)),
        invalid_days=tuple(sorted(invalid)),
        profile_required_days=tuple(sorted(profile_required)),
    )
