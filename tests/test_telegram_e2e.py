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


def test_expected_day_summary_supports_goal_and_no_goal_modes() -> None:
    assert e2e._expected_day_summary(319.5, None) == "Сьогодні: 320 кк"
    assert e2e._expected_day_summary(319.5, 1600) == "Сьогодні: 320 із 1600 кк"


def test_parser_does_not_confirm_live_run_by_default() -> None:
    args = e2e.build_parser().parse_args([])

    assert args.confirm is False
    assert args.timeout == 60
