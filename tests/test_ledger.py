"""Tests for the Work-OS ledger dataset (`evallab.ledger`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.ledger import (
    MIN_N,
    build_scoreboard,
    parse_identity,
    parse_lane_spec,
    parse_ledger,
    parse_line,
    render_scoreboard,
    sum_session_cost,
)

REAL_LEDGER = Path(__file__).resolve().parents[1] / "research/inbox/ledger.md"


def test_parse_identity_with_model() -> None:
    pane, model, known = parse_identity("wH:p1 (GPT-5.6-sol)")
    assert pane == "wH:p1"
    assert model == "GPT-5.6-sol"
    assert known is True


def test_parse_identity_legacy_collective() -> None:
    pane, model, known = parse_identity("engineer-data + ops")
    assert pane == "engineer-data + ops"
    assert model is None
    assert known is False


def test_parse_lane_spec_variants() -> None:
    assert parse_lane_spec("analysis #1 reward gap") == ("analysis", 1, "reward gap")
    assert parse_lane_spec("good-codebase") == ("good-codebase", None, "")
    assert parse_lane_spec("training-signal") == ("training-signal", None, "")


def test_parse_line_full_shape() -> None:
    mark = parse_line(
        "- wS:pA (zai/glm-5.3-flash) → good-codebase #2 executor hygiene: ✓ — "
        "commit 21561c98 pushed, green; pending review"
    )
    assert mark.pane == "wS:pA"
    assert mark.model == "zai/glm-5.3-flash"
    assert mark.lane == "good-codebase"
    assert mark.task_n == 2
    assert mark.task == "executor hygiene"
    assert mark.mark == "✓"
    assert mark.passed is True
    assert "21561c98" in mark.note


def test_parse_line_blocked_tilde() -> None:
    mark = parse_line(
        "- wS:p2 Synth → training-signal: Track C PR #360: ~ — BLOCKED at review"
    )
    assert mark.pane == "wS:p2 Synth"
    assert mark.mark == "~"
    assert mark.passed is False


def test_parse_line_rejects_missing_mark() -> None:
    with pytest.raises(ValueError, match="mark"):
        parse_line("- wK:p9 → system-design: delivered criteria with no verdict")


def test_parse_ledger_real_file_all_lines() -> None:
    marks, malformed = parse_ledger(REAL_LEDGER.read_text())
    assert marks, "real ledger must parse to rows"
    assert malformed == [], f"malformed ledger lines: {malformed}"
    assert all(m.mark in ("✓", "~", "✗") for m in marks)


def test_parse_ledger_collects_malformed_not_raise() -> None:
    text = "\n".join(
        [
            "# header",
            "- wS:pA (zai) → analysis #1 reward gap: ✓ — fine",
            "not a ledger line",
            "- broken because no arrow",
        ]
    )
    marks, malformed = parse_ledger(text)
    assert len(marks) == 1
    assert len(malformed) == 1
    assert "broken" in malformed[0]


def _tmp_session(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_sum_session_cost_sums_assistant_totals(tmp_path: Path) -> None:
    path = _tmp_session(
        tmp_path,
        [
            {"type": "session", "id": "x"},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "usage": {"cost": {"input": 0.01, "output": 0.02, "total": 0.03}},
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "user",
                    "usage": {"cost": {"total": 999.0}},
                },
            },
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "usage": {"cost": {"total": 0.07}},
                },
            },
        ],
    )
    assert sum_session_cost(path) == pytest.approx(0.10)


def test_sum_session_cost_tolerates_missing_and_bad_rows(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        "\n".join(
            [
                "{not json",
                json.dumps({"type": "message", "message": {"role": "assistant"}}),
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "usage": {"cost": {"total": "oops"}},
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "usage": {"cost": {"total": 1.5}},
                        },
                    }
                ),
            ]
        )
        + "\n"
    )
    assert sum_session_cost(path) == pytest.approx(1.5)


def test_scoreboard_insufficient_n_and_wilson() -> None:
    from evallab.ledger import LedgerMark

    marks = [
        LedgerMark(
            pane="wS:pA",
            model="zai",
            model_known=True,
            lane="analysis",
            task_n=1,
            task="a",
            mark="✓",
            note="",
            raw="- x",
            cost_usd=0.5,
        ),
        LedgerMark(
            pane="wS:pA",
            model="zai",
            model_known=True,
            lane="analysis",
            task_n=2,
            task="b",
            mark="✗",
            note="",
            raw="- x",
        ),
        LedgerMark(
            pane="wK:p7",
            model="Fable",
            model_known=True,
            lane="analysis",
            task_n=3,
            task="c",
            mark="✓",
            note="",
            raw="- x",
        ),
    ]
    cells = build_scoreboard(marks)
    by_actor = {c.actor: c for c in cells}
    small = by_actor["wK:p7 (Fable)"]
    assert small.n == 1
    assert small.sufficient is False
    big = by_actor["wS:pA (zai)"]
    assert big.n == MIN_N - 3  # still below threshold in this fixture
    assert big.sufficient is False

    enough = [
        LedgerMark(
            pane="wS:pA",
            model="zai",
            model_known=True,
            lane="tooling",
            task_n=i,
            task=f"t{i}",
            mark="✓" if i < 4 else "✗",
            note="",
            raw="- x",
            cost_usd=float(i) if i < 4 else None,
        )
        for i in range(1, 6)
    ]
    cells = build_scoreboard(enough)
    assert len(cells) == 1
    cell = cells[0]
    assert cell.sufficient is True
    assert cell.wins == 3
    assert cell.rate == pytest.approx(0.6)
    assert cell.wilson_low is not None and cell.wilson_high is not None
    assert cell.wilson_low < 0.6 < cell.wilson_high
    assert cell.median_cost_per_win == pytest.approx(2.0)


def test_render_scoreboard_prints_insufficient_n() -> None:
    from evallab.ledger import LedgerMark

    mark = LedgerMark(
        pane="wS:pA",
        model="zai",
        model_known=True,
        lane="analysis",
        task_n=1,
        task="a",
        mark="✓",
        note="",
        raw="- x",
    )
    rendered = render_scoreboard(build_scoreboard([mark]))
    assert "insufficient n" in rendered
    assert "| analysis |" in rendered


def test_render_scoreboard_is_byte_stable() -> None:
    from evallab.ledger import LedgerMark

    def row(i: int, mark: str) -> LedgerMark:
        return LedgerMark(
            pane="wS:pA",
            model="zai",
            model_known=True,
            lane="tooling",
            task_n=i,
            task=f"t{i}",
            mark=mark,
            note="",
            raw="- x",
            cost_usd=0.25,
        )

    marks = [row(i, "✓" if i < 4 else "✗") for i in range(1, 6)]
    assert render_scoreboard(build_scoreboard(marks)) == render_scoreboard(
        build_scoreboard(marks)
    )
