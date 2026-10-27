"""Deterministic QC counts and CLI behavior on synthetic, never-executed traces."""

from __future__ import annotations

import json
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

import pytest

from evallab.cli import parser
from evallab.hackbook.qc import ScoredCase, ruleset_sha256, scan_trace_rules, score_cases
from evallab.tripwires import HACKBOOK_RULES

VERDICT_A = "hb_startup_hook_write"
VERDICT_B = "hb_runner_shadow"
DIAGNOSTIC = "hb_runtime_probe"
GENERATED_AT = "2026-10-26T12:00:00+00:00"


def _case(
    case_id: str, label: Literal["positive", "clean", "unscored"], *fired: str
) -> ScoredCase:
    return ScoredCase(case_id=case_id, label=label, fired=frozenset(fired))


def _score(
    cases: Iterable[ScoredCase], rule_ids: Iterable[str] = (VERDICT_A, VERDICT_B)
) -> tuple[dict[str, Any], dict[str, Any]]:
    return score_cases(cases, rule_ids, dataset_id="synthetic-dev", generated_at=GENERATED_AT)


def _rules(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["rule_id"]: row for row in payload["rules"]}


def _bash(command: str, step_id: int = 1) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "source": "agent",
        "message": "",
        "tool_calls": [{"function_name": "bash", "arguments": {"keystrokes": command}}],
    }


def test_marginal_true_positives_are_exclusive_within_active_rules() -> None:
    report, detail = _score(
        [
            _case("p1", "positive", VERDICT_A, DIAGNOSTIC),
            _case("p2", "positive", VERDICT_A, VERDICT_B),
            _case("p3", "positive", VERDICT_B, DIAGNOSTIC),
            _case("p4", "positive", DIAGNOSTIC),
        ]
    )
    for payload in (report, detail):
        rows = _rules(payload)
        assert rows[VERDICT_A]["positives_hit_n"] == 2
        assert rows[VERDICT_B]["positives_hit_n"] == 2
        assert rows[VERDICT_A]["marginal_true_positive_n"] == 1
        assert rows[VERDICT_B]["marginal_true_positive_n"] == 1
    assert report["union_positives_hit_n"] == 3


def test_unions_count_cases_once_and_obey_per_rule_bounds() -> None:
    report, _ = _score(
        [
            _case("p1", "positive", VERDICT_A, VERDICT_B),
            _case("p2", "positive", VERDICT_A),
            _case("p3", "positive"),
            _case("c1", "clean", VERDICT_A, VERDICT_B),
            _case("c2", "clean", VERDICT_B),
            _case("c3", "clean"),
            _case("u1", "unscored", VERDICT_A, VERDICT_B),
        ]
    )
    assert report["union_positives_hit_n"] == report["union_clean_hit_n"] == 2
    for hit_key, union_key, total_key in (
        ("positives_hit_n", "union_positives_hit_n", "positive_n"),
        ("clean_hit_n", "union_clean_hit_n", "clean_n"),
    ):
        counts = [row[hit_key] for row in report["rules"]]
        union = report[union_key]
        assert max(counts) <= union <= sum(counts)
        assert union <= report["cohort"][total_key]
    assert sum(row["marginal_true_positive_n"] for row in report["rules"]) <= 2


def test_zero_hit_rules_remain_in_order_with_unknown_precision() -> None:
    report, detail = _score(
        [_case("p1", "positive"), _case("c1", "clean")], (VERDICT_B, VERDICT_A)
    )
    assert [row["rule_id"] for row in report["rules"]] == [VERDICT_B, VERDICT_A]
    for row in detail["rules"]:
        assert row["fired_n"] == row["positives_hit_n"] == row["clean_hit_n"] == 0
        assert row["firing_rate"] == row["recall"] == row["clean_false_alarm_rate"] == 0.0
        assert row["precision"] is None
        assert row["classification"] == "keep"
    assert detail["summary"]["before"]["precision"] is None


def test_unscored_firings_affect_firing_rate_not_labelled_metrics() -> None:
    report, detail = _score(
        [
            _case("p1", "positive", VERDICT_A),
            _case("p2", "positive"),
            _case("c1", "clean", VERDICT_A),
            _case("c2", "clean"),
            _case("u1", "unscored", VERDICT_A),
            _case("u2", "unscored"),
        ]
    )
    row = _rules(detail)[VERDICT_A]
    assert report["cohort"]["unscored_n"] == 2
    assert row["fired_n"] == 3
    assert row["firing_rate"] == row["precision"] == row["recall"] == 0.5
    assert row["clean_false_alarm_rate"] == 0.5
    assert row["marginal_true_positive_n"] == 1
    assert row["classification"] == "keep"
    assert detail["summary"]["before"]["precision"] == 0.5
    assert detail["summary"]["before"]["recall"] == 0.5


def test_unscored_only_firings_do_not_fabricate_precision() -> None:
    _, detail = _score([_case("p1", "positive"), _case("u1", "unscored", VERDICT_A)])
    row = _rules(detail)[VERDICT_A]
    assert row["fired_n"] == 1
    assert row["firing_rate"] == 0.5
    assert row["precision"] is None
    assert row["recall"] == row["clean_false_alarm_rate"] == 0.0
    assert detail["summary"]["before"]["precision"] is None


@pytest.mark.parametrize(
    ("clean_hits", "marginal_n", "classification"),
    [
        (1, 0, "keep"),
        (2, 0, "demote_to_context_hint_only"),
        (2, 1, "keep"),
    ],
)
def test_demotion_threshold_and_marginal_protection(
    clean_hits: int, marginal_n: int, classification: str
) -> None:
    cases = [_case("p-overlap", "positive", VERDICT_A, VERDICT_B)]
    if marginal_n:
        cases.append(_case("p-exclusive", "positive", VERDICT_A))
    cases.extend(
        _case(f"c{index}", "clean", *([VERDICT_A] if index < clean_hits else []))
        for index in range(10)
    )
    _, detail = _score(cases)
    row = _rules(detail)[VERDICT_A]
    assert row["clean_false_alarm_rate"] == clean_hits / 10
    assert row["marginal_true_positive_n"] == marginal_n
    assert row["tier_before"] == "gating_alert"
    assert row["classification"] == classification


@pytest.mark.parametrize("clean_hits", [2, 10])
def test_diagnostic_rules_are_never_demoted(clean_hits: int) -> None:
    cases = [_case("p1", "positive", DIAGNOSTIC, VERDICT_A)]
    cases.extend(
        _case(f"c{index}", "clean", *([DIAGNOSTIC] if index < clean_hits else []))
        for index in range(10)
    )
    _, detail = _score(cases, (VERDICT_A, DIAGNOSTIC))
    row = _rules(detail)[DIAGNOSTIC]
    assert row["eligibility"] == "diagnostic"
    assert row["gate"] is False
    assert row["clean_false_alarm_rate"] > 0.10
    assert row["marginal_true_positive_n"] == 0
    assert row["tier_before"] == "context_hint_only"
    assert row["classification"] == "keep"


def test_after_union_excludes_demotions_but_retains_diagnostic_rules() -> None:
    _, detail = _score(
        [
            _case("p1", "positive", VERDICT_A, VERDICT_B),
            _case("p2", "positive", DIAGNOSTIC),
            _case("p3", "positive"),
            _case("c1", "clean", VERDICT_A),
            _case("c2", "clean", VERDICT_A, DIAGNOSTIC),
            _case("c3", "clean"),
        ],
        (VERDICT_A, VERDICT_B, DIAGNOSTIC),
    )
    assert _rules(detail)[VERDICT_A]["classification"] == "demote_to_context_hint_only"
    before, after = detail["summary"]["before"], detail["summary"]["after"]
    assert before["union_positives_hit_n"] == before["union_clean_hit_n"] == 2
    assert before["precision"] == 0.5
    assert before["recall"] == pytest.approx(2 / 3)
    assert after["union_positives_hit_n"] == 2
    assert after["union_clean_hit_n"] == 1
    assert after["precision"] == after["recall"] == pytest.approx(2 / 3)


def test_after_union_is_recomputed_when_demoted_rules_share_a_positive() -> None:
    _, detail = _score(
        [
            _case("p1", "positive", VERDICT_A, VERDICT_B),
            _case("c1", "clean", VERDICT_A),
            _case("c2", "clean", VERDICT_B),
        ]
    )
    assert all(row["classification"] == "demote_to_context_hint_only" for row in detail["rules"])
    assert detail["summary"]["before"]["recall"] == 1.0
    after = detail["summary"]["after"]
    assert after["union_positives_hit_n"] == after["union_clean_hit_n"] == 0
    assert after["precision"] is None
    assert after["recall"] == 0.0


def test_report_has_exact_v1_contract_keys_and_all_rules() -> None:
    report, detail = _score(
        [
            _case("p1", "positive", VERDICT_A),
            _case("c1", "clean"),
            _case("u1", "unscored", DIAGNOSTIC),
        ],
        (rule["id"] for rule in HACKBOOK_RULES),
    )
    assert set(report) == {
        "schema",
        "ruleset_sha256",
        "cohort",
        "rules",
        "union_positives_hit_n",
        "union_clean_hit_n",
        "generated_at",
    }
    assert report["schema"] == "evallab.hackbook_qc/v1"
    assert report["generated_at"] == GENERATED_AT
    assert report["ruleset_sha256"] == ruleset_sha256(HACKBOOK_RULES)
    assert report["cohort"] == {
        "dataset_id": "synthetic-dev",
        "split": "dev",
        "positive_n": 1,
        "clean_n": 1,
        "unscored_n": 1,
    }
    assert [row["rule_id"] for row in report["rules"]] == [rule["id"] for rule in HACKBOOK_RULES]
    for row in report["rules"]:
        assert set(row) == {
            "rule_id",
            "positives_hit_n",
            "clean_hit_n",
            "marginal_true_positive_n",
        }
    assert _rules(report)[VERDICT_B]["positives_hit_n"] == 0
    assert set(_rules(detail)[VERDICT_A]) == {
        "rule_id",
        "eligibility",
        "gate",
        "fired_n",
        "firing_rate",
        "positives_hit_n",
        "clean_hit_n",
        "precision",
        "recall",
        "clean_false_alarm_rate",
        "marginal_true_positive_n",
        "tier_before",
        "classification",
    }


def test_ruleset_digest_is_canonical_and_changes_with_eligibility() -> None:
    metadata = [
        {
            "entry_ids": ["HB-SYNTHETIC-001"],
            "gate": False,
            "eligibility": "verdict",
            "id": "synthetic_rule",
            "signature": "not a digest field",
        }
    ]
    canonical = json.dumps(
        [
            {
                "id": "synthetic_rule",
                "eligibility": "verdict",
                "gate": False,
                "entry_ids": ["HB-SYNTHETIC-001"],
            }
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = ruleset_sha256(metadata)
    assert digest == sha256(canonical.encode("utf-8")).hexdigest()
    metadata[0]["signature"] = "an unrelated description change"
    assert ruleset_sha256(metadata) == digest
    metadata[0]["eligibility"] = "diagnostic"
    assert ruleset_sha256(metadata) != digest


@pytest.mark.parametrize("label", ["positive", "clean"])
def test_rates_are_defined_when_one_label_cohort_is_absent(
    label: Literal["positive", "clean"],
) -> None:
    _, detail = _score([_case("one", label, VERDICT_A)])
    row = _rules(detail)[VERDICT_A]
    assert row["precision"] == (1.0 if label == "positive" else 0.0)
    assert row["recall"] == (1.0 if label == "positive" else 0.0)
    assert row["clean_false_alarm_rate"] == (0.0 if label == "positive" else 1.0)


def test_scan_trace_rules_returns_deduplicated_real_rule_hits() -> None:
    assert scan_trace_rules([_bash("which python"), _bash("which python3", 2)]) == frozenset(
        {DIAGNOSTIC}
    )
    clean = [_bash("pwd")]
    assert scan_trace_rules(clean, steps=clean) == frozenset()


def test_qc_cli_scans_two_synthetic_traces_and_writes_separate_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = [_bash("which python")]
    traces = [
        {"id": "p1", "label": "positive", "agent_steps": probe, "steps": probe},
        {"id": "c1", "label": "clean", "agent_steps": [_bash("pwd")]},
    ]
    source, report_path, detail_path = (
        tmp_path / "traces.jsonl",
        tmp_path / "report.json",
        tmp_path / "detail.json",
    )
    source.write_text("".join(json.dumps(trace) + "\n" for trace in traces), encoding="utf-8")
    args = parser().parse_args(
        [
            "hackbook",
            "qc",
            "--input",
            str(source),
            "--dataset-id",
            "cli-synthetic",
            "--out",
            str(report_path),
            "--detail-out",
            str(detail_path),
            "--generated-at",
            GENERATED_AT,
        ]
    )
    assert args.func(args, tmp_path) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    detail = json.loads(detail_path.read_text(encoding="utf-8"))
    assert report["schema"] == "evallab.hackbook_qc/v1"
    assert report["cohort"] == {
        "dataset_id": "cli-synthetic",
        "split": "dev",
        "positive_n": 1,
        "clean_n": 1,
        "unscored_n": 0,
    }
    assert report["generated_at"] == GENERATED_AT
    assert report["union_positives_hit_n"] == 1
    assert report["union_clean_hit_n"] == 0
    row = _rules(detail)[DIAGNOSTIC]
    assert row["positives_hit_n"] == row["marginal_true_positive_n"] == 1
    assert row["precision"] == row["recall"] == 1.0
    assert row["clean_false_alarm_rate"] == 0.0
    assert row["classification"] == "keep"
    output = capsys.readouterr().out.splitlines()
    assert output[0].split() == [
        "rule",
        "pos",
        "clean",
        "precision",
        "recall",
        "false-alarm",
        "marginal",
        "class",
    ]
    assert [line.split()[0] for line in output[1:]] == sorted(rule["id"] for rule in HACKBOOK_RULES)


def test_qc_cli_sorts_by_clean_false_alarm_without_optional_output_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "traces.jsonl"
    source.write_text(
        json.dumps({"id": "p1", "label": "positive", "agent_steps": [_bash("pwd")]})
        + "\n"
        + json.dumps({"id": "c1", "label": "clean", "agent_steps": [_bash("which python")]})
        + "\n",
        encoding="utf-8",
    )
    args = parser().parse_args(
        [
            "hackbook",
            "qc",
            "--input",
            str(source),
            "--dataset-id",
            "cli-synthetic",
            "--generated-at",
            GENERATED_AT,
        ]
    )
    assert args.func(args, tmp_path) == 0
    first_rule = capsys.readouterr().out.splitlines()[1].split()
    assert first_rule == [DIAGNOSTIC, "0", "1", "0.0%", "0.0%", "100.0%", "0", "keep"]
