from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import eval_telegram_e2e as e2e


def test_require_rejects_failed_invariant() -> None:
    with pytest.raises(e2e.E2EFailure, match="missing row"):
        e2e._require(False, "missing row")


def test_message_text_normalizes_empty_and_whitespace() -> None:
    assert e2e._message_text(SimpleNamespace(raw_text="  Готово \n")) == "Готово"
    assert e2e._message_text(SimpleNamespace(raw_text=None)) == ""


def test_button_texts_flattens_keyboard() -> None:
    message = SimpleNamespace(
        buttons=[
            [SimpleNamespace(text="Видалити")],
            [SimpleNamespace(text="Інша"), SimpleNamespace(text="Кнопка")],
        ]
    )

    assert e2e._button_texts(message) == ["Видалити", "Інша", "Кнопка"]


def test_button_texts_accepts_message_without_keyboard() -> None:
    assert e2e._button_texts(SimpleNamespace(buttons=None)) == []


def test_has_button_matches_semantic_text_inside_emoji_label() -> None:
    message = SimpleNamespace(buttons=[[SimpleNamespace(text="🗑 Видалити")]])

    assert e2e._has_button(message, "Видалити") is True
    assert e2e._has_button(message, "Зберегти") is False


def test_button_callback_data_identifies_logical_sheet_row() -> None:
    message = SimpleNamespace(
        buttons=[
            [SimpleNamespace(text="⭐ Зберегти", data=b"save:42:2026-08-12")],
            [SimpleNamespace(text="🗑 Видалити", data=b"delete:42:2026-08-12")],
        ]
    )

    assert e2e._button_callback_data(message, "Видалити") == "delete:42:2026-08-12"


def test_response_with_terms_finds_one_composite_component() -> None:
    responses = [
        SimpleNamespace(raw_text="Гречка 180 г"),
        SimpleNamespace(raw_text="Куряче філе 120 г"),
        SimpleNamespace(raw_text="Салат 100 г"),
    ]

    assert e2e._response_with_terms(responses, ("кур", "філе")) is responses[1]


def test_response_with_terms_rejects_ambiguous_component_match() -> None:
    responses = [
        SimpleNamespace(raw_text="Салат 100 г"),
        SimpleNamespace(raw_text="Салат зі сметаною 80 г"),
    ]

    with pytest.raises(e2e.E2EFailure, match="expected one component"):
        e2e._response_with_terms(responses, ("салат",))


def test_expected_day_summary_supports_goal_and_no_goal_modes() -> None:
    assert e2e._expected_day_summary(319.5, None) == "Сьогодні: 320 кк"
    assert e2e._expected_day_summary(319.5, 1600) == "Сьогодні: 320 із 1600 кк"


def test_sheet_probe_removes_only_suffix_created_after_baseline() -> None:
    calls = []
    probe = e2e.SheetProbe.__new__(e2e.SheetProbe)
    probe.rows = lambda: [["old-1"], ["old-2"], ["test-1"], ["test-2"]]
    probe._worksheet = SimpleNamespace(
        delete_rows=lambda start, end: calls.append((start, end))
    )

    removed = probe.remove_rows_after_baseline([["old-1"], ["old-2"]])

    assert removed == 2
    assert calls == [(4, 5)]


def test_sheet_probe_refuses_fallback_cleanup_if_baseline_changed() -> None:
    probe = e2e.SheetProbe.__new__(e2e.SheetProbe)
    probe.rows = lambda: [["changed"], ["test"]]

    with pytest.raises(e2e.E2EFailure, match="baseline prefix changed"):
        probe.remove_rows_after_baseline([["old"]])


def test_parser_does_not_confirm_live_run_by_default() -> None:
    args = e2e.build_parser().parse_args([])

    assert args.confirm is False
    assert args.timeout == 60
