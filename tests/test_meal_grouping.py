from decimal import Decimal
from types import SimpleNamespace

import pytest

from calories_bot.analyzer import ModelPricing
from calories_bot.meal_grouping import (
    MealGroupAssignment,
    MealGrouping,
    MealGroupingError,
    OpenAIMealGrouper,
)


class FakeRecorder:
    def __init__(self) -> None:
        self.calls = []

    def record_llm_usage(self, *args) -> None:
        self.calls.append(args)


def _grouper(output: MealGrouping, usage=None):
    seen = SimpleNamespace(kwargs=None)

    def parse(**kwargs):
        seen.kwargs = kwargs
        return SimpleNamespace(output_parsed=output, usage=usage)

    grouper = OpenAIMealGrouper.__new__(OpenAIMealGrouper)
    grouper._client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    grouper._model = "group-model"
    grouper._effort = "medium"
    grouper._pricing = ModelPricing(None, None, None)
    grouper._usage_recorder = None
    return grouper, seen


def test_grouping_uses_structured_ids_and_dedicated_model_effort() -> None:
    grouper, seen = _grouper(
        MealGrouping(
            assignments=[
                MealGroupAssignment(source_id=1, group_name=" Кава "),
                MealGroupAssignment(source_id=0, group_name="Вино"),
            ]
        )
    )

    result = grouper.group(("Сухе вино", "Кава чорна"))

    assert result.group_names == ("Вино", "Кава")
    assert seen.kwargs["model"] == "group-model"
    assert seen.kwargs["reasoning"] == {"effort": "medium"}
    assert seen.kwargs["store"] is False
    assert seen.kwargs["text_format"] is MealGrouping
    assert '"source_id": 0' in seen.kwargs["input"][1]["content"]


@pytest.mark.parametrize(
    "assignments",
    [
        [MealGroupAssignment(source_id=0, group_name="Вино")],
        [
            MealGroupAssignment(source_id=0, group_name="Вино"),
            MealGroupAssignment(source_id=0, group_name="Кава"),
        ],
        [
            MealGroupAssignment(source_id=0, group_name="Вино"),
            MealGroupAssignment(source_id=2, group_name="Кава"),
        ],
    ],
)
def test_grouping_rejects_missing_duplicate_or_unknown_ids(assignments) -> None:
    grouper, _ = _grouper(MealGrouping(assignments=assignments))

    with pytest.raises(MealGroupingError):
        grouper.group(("Вино", "Кава"))


def test_grouping_records_tokens_and_cost_in_common_statistics() -> None:
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        input_tokens_details=SimpleNamespace(cached_tokens=40),
    )
    grouper, _ = _grouper(
        MealGrouping(assignments=[MealGroupAssignment(source_id=0, group_name="Вино")]),
        usage,
    )
    recorder = FakeRecorder()
    grouper._pricing = ModelPricing(Decimal("2"), Decimal("1"), Decimal("10"))
    grouper._usage_recorder = recorder

    result = grouper.group(("Сухе вино",))

    assert result.metadata.input_tokens == 100
    assert result.metadata.cached_input_tokens == 40
    assert result.metadata.output_tokens == 20
    assert result.metadata.llm_cost_usd == Decimal("0.00036")
    assert recorder.calls[0][1:] == (
        "group-model",
        100,
        40,
        20,
        Decimal("0.00036"),
    )


def test_empty_grouping_does_not_call_openai() -> None:
    grouper, seen = _grouper(MealGrouping(assignments=[]))

    result = grouper.group(())

    assert result.group_names == ()
    assert seen.kwargs is None


def test_grouping_accepts_more_than_twenty_valid_groups() -> None:
    assignments = [
        MealGroupAssignment(source_id=index, group_name=f"Група {index}")
        for index in range(21)
    ]
    grouper, _ = _grouper(MealGrouping(assignments=assignments))

    result = grouper.group(tuple(f"Страва {index}" for index in range(21)))

    assert result.group_names == tuple(f"Група {index}" for index in range(21))
