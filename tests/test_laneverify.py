"""Behavioral failures must remain visible without network or model calls."""

import json
import os

import pytest

from evallab.laneverify import numbers, sessions, verify


def gh(argv, cwd):
    if argv[:3] == ["gh", "pr", "view"]:
        return 1, "GraphQL: Could not resolve to a PullRequest"
    return 0, ""


def test_false_pr_number_and_nested_paid_route_are_flagged(tmp_path):
    lane = tmp_path / "lane"
    lane.mkdir()
    report = lane / "RESULT.md"
    report.write_text(
        "Passed 8 of 10 cases. Receipt: `counts.json`.\nPR #999999 merged with CI passed.\n"
    )
    (lane / "counts.json").write_text('{"passed": 3, "total": 10}')
    nested = lane / "workers" / "nested" / "helper" / "session"
    nested.mkdir(parents=True)
    (nested / "turns.jsonl").write_text(
        json.dumps(
            {
                "type": "message",
                "message": {"role": "assistant", "provider": "deepseek", "model": "deepseek-flash"},
            }
        )
        + "\n"
    )
    claims = [
        {
            "quote": report.read_text().splitlines()[0],
            "number": 0,
            "recipe": {"op": "value", "file": "counts.json", "pointer": "/passed"},
        }
    ]
    result = verify(
        lane, tmp_path / "out", tmp_path, recipes=claims, allowed=["openai-codex/*"], run=gh
    )
    kinds = {d["kind"] for d in result["discrepancies"]}
    assert {"number_differs", "pr_unverified", "paid_route", "off_route"} <= kinds
    assert result["served_turns"] == {"deepseek/deepseek-flash": 1}
    assert result["status"] == "discrepancies"


def test_all_numbers_need_receipts_and_quotes_are_bound(tmp_path):
    report = tmp_path / "RESULT.md"
    report.write_text("Passed 3 of 10 cases (30.0%).\n")
    (tmp_path / "counts.json").write_text('{"passed": 3, "total": 10}')
    quote = report.read_text().strip()
    recipes = [
        {
            "quote": quote,
            "number": 0,
            "recipe": {"op": "value", "file": "counts.json", "pointer": "/passed"},
        },
        {
            "quote": quote,
            "number": 1,
            "recipe": {"op": "value", "file": "counts.json", "pointer": "/total"},
        },
        {
            "quote": quote,
            "number": 2,
            "scale": 100,
            "recipe": {
                "op": "ratio",
                "num": {"op": "value", "file": "counts.json", "pointer": "/passed"},
                "den": {"op": "value", "file": "counts.json", "pointer": "/total"},
            },
        },
    ]
    claims, errors = numbers(report, recipes)
    assert not errors
    assert [n["actual"] for n in claims[0]["numbers"]] == [3, 10, 30]
    report.write_text("Passed 4 of 10 cases (40.0%).\n")
    _, errors = numbers(report, recipes)
    assert {e["kind"] for e in errors} == {"recipe_quote_missing", "numbers_unverified"}


def test_unknown_route_and_malformed_session_fail_closed(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    (session / "log.jsonl").write_text(
        '{"type":"session"}\ninvalid\n'
        + json.dumps({"type": "message", "message": {"role": "assistant"}})
        + "\n"
    )
    _, errors = sessions(tmp_path, ["openai-codex/*"])
    assert {e["kind"] for e in errors} == {
        "session_invalid_json",
        "served_route_unknown",
        "off_route",
    }


def test_no_output_inside_lane_or_overwrite(tmp_path):
    lane = tmp_path / "lane"
    lane.mkdir()
    with pytest.raises(ValueError, match="outside"):
        verify(lane, lane / "out", tmp_path, run=gh)
    out = tmp_path / "out"
    out.mkdir()
    (out / "VERIFY.json").write_text("existing evidence")
    with pytest.raises(ValueError, match="already contains"):
        verify(lane, out, tmp_path, run=gh)
    assert (out / "VERIFY.json").read_text() == "existing evidence"


def test_artifact_modified_after_claim_is_not_claimed_absent(tmp_path):
    lane = tmp_path / "lane"
    lane.mkdir()
    report = lane / "RESULT.md"
    report.write_text("Evidence: `receipt.json`.\n")
    receipt = lane / "receipt.json"
    receipt.write_text("{}")
    os.utime(report, (100, 100))
    os.utime(receipt, (200, 200))
    result = verify(lane, tmp_path / "out", tmp_path, run=gh)
    assert result["artifacts"][0]["status"] == "exists_now"
    assert result["artifacts"][0]["paths"][0]["chronology"] == "modified_after_report"
    assert "artifact_newer_than_claim" in {e["kind"] for e in result["discrepancies"]}


def test_pr_state_head_ci_and_main_ancestry_are_independent(tmp_path):
    lane = tmp_path / "lane"
    lane.mkdir()
    (lane / "RESULT.md").write_text(
        "PR #12 merged on exact head abcdef123; merge commit ddddddd; CI checks passed.\n"
    )

    def probe(argv, cwd):
        if argv[0] == "gh":
            return 0, json.dumps(
                {
                    "state": "MERGED",
                    "headRefOid": "bbbbbbb",
                    "mergeCommit": {"oid": "ccccccc"},
                    "statusCheckRollup": [
                        {"name": "quality-required", "conclusion": "SKIPPED"},
                        {"name": "typecheck-required", "conclusion": "SUCCESS"},
                    ],
                }
            )
        if argv[:2] == ["git", "merge-base"]:
            return 1, "not an ancestor"
        return 0, ""

    result = verify(lane, tmp_path / "out", tmp_path, run=probe)
    kinds = {e["kind"] for e in result["discrepancies"]}
    assert {
        "pr_head_differs",
        "pr_merge_differs",
        "pr_ci_not_green",
        "merge_not_on_origin_main",
    } <= kinds


def test_statistics_use_receipt_counts_and_printed_precision(tmp_path):
    report = tmp_path / "RESULT.md"
    report.write_text("Wilson lower bound: 10.8%.\nExact paired p: 0.125.\n")
    (tmp_path / "counts.json").write_text('{"k":3,"n":10,"b":4,"c":0}')

    def value(field):
        return {"op": "value", "file": "counts.json", "pointer": "/" + field}

    specs = [
        {
            "quote": "Wilson lower bound: 10.8%.",
            "number": 0,
            "scale": 100,
            "stat": "wilson",
            "field": "low",
            "inputs": {"k": value("k"), "n": value("n")},
        },
        {
            "quote": "Exact paired p: 0.125.",
            "number": 0,
            "stat": "mcnemar_exact",
            "field": "p_value",
            "inputs": {"b": value("b"), "c": value("c")},
        },
    ]
    rows, errors = numbers(report, specs)
    assert not errors
    assert rows[0]["numbers"][0]["actual"] == pytest.approx(10.7791267406)
    assert rows[1]["numbers"][0]["actual"] == 0.125


def test_numeric_code_spans_are_measurements_not_paths(tmp_path):
    report = tmp_path / "RESULT.md"
    report.write_text("Observed `3/10` on `openai-codex/gpt-6.1-sol`.\n")
    rows, errors = numbers(report, [])
    assert [t["text"] for t in rows[0]["numbers"]] == ["3", "10"]
    result = verify(tmp_path, tmp_path.parent / (tmp_path.name + "-out"), tmp_path, run=gh)
    assert not result["artifacts"]


def test_status_citation_time_controls_artifact_chronology(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    lane = tmp_path / "lane"
    lane.mkdir()
    (lane / "RESULT.md").write_text("Receipt: `receipt.json`.\n")
    (lane / "STATUS.md").write_text("2026-11-04 1:00 AM ET | published receipt.json\n")
    receipt = lane / "receipt.json"
    receipt.write_text("{}")
    stamp = datetime(2026, 11, 4, 2, tzinfo=ZoneInfo("America/New_York")).timestamp()
    os.utime(receipt, (stamp, stamp))
    result = verify(lane, tmp_path / "out", tmp_path, run=gh)
    artifact = result["artifacts"][0]["paths"][0]
    assert "STATUS.md:1" in artifact["claim_time_basis"]
    assert artifact["claim_time"] == stamp - 3600
    assert "artifact_newer_than_claim" in {d["kind"] for d in result["discrepancies"]}


@pytest.mark.parametrize("claim", ["merged", "not merged", "unmerged"])
def test_repeated_pr_reference_does_not_hide_state_claim(tmp_path, claim):
    lane = tmp_path / "lane"
    lane.mkdir()
    (lane / "RESULT.md").write_text(
        f"PR #12 (https://github.com/PeterMakhnatch/eval-lab/pull/12) {claim}.\n"
    )

    def probe(argv, cwd):
        if argv[0] == "gh":
            return 0, json.dumps({"state": "MERGED", "mergeCommit": {"oid": "abcdef123"}})
        return 0, ""

    result = verify(lane, tmp_path / "out", tmp_path, run=probe)
    errors = [d for d in result["discrepancies"] if d["kind"] == "pr_state_differs"]
    assert len(errors) == (0 if claim == "merged" else 1)


@pytest.mark.parametrize(
    ("relation", "threshold", "status"),
    [
        ("<", "0.05", "reproduced"),
        ("<", "0.03125", "differs"),
        ("≤", "0.03125", "reproduced"),
        (">", "0.03125", "differs"),
        (">=", "0.03125", "reproduced"),
    ],
)
def test_exact_p_value_bounds_keep_strict_and_inclusive_meaning(
    tmp_path, relation, threshold, status
):
    report = tmp_path / "RESULT.md"
    quote = f"McNemar p {relation} {threshold}."
    report.write_text(quote + "\n")
    (tmp_path / "counts.json").write_text('{"b": 6, "c": 0}')
    claims, errors = numbers(
        report,
        [
            {
                "quote": quote,
                "number": 0,
                "stat": "mcnemar_exact",
                "inputs": {
                    key: {"op": "value", "file": "counts.json", "pointer": "/" + key}
                    for key in ("b", "c")
                },
            }
        ],
    )
    assert claims[0]["numbers"][0]["actual"] == 0.03125
    assert claims[0]["numbers"][0]["status"] == status
    assert {error["kind"] for error in errors} == (
        {"number_differs"} if status == "differs" else set()
    )
