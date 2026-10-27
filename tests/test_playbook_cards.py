from __future__ import annotations

import argparse
import json
from importlib.resources import files
from pathlib import Path

import pytest

from evallab.hackbook.cli import build_hackbook_parser
from evallab.hackbook.playbook_cards import (
    format_playbook_cards,
    list_playbook_cards,
    match_playbook_cards,
)


def _ids(cards) -> list[str]:
    return [card["card_id"] for card in cards]


def _run_cards(tmp_path: Path, *argv: str) -> int:
    parser = argparse.ArgumentParser()
    build_hackbook_parser(parser.add_subparsers(required=True))
    args = parser.parse_args(["hackbook", "cards", *argv])
    return args.func(args, tmp_path)


def test_packaged_catalog_contains_only_the_twelve_compact_cards() -> None:
    payload = json.loads(
        files("evallab.hackbook").joinpath("playbook_cards_v1.json").read_text(encoding="utf-8")
    )
    assert set(payload) == {"schema", "cards"}
    assert payload["schema"] == "evallab.playbook_cards/v1"
    cards = list_playbook_cards()
    assert _ids(cards) == [f"QA{i:02}" for i in range(1, 7)] + [
        f"TW{i:02}" for i in range(1, 7)
    ]
    assert cards == payload["cards"]
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
    listed = list_playbook_cards()
    listed[0]["card_id"] = "changed"
    listed[0]["trigger_flags"].append("invented")
    listed.clear()
    assert list_playbook_cards()[0]["card_id"] == "QA01"
    assert "invented" not in list_playbook_cards()[0]["trigger_flags"]
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


def test_formatter_is_compact_card_json_without_audit_or_invented_index() -> None:
    cards = match_playbook_cards(["reasoning_eval_awareness", "git_history_peek"])
    rendered = format_playbook_cards(cards)
    lines = rendered.splitlines()
    assert len(lines) == 2
    assert [json.loads(line) for line in lines] == cards
    assert all(line == json.dumps(card, ensure_ascii=False, separators=(",", ":"))
               for line, card in zip(lines, cards, strict=True))
    assert format_playbook_cards([]) == ""


def test_cli_without_flags_lists_only_a_short_index(tmp_path: Path, capsys) -> None:
    assert _run_cards(tmp_path) == 0
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 12
    assert "QA01" in captured.out and "TW06" in captured.out
    assert "verification_recipe" not in captured.out
    assert "flags=" in captured.out
    assert "leads, not verdicts" in captured.err


def test_cli_kind_filters_index_and_explicit_json_catalog(tmp_path: Path, capsys) -> None:
    assert _run_cards(tmp_path, "--kind", "trace") == 0
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 6
    assert "TW01" in captured.out and "QA01" not in captured.out
    assert _run_cards(tmp_path, "--kind", "task", "--json") == 0
    assert _ids(json.loads(capsys.readouterr().out)) == [f"QA{i:02}" for i in range(1, 7)]


def test_cli_flags_emit_only_selected_cards(tmp_path: Path, capsys) -> None:
    assert _run_cards(
        tmp_path, "--flag", "test_exclusion_flag", "--flag", "test_or_grader_edit",
        "--flag", "git_history_peek", "--max-cards", "1",
    ) == 0
    captured = capsys.readouterr()
    assert [json.loads(line)["card_id"] for line in captured.out.splitlines()] == ["TW05"]
    assert "1 matching cards" in captured.err
    assert _run_cards(tmp_path, "--flag", "git_history_peek", "--json") == 0
    assert _ids(json.loads(capsys.readouterr().out)) == ["TW02"]


def test_cli_no_matches_and_negative_limit(tmp_path: Path, capsys) -> None:
    assert _run_cards(tmp_path, "--flag", "unknown", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []
    assert _run_cards(tmp_path, "--flag", "git_history_peek", "--max-cards", "-1") == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "max_cards must be nonnegative" in captured.err


def test_root_cli_registers_cards(tmp_path: Path, capsys) -> None:
    from evallab.cli import parser

    args = parser().parse_args(["hackbook", "cards", "--flag", "package_fetch", "--json"])
    assert args.func(args, tmp_path) == 0
    assert _ids(json.loads(capsys.readouterr().out)) == ["TW03"]
