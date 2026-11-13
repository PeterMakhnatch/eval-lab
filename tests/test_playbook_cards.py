from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest

from evallab.hackbook.playbook_cards import (
    match_playbook_cards,
)


def _ids(cards) -> list[str]:
    return [card["card_id"] for card in cards]


def test_packaged_catalog_contains_only_the_twelve_compact_cards() -> None:
    payload = json.loads(
        files("evallab.hackbook").joinpath("playbook_cards_v1.json").read_text(encoding="utf-8")
    )
    assert set(payload) == {"schema", "cards"}
    assert payload["schema"] == "evallab.playbook_cards/v1"
    cards = payload["cards"]
    assert _ids(cards) == [f"QA{i:02}" for i in range(1, 7)] + [
        f"TW{i:02}" for i in range(1, 7)
    ]
    for card in cards:
        assert set(card) == {
            "card_id", "trigger_flags", "mechanism_summary", "signature_pattern",
            "verification_recipe", "dev_support_count",
        }
        assert card["trigger_flags"]
        assert len(card["mechanism_summary"].splitlines()) == 2
        assert len(card["signature_pattern"].splitlines()) == 3
        assert len(card["verification_recipe"].splitlines()) == 2
        assert card["dev_support_count"] > 0


def test_catalog_works_outside_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _ids(match_playbook_cards("git_history_peek")) == ["TW02"]


def test_returned_cards_cannot_poison_cached_catalog() -> None:
    matched = match_playbook_cards(["git_history_peek"])
    matched[0]["trigger_flags"].clear()
    assert _ids(match_playbook_cards(["git_history_peek"])) == ["TW02"]


@pytest.mark.parametrize(
    ("flags", "kind", "expected"),
    [
        (["conftest_or_sitecustomize_writable", "git_history_peek"], "task", ["QA01"]),
        (["conftest_or_sitecustomize_writable", "git_history_peek"], "trace", ["TW02"]),
        (["reasoning_eval_awareness", "git_history_peek"], None, ["TW01", "TW02"]),
        (["git_history_peek", "test_exclusion_flag", "test_or_grader_edit"], None,
         ["TW05", "TW02"]),
        (["label:fabricated_verification"], "trace", ["TW04"]),
        (["issue:zero_invariant_tolerance"], "task", ["QA05"]),
        (["single_seed_or_no_repeat", "test_only_numeric_constants"], "task", ["QA03"]),
        (["not_a_known_flag"], None, []),
        ([], None, []),
    ],
)
def test_matches_fired_names_and_kind_without_fallback(flags, kind, expected) -> None:
    assert _ids(match_playbook_cards(flags, kind=kind)) == expected


def test_duplicate_flags_do_not_inflate_rank_and_default_is_two_cards() -> None:
    flags = [
        "test_or_grader_edit", "test_or_grader_edit", "reasoning_eval_awareness",
        "answer_path_probe", "git_history_peek",
    ]
    assert _ids(match_playbook_cards(flags)) == ["TW01", "TW02"]
    assert _ids(match_playbook_cards(reversed(flags))) == ["TW01", "TW02"]
    assert _ids(match_playbook_cards(flags, max_cards=3)) == ["TW01", "TW02", "TW05"]
    assert match_playbook_cards(flags, max_cards=0) == []


def test_invalid_kind_limit_and_state_mapping_are_not_silently_matched() -> None:
    with pytest.raises(ValueError, match="kind"):
        match_playbook_cards(["git_history_peek"], kind="unknown")
    with pytest.raises(ValueError, match="max_cards"):
        match_playbook_cards(["git_history_peek"], max_cards=-1)
    with pytest.raises(TypeError, match="fired flag names"):
        match_playbook_cards({"git_history_peek": False})


